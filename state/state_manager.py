from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from fastapi import WebSocket

_STATS_FILE             = Path("logs/lifetime_stats.json")
_EXECUTOR_BANKROLL_FILE = Path("logs/executor_bankroll.json")
_RESOLUTION_FILE        = Path("logs/resolution_history.json")


def _load_resolution_history() -> list[str]:
    try:
        history = list(json.loads(_RESOLUTION_FILE.read_text()).get("resolutions", []))
        if history:
            return history
    except Exception:
        pass
    return _bootstrap_resolution_history()


def _bootstrap_resolution_history() -> list[str]:
    """Build resolution history from predictions.csv on first run (no JSON file yet)."""
    import csv as _csv
    pred_path = Path("logs/predictions.csv")
    if not pred_path.exists():
        return []
    try:
        rows: list[dict] = []
        with open(pred_path, newline="") as f:
            for row in _csv.DictReader(f):
                rows.append(row)
        messages: list[str] = []
        for row in reversed(rows[-100:]):
            try:
                ticker      = row.get("ticker", "?")
                btc_close   = float(row.get("btc_close") or 0)
                btc_change  = float(row.get("btc_change") or 0)
                resolution  = row.get("resolution", "?")
                src         = row.get("result_source", "?")
                final_side  = row.get("final_model_side", "")
                correct_raw = row.get("prediction_correct", "")
                chg_sign    = "+" if btc_change >= 0 else ""
                pred_label  = ""
                if final_side and correct_raw in ("True", "False"):
                    pred_label = f"  model={final_side} [{'CORRECT' if correct_raw == 'True' else 'WRONG'}]"
                messages.append(
                    f"{ticker}  BTC {btc_close:.2f}  "
                    f"({chg_sign}{btc_change:.2f})  → {resolution} [{src}]{pred_label}"
                )
            except Exception:
                continue
        return messages
    except Exception:
        return []


def _save_resolution_history(history: list[str]) -> None:
    try:
        _RESOLUTION_FILE.parent.mkdir(parents=True, exist_ok=True)
        _RESOLUTION_FILE.write_text(json.dumps({"resolutions": history[:100]}))
    except Exception:
        pass

def _load_pred_stats() -> tuple[int, int, int, int]:
    try:
        data = json.loads(_STATS_FILE.read_text())
        return (
            int(data.get("pred_total", 0)),
            int(data.get("pred_correct", 0)),
            int(data.get("res_pred_total", 0)),
            int(data.get("res_pred_correct", 0)),
        )
    except Exception:
        return 0, 0, 0, 0

def _save_pred_stats(
    pred_total: int, pred_correct: int, res_pred_total: int, res_pred_correct: int
) -> None:
    try:
        _STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
        try:
            existing = json.loads(_STATS_FILE.read_text())
        except Exception:
            existing = {}
        existing.update({
            "pred_total": pred_total,
            "pred_correct": pred_correct,
            "res_pred_total": res_pred_total,
            "res_pred_correct": res_pred_correct,
        })
        _STATS_FILE.write_text(json.dumps(existing))
    except Exception:
        pass


@dataclass
class Orderbook:
    yes_bids: dict = field(default_factory=dict)
    yes_asks: dict = field(default_factory=dict)
    top_yes_bid: Optional[float] = None
    top_yes_ask: Optional[float] = None
    last_update: float = 0.0

    def best_bid(self) -> Optional[float]:
        if self.top_yes_bid is not None:
            return self.top_yes_bid
        return max(self.yes_bids) if self.yes_bids else None

    def best_ask(self) -> Optional[float]:
        if self.top_yes_ask is not None:
            return self.top_yes_ask
        return min(self.yes_asks) if self.yes_asks else None

    def mid(self) -> Optional[float]:
        bb, ba = self.best_bid(), self.best_ask()
        if bb is not None and ba is not None:
            return (bb + ba) / 2.0
        return bb if bb is not None else ba
    
    def imbalance(self) -> Optional[float]:
        bid_vol = sum(self.yes_bids.values())
        ask_vol = sum(self.yes_asks.values())
        total = bid_vol + ask_vol
        if total < 10:
            return None
        return (bid_vol - ask_vol) / total  # +1 = all bids, -1 = all asks

class StateManager:
    def __init__(
        self,
        momentum_threshold_usd: float = 150.0,
        starting_bankroll: float = 250.0,
        live_mode: bool = False,
    ):
        self._live_mode = live_mode
        # Feed state
        self._exchange_prices: dict[str, float] = {
            "coinbase": 0.0, "kraken": 0.0, "bitstamp": 0.0, "gemini": 0.0,
        }
        self.btc_price: float = 0.0
        self.btc_history: deque[tuple[float, float]] = deque(maxlen=6000)
        self.btc_feed_active: bool = False
        self.kalshi_feed_active: bool = False

        self.kalshi_mid_history: deque[tuple[float, float]] = deque(maxlen=2000)

        # Contract / window
        self.active_contract: Optional[str] = None
        self.window_close_ts: float = 0.0
        self.window_open_ts: float = 0.0
        self.window_discovered_ts: float = 0.0
        self.open_interest: float = 0.0
        self.btc_open: float = 0.0

        # Momentum / velocity
        self.momentum_direction: str = "neutral"
        self.velocity_pause: bool = False
        self.velocity_pause_until: float = 0.0
        self.momentum_threshold_usd: float = momentum_threshold_usd

        # Orderbook
        self.orderbook: Orderbook = Orderbook()

        # Technicals
        self.pre_window_bias: str = "neutral"
        self.pre_window_bias_locked: bool = False

        # Bias snapshots at 15m / 10m / 5m remaining
        self.bias_snap_15m: str = ""
        self.bias_snap_10m: str = ""
        self.bias_snap_5m: str = ""
        self.tech_rsi: float = 50.0
        self.tech_adx: float = 25.0
        self.tech_bb_position: float = 0.5
        self.tech_bb_width: float = 0.0
        self.tech_fetched: bool = False

        # GBM model prediction
        self.prediction_yes_pct: float = 50.0
        self.predicted_direction: str = "NEUTRAL"
        self.predicted_btc_close: float = 0.0
        self.predicted_resolution: str = "NEUTRAL"  # "YES" | "NO" | "NEUTRAL" (slope-based)

        # Prediction locked at first entry-open tick (used for accuracy scoring)
        self.prediction_locked_direction: str = "NEUTRAL"
        self.prediction_locked_yes_pct: float = 50.0
        self.prediction_locked: bool = False

        # Final model decision locked at 8-min mark (first entry_open tick, after flip suppression)
        # This is what the executor acts on and what determines model correctness in logs.
        self.final_model_side: Optional[str] = None   # "YES" | "NO" | None
        self.final_model_locked: bool = False
        self.final_model_contract: Optional[str] = None  # contract the lock was set for
        self.final_model_fv: float = 50.0             # GBM fair value (cents) at the moment of lock
        self.final_model_gap: float = 0.0             # GBM–market gap (cents) at the moment of lock

        # First stability hit during entry_open (regardless of gap outcome) — for edge tracking
        self.signal_snapshot: dict = {}

        # External market data
        self.dvol: float = 0.0                   # Deribit DVOL index (annualized %)
        self.futures_basis_pct: float = 0.0      # (futures − spot) / spot × 100
        self.funding_rate_pct: float = 0.0       # Binance perp funding rate (%)
        self.sentiment_fetched: bool = False

        # CVD (Cumulative Volume Delta) from Coinbase trade stream
        self.cvd_window: float = 0.0             # net buy BTC volume since window open
        self.cvd_total: float = 0.0              # total BTC volume since window open
        self.cvd_history: deque[tuple[float, float]] = deque(maxlen=5000)  # (ts, delta)

        # Futures taker buy/sell ratio (Binance Futures — polled every 30 s)
        self.futures_taker_ratio: float = 0.0
        self.futures_taker_ratio_history: deque[tuple[float, float]] = deque(maxlen=12)

        # Kraken PI_XBTUSD perpetual futures — perp basis as a spot lead signal
        self.perp_mid: float = 0.0
        self.perp_index: float = 0.0
        self.perp_basis_history: deque[tuple[float, float]] = deque(maxlen=600)  # 10 min @ ~1/s

        # Open interest history (for OI delta — appended in update_open_interest)
        self.oi_history: deque[tuple[float, float]] = deque(maxlen=600)

        # Binance spot orderbook depth imbalance (WebSocket, 100 ms updates)
        self.binance_depth_imbalance: float = 0.0
        self.binance_depth_history: deque[tuple[float, float]] = deque(maxlen=3000)

        # Liquidation data (Binance Futures forceOrder stream)
        self.liq_long_2m: float = 0.0
        self.liq_short_2m: float = 0.0
        self.liq_history: deque[tuple[float, float, str]] = deque(maxlen=500)  # (ts, usd, side)

        # Analysis conditions (written directly by analyzer — sole writer in asyncio)
        self.analysis: dict = {
            "phase": "waiting",
            "side": None,
            "btc_move_ok": False,
            "price_in_range": False,
            "entry_price": None,
            "line_crossings": None,
            "crossings_ok": None,
            "direction_score": None,
            "direction_ok": None,
        }

        # Recommendation (written directly by analyzer)
        self.recommendation: dict = {
            "side": None,        # "YES" | "NO" | None
            "entry_price": None,
            "confidence": 0.0,
            "signal_count": 0,
            "basis": [],
        }

        # Prediction accuracy (persisted across sessions)
        (
            self.lifetime_pred_total,
            self.lifetime_pred_correct,
            self.lifetime_res_pred_total,
            self.lifetime_res_pred_correct,
        ) = _load_pred_stats()
        self.session_pred_total: int = 0
        self.session_pred_correct: int = 0
        self.session_res_pred_total: int = 0
        self.session_res_pred_correct: int = 0

        # Executor position (one per window)
        self.position: dict = {
            "ticker":     None,
            "side":       None,
            "contracts":  0,
            "fill_price": None,
            "cost":       0.0,
            "status":     "none",   # none | open | won | lost
            "mode":       None,
            "pnl":        None,
        }
        # Executor P&L tracking
        self.executor_bankroll_original: float = starting_bankroll
        self.executor_all_time_trades: int = 0
        # Live mode: balance comes from Kalshi on startup — never read/write the file.
        # Paper mode: persist balance across restarts via executor_bankroll.json.
        if live_mode:
            self.executor_bankroll: float = 0.0  # overwritten by LiveExecutor.startup()
        else:
            self.executor_bankroll = self._load_executor_bankroll(default=starting_bankroll)
        self.executor_session_pnl: float = 0.0
        self.executor_session_trades: int = 0

        # Resolution history (persisted across restarts)
        self.resolution_history: list[str] = _load_resolution_history()

        # Trading mode (set by main.py after executor is chosen)
        self.trading_mode: str = "paper"

        # Logs
        self.event_log: deque[str] = deque(maxlen=200)
        self.last_resolution_msg: str = ""
        self.session_start_ts: float = time.time()

        # Internal
        self._lock = asyncio.Lock()
        self._dirty = asyncio.Event()
        self._connections: set[WebSocket] = set()

    # ── WebSocket connection management ──────────────────────────────────────

    def register_ws(self, ws: WebSocket) -> None:
        self._connections.add(ws)

    def unregister_ws(self, ws: WebSocket) -> None:
        self._connections.discard(ws)

    # ── State-update methods ──────────────────────────────────────────────────

    def _compute_btc_avg(self) -> float:
        active = [p for p in self._exchange_prices.values() if p > 0.0]
        return sum(active) / len(active) if active else 0.0

    async def update_btc(self, price: float) -> None:
        async with self._lock:
            ts = time.time()
            self._exchange_prices["coinbase"] = price
            self.btc_price = self._compute_btc_avg()
            self.btc_history.append((ts, self.btc_price))
            self.btc_feed_active = True
            if self.btc_open == 0.0 and self.active_contract:
                self.btc_open = self.btc_price
            self._update_momentum_velocity(ts, self.btc_price)
        self._dirty.set()

    async def update_exchange_price(self, exchange: str, price: float) -> None:
        async with self._lock:
            self._exchange_prices[exchange] = price
            self.btc_price = self._compute_btc_avg()
        self._dirty.set()

    def _update_momentum_velocity(self, now: float, price: float) -> None:
        history = list(self.btc_history)
        p10 = _nearest_price(history, now - 10.0)
        if p10 is not None and abs(price - p10) > 50.0:
            self.velocity_pause = True
            self.velocity_pause_until = now + 30.0
        elif self.velocity_pause and now >= self.velocity_pause_until:
            self.velocity_pause = False
        p30 = _nearest_price(history, now - 30.0)
        p20 = _nearest_price(history, now - 20.0)
        if p30 is not None and p20 is not None:
            delta_30 = price - p30
            delta_20 = price - p20
            if abs(delta_30) >= self.momentum_threshold_usd and delta_30 * delta_20 > 0:
                self.momentum_direction = "up" if delta_30 > 0 else "down"
            else:
                self.momentum_direction = "neutral"
        else:
            self.momentum_direction = "neutral"

    async def update_orderbook(self, ob: Orderbook) -> None:
        async with self._lock:
            self.orderbook = ob
            self.kalshi_feed_active = True
            mid = ob.mid()
            if mid is not None:
                self.kalshi_mid_history.append((time.time(), mid))
        self._dirty.set()

    async def set_active_contract(
        self, ticker: str, close_ts: float, open_ts: float, open_interest: float = 0.0
    ) -> None:
        async with self._lock:
            self.active_contract = ticker
            self.window_close_ts = close_ts
            self.window_open_ts = open_ts
            self.window_discovered_ts = time.time()
            self.open_interest = open_interest
            self.btc_open = 0.0
            if self.btc_price > 0:
                self.btc_open = self.btc_price
            self.cvd_window = 0.0
            self.cvd_total = 0.0
            self.cvd_history.clear()
            self.prediction_locked_direction = "NEUTRAL"
            self.prediction_locked_yes_pct = 50.0
            self.prediction_locked = False
            self.final_model_side = None
            self.final_model_locked = False
            self.final_model_contract = None
            self.final_model_fv = 50.0
            self.final_model_gap = 0.0
            self.signal_snapshot = {}
            self.pre_window_bias_locked = False
            self.bias_snap_15m = ""
            self.bias_snap_10m = ""
            self.bias_snap_5m  = ""
        self._dirty.set()

    async def set_btc_open(self, price: float) -> None:
        async with self._lock:
            self.btc_open = price
        self._dirty.set()

    async def update_technicals(
        self, rsi: float, adx: float, bb_position: float, bb_width: float, bias: str,
        lock: bool = False,
    ) -> None:
        async with self._lock:
            self.tech_rsi = rsi
            self.tech_adx = adx
            self.tech_bb_position = bb_position
            self.tech_bb_width = bb_width
            if not self.pre_window_bias_locked:
                self.pre_window_bias = bias
            if lock:
                self.pre_window_bias_locked = True
            self.tech_fetched = True
        self._dirty.set()

    async def update_prediction(self, yes_pct: float, predicted_close: float = 0.0) -> None:
        async with self._lock:
            self.prediction_yes_pct = round(yes_pct, 1)
            if yes_pct > 52:
                self.predicted_direction = "UP"
            elif yes_pct < 48:
                self.predicted_direction = "DOWN"
            else:
                self.predicted_direction = "NEUTRAL"
            if predicted_close > 0:
                self.predicted_btc_close = round(predicted_close, 2)
                if self.btc_open > 0:
                    if predicted_close > self.btc_open:
                        self.predicted_resolution = "YES"
                    elif predicted_close < self.btc_open:
                        self.predicted_resolution = "NO"
                    else:
                        self.predicted_resolution = "NEUTRAL"
            else:
                self.predicted_resolution = "NEUTRAL"
        self._dirty.set()

    def _load_executor_bankroll(self, default: float) -> float:
        try:
            data = json.loads(_EXECUTOR_BANKROLL_FILE.read_text())
            bankroll = float(data["bankroll"])
            self.executor_bankroll_original = float(data.get("original", bankroll))
            self.executor_all_time_trades   = int(data.get("trades_all_time", 0))
            return bankroll
        except Exception:
            return default

    def _save_executor_bankroll(self) -> None:
        if self._live_mode:
            return  # balance is owned by Kalshi; do not overwrite the paper file
        try:
            _EXECUTOR_BANKROLL_FILE.parent.mkdir(parents=True, exist_ok=True)
            _EXECUTOR_BANKROLL_FILE.write_text(json.dumps({
                "bankroll":        round(self.executor_bankroll, 2),
                "original":        round(self.executor_bankroll_original, 2),
                "trades_all_time": self.executor_all_time_trades,
            }))
        except Exception:
            pass

    async def open_position(
        self, ticker: str, side: str, contracts: int, fill_price: float, mode: str
    ) -> None:
        async with self._lock:
            self.executor_session_trades  += 1
            self.executor_all_time_trades += 1
            self.position = {
                "ticker":     ticker,
                "side":       side,
                "contracts":  contracts,
                "fill_price": round(fill_price, 1),
                "cost":       round(contracts * fill_price / 100.0, 2),
                "status":     "open",
                "mode":       mode,
                "pnl":        None,
            }
            self._save_executor_bankroll()
        self._dirty.set()

    async def settle_position(self, ticker: str, resolution: str) -> None:
        """Called by _settle_window with the official YES/NO resolution."""
        async with self._lock:
            pos = self.position
            if pos["status"] != "open" or pos["ticker"] != ticker:
                return
            won = pos["side"] == resolution
            pnl = round(pos["contracts"] - pos["cost"], 2) if won else round(-pos["cost"], 2)
            pos["status"] = "won" if won else "lost"
            pos["pnl"]    = pnl
            self.executor_session_pnl = round(self.executor_session_pnl + pnl, 2)
            if not self._live_mode:
                # Live: Kalshi owns the balance; the 30-s sync loop updates it.
                self.executor_bankroll = round(self.executor_bankroll + pnl, 2)
                self._save_executor_bankroll()
            # Clear the lock so maybe_trade can't fire a second buy after settlement
            self.final_model_locked   = False
            self.final_model_side     = None
            self.final_model_contract = None
            self.final_model_fv       = 50.0
            self.final_model_gap      = 0.0
        self._dirty.set()

    async def stop_position(self, ticker: str, sell_price: float) -> None:
        """Stop-loss: sell an open position at the current market price mid-window."""
        async with self._lock:
            pos = self.position
            if pos["status"] != "open" or pos["ticker"] != ticker:
                return
            proceeds = round(pos["contracts"] * sell_price / 100.0, 2)
            pnl      = round(proceeds - pos["cost"], 2)
            pos["status"] = "stopped"
            pos["pnl"]    = pnl
            self.executor_session_pnl = round(self.executor_session_pnl + pnl, 2)
            if not self._live_mode:
                # Live: Kalshi owns the balance; the 30-s sync loop updates it.
                self.executor_bankroll = round(self.executor_bankroll + pnl, 2)
                self._save_executor_bankroll()
        self._dirty.set()

    def lock_entry_prediction(self) -> None:
        if self.prediction_locked:
            return
        if self.predicted_direction == "NEUTRAL":
            return
        mid = self.orderbook.mid()
        if mid is not None:
            if self.predicted_direction == "UP" and mid < 30:
                self.prediction_locked_direction = "NEUTRAL"
                self.prediction_locked = True
                return
            if self.predicted_direction == "DOWN" and mid > 70:
                self.prediction_locked_direction = "NEUTRAL"
                self.prediction_locked = True
                return
        self.prediction_locked_direction = self.predicted_direction
        self.prediction_locked_yes_pct = self.prediction_yes_pct
        self.prediction_locked = True

    def lock_final_model_decision(self, side: Optional[str], fv: float = 50.0, gap: float = 0.0) -> None:
        """Lock the model's 8-min recommendation — retries each entry_open tick until side is non-None."""
        if self.final_model_locked:
            return
        if side is None:
            return  # keep retrying until a real signal appears
        self.final_model_side = side
        self.final_model_locked = True
        self.final_model_contract = self.active_contract
        self.final_model_fv = fv
        self.final_model_gap = gap

    async def update_open_interest(self, oi: float) -> None:
        async with self._lock:
            self.open_interest = oi
            self.oi_history.append((time.time(), oi))
        self._dirty.set()

    async def update_futures_taker_ratio(self, ratio: float) -> None:
        async with self._lock:
            self.futures_taker_ratio = ratio
            self.futures_taker_ratio_history.append((time.time(), ratio))
        self._dirty.set()

    async def update_perp_data(self, mid: float, index: float) -> None:
        async with self._lock:
            self.perp_mid   = mid
            self.perp_index = index
            self.perp_basis_history.append((time.time(), mid - index))
        self._dirty.set()

    def perp_basis_smoothed(self, window_s: float = 30.0) -> float:
        """Mean perp basis (mark − index, $) over the last window_s seconds."""
        history = list(self.perp_basis_history)
        if not history:
            return 0.0
        cutoff = history[-1][0] - window_s
        recent = [b for ts, b in history if ts >= cutoff]
        return sum(recent) / len(recent) if recent else 0.0

    def perp_basis_slope(self, window_s: float = 60.0) -> float:
        """$/s slope of the perp basis over the last window_s seconds.
        Rising basis → buyers bidding up the perp → incoming spot upward pressure.
        """
        history = list(self.perp_basis_history)
        if len(history) < 5:
            return 0.0
        cutoff = history[-1][0] - window_s
        recent = [(ts, b) for ts, b in history if ts >= cutoff]
        if len(recent) < 5:
            return 0.0
        dt = recent[-1][0] - recent[0][0]
        if dt < 10.0:
            return 0.0
        return (recent[-1][1] - recent[0][1]) / dt

    def oi_delta_pct(self, lookback_seconds: float) -> Optional[float]:
        history = list(self.oi_history)
        if len(history) < 2:
            return None
        now_ts = history[-1][0]
        cutoff = now_ts - lookback_seconds
        baseline = next((oi for ts, oi in history if ts >= cutoff), None)
        if baseline is None or baseline == 0.0:
            return None
        current = history[-1][1]
        return round((current - baseline) / baseline * 100.0, 3)

    async def update_binance_depth(self, imbalance: float) -> None:
        async with self._lock:
            self.binance_depth_imbalance = imbalance
            self.binance_depth_history.append((time.time(), imbalance))
        self._dirty.set()

    def binance_depth_smoothed(self) -> float:
        now_ts = time.time()
        recent = [v for ts, v in self.binance_depth_history if ts >= now_ts - 30.0]
        if not recent:
            return 0.0
        return round(sum(recent) / len(recent), 3)

    async def update_liquidation(self, side: str, usd_value: float) -> None:
        async with self._lock:
            now_ts = time.time()
            self.liq_history.append((now_ts, usd_value, side))
            cutoff = now_ts - 120.0
            self.liq_long_2m = sum(v for ts, v, s in self.liq_history if ts >= cutoff and s == "LONG")
            self.liq_short_2m = sum(v for ts, v, s in self.liq_history if ts >= cutoff and s == "SHORT")
        self._dirty.set()

    def liq_pressure(self) -> Optional[str]:
        from config import settings as _s
        if self.liq_long_2m > _s.liq_veto_threshold_usd:
            return "long_squeeze"
        if self.liq_short_2m > _s.liq_veto_threshold_usd:
            return "short_squeeze"
        return None

    async def update_dvol(self, dvol_pct: float) -> None:
        async with self._lock:
            self.dvol = round(dvol_pct, 1)
        self._dirty.set()

    async def update_market_sentiment(self, basis_pct: float, funding_pct: float) -> None:
        async with self._lock:
            self.futures_basis_pct = round(basis_pct, 4)
            self.funding_rate_pct = round(funding_pct, 4)
            self.sentiment_fetched = True
        self._dirty.set()

    async def update_cvd_trade(self, size: float, is_buy: bool) -> None:
        delta = size if is_buy else -size
        async with self._lock:
            self.cvd_window += delta
            self.cvd_total += size
            self.cvd_history.append((time.time(), delta))
        self._dirty.set()

    async def record_prediction_outcome(self, correct: bool) -> None:
        async with self._lock:
            self.session_pred_total += 1
            self.lifetime_pred_total += 1
            if correct:
                self.session_pred_correct += 1
                self.lifetime_pred_correct += 1
            _save_pred_stats(
                self.lifetime_pred_total, self.lifetime_pred_correct,
                self.lifetime_res_pred_total, self.lifetime_res_pred_correct,
            )
        self._dirty.set()

    async def record_resolution_prediction_outcome(self, correct: bool) -> None:
        async with self._lock:
            self.session_res_pred_total += 1
            self.lifetime_res_pred_total += 1
            if correct:
                self.session_res_pred_correct += 1
                self.lifetime_res_pred_correct += 1
            _save_pred_stats(
                self.lifetime_pred_total, self.lifetime_pred_correct,
                self.lifetime_res_pred_total, self.lifetime_res_pred_correct,
            )
        self._dirty.set()

    async def log_event(self, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        async with self._lock:
            self.event_log.appendleft(f"[{ts}] {msg}")
        self._dirty.set()

    async def set_last_resolution(self, msg: str) -> None:
        async with self._lock:
            self.last_resolution_msg = msg
            self.resolution_history.insert(0, msg)
            self.resolution_history = self.resolution_history[:100]
        _save_resolution_history(self.resolution_history)
        self._dirty.set()

    # ── Serialisation ─────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        from strategy.scalper import _btc_slope as _slope_fn
        now = time.time()
        ob = self.orderbook
        return {
            "ts": now,
            # BTC
            "btc_price": self.btc_price,
            "exchange_prices": dict(self._exchange_prices),
            "btc_history": list(self.btc_history)[-120:],
            "btc_feed_active": self.btc_feed_active,
            "btc_slope": round(_slope_fn(list(self.btc_history)), 3),
            # Contract / window
            "active_contract": self.active_contract,
            "window_close_ts": self.window_close_ts,
            "window_open_ts": self.window_open_ts,
            "window_seconds_left": max(0.0, self.window_close_ts - now),
            "btc_open": self.btc_open,
            "btc_change": round(self.btc_price - self.btc_open, 2) if self.btc_open > 0 else 0.0,
            "open_interest": self.open_interest,
            # Momentum / velocity
            "momentum_direction": self.momentum_direction,
            "velocity_pause": self.velocity_pause,
            # Orderbook
            "orderbook": {
                "best_bid": ob.best_bid(),
                "best_ask": ob.best_ask(),
                "mid": ob.mid(),
                "top_yes_bid": ob.top_yes_bid,
                "top_yes_ask": ob.top_yes_ask,
                "yes_bids": {str(k): v for k, v in sorted(ob.yes_bids.items(), reverse=True)},
                "yes_asks": {str(k): v for k, v in sorted(ob.yes_asks.items())},
            },
            "kalshi_feed_active": self.kalshi_feed_active,
            # Technicals
            "pre_window_bias": self.pre_window_bias,
            "technicals": {
                "rsi": self.tech_rsi,
                "adx": self.tech_adx,
                "bb_position": self.tech_bb_position,
                "bb_width": self.tech_bb_width,
                "fetched": self.tech_fetched,
            },
            # GBM prediction
            "prediction_yes_pct": self.prediction_yes_pct,
            "predicted_direction": self.predicted_direction,
            "predicted_btc_close": self.predicted_btc_close,
            "predicted_resolution": self.predicted_resolution,
            "prediction_locked_direction": self.prediction_locked_direction,
            "prediction_locked": self.prediction_locked,
            "final_model_side": self.final_model_side,
            "final_model_locked": self.final_model_locked,
            "final_model_fv": self.final_model_fv,
            "final_model_gap": self.final_model_gap,
            # External market data
            "dvol": self.dvol,
            "futures_basis_pct": self.futures_basis_pct,
            "funding_rate_pct": self.funding_rate_pct,
            "sentiment_fetched": self.sentiment_fetched,
            # CVD
            "cvd_window": round(self.cvd_window, 4),
            "cvd_total": round(self.cvd_total, 4),
            "cvd_ratio": round(self.cvd_window / self.cvd_total, 3) if self.cvd_total > 0 else 0.0,
            "cvd_5m": round(sum(d for ts, d in self.cvd_history if ts >= now - 300.0), 4),
            # New feeds
            "futures_taker_ratio": self.futures_taker_ratio,
            "futures_taker_ratio_history": list(self.futures_taker_ratio_history)[-12:],
            "perp_mid": round(self.perp_mid, 2),
            "perp_index": round(self.perp_index, 2),
            "perp_basis": round(self.perp_mid - self.perp_index, 2) if self.perp_mid > 0 else 0.0,
            "perp_basis_smoothed": round(self.perp_basis_smoothed(), 2),
            "perp_basis_slope": round(self.perp_basis_slope(), 3),
            "binance_depth_imbalance": self.binance_depth_imbalance,
            "binance_depth_smoothed": self.binance_depth_smoothed(),
            "liq_long_2m": round(self.liq_long_2m, 0),
            "liq_short_2m": round(self.liq_short_2m, 0),
            "liq_pressure": self.liq_pressure(),
            # Analysis conditions
            "analysis": dict(self.analysis),
            # Recommendation
            "recommendation": dict(self.recommendation),
            # Prediction accuracy
            "lifetime_pred_total": self.lifetime_pred_total,
            "lifetime_pred_correct": self.lifetime_pred_correct,
            "lifetime_pred_accuracy": self._pred_accuracy(lifetime=True),
            "session_pred_total": self.session_pred_total,
            "session_pred_correct": self.session_pred_correct,
            "session_pred_accuracy": self._pred_accuracy(lifetime=False),
            # Resolution prediction accuracy (slope-based)
            "lifetime_res_pred_total": self.lifetime_res_pred_total,
            "lifetime_res_pred_correct": self.lifetime_res_pred_correct,
            "lifetime_res_pred_accuracy": self._res_pred_accuracy(lifetime=True),
            "session_res_pred_total": self.session_res_pred_total,
            "session_res_pred_correct": self.session_res_pred_correct,
            "session_res_pred_accuracy": self._res_pred_accuracy(lifetime=False),
            # Session
            "trading_mode": self.trading_mode,
            "session_start_ts": self.session_start_ts,
            "last_resolution_msg": self.last_resolution_msg,
            # Log
            "event_log": list(self.event_log)[:50],
            # Executor position + P&L
            "position":                  dict(self.position),
            "executor_bankroll":          round(self.executor_bankroll, 2),
            "executor_bankroll_original": round(self.executor_bankroll_original, 2),
            "executor_session_pnl":      round(self.executor_session_pnl, 2),
            "executor_session_trades":   self.executor_session_trades,
            "executor_all_time_trades":  self.executor_all_time_trades,
            # Persistent resolution history
            "resolution_history":        self.resolution_history[:50],
        }

    def _pred_accuracy(self, lifetime: bool = True) -> float:
        total = self.lifetime_pred_total if lifetime else self.session_pred_total
        correct = self.lifetime_pred_correct if lifetime else self.session_pred_correct
        if total == 0:
            return 0.0
        return round(correct / total, 3)

    def _res_pred_accuracy(self, lifetime: bool = True) -> float:
        total = self.lifetime_res_pred_total if lifetime else self.session_res_pred_total
        correct = self.lifetime_res_pred_correct if lifetime else self.session_res_pred_correct
        if total == 0:
            return 0.0
        return round(correct / total, 3)

    # ── Broadcast loop ─────────────────────────────────────────────────────────

    async def broadcast_loop(self) -> None:
        while True:
            await self._dirty.wait()
            self._dirty.clear()
            await self._broadcast_all()

    async def _broadcast_all(self) -> None:
        if not self._connections:
            return
        payload = json.dumps(self.to_dict())
        dead: set[WebSocket] = set()
        for ws in list(self._connections):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.add(ws)
        self._connections -= dead


def _nearest_price(history: list, target_ts: float) -> Optional[float]:
    best_price = None
    best_diff = float("inf")
    for ts, price in history:
        diff = abs(ts - target_ts)
        if diff < best_diff:
            best_diff = diff
            best_price = price
    return best_price if best_diff <= 5.0 else None
