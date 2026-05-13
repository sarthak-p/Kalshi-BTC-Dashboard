from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from fastapi import WebSocket

_STATS_FILE = Path("logs/lifetime_stats.json")


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
    last_seq: int = 0
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


class StateManager:
    def __init__(self, momentum_threshold_usd: float = 150.0):
        # Feed state
        self.btc_price: float = 0.0
        self.btc_history: deque[tuple[float, float]] = deque(maxlen=300)
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
        self.prediction_locked: bool = False

        # External market data
        self.dvol: float = 0.0                   # Deribit DVOL index (annualized %)
        self.futures_basis_pct: float = 0.0      # (futures − spot) / spot × 100
        self.funding_rate_pct: float = 0.0       # Binance perp funding rate (%)
        self.sentiment_fetched: bool = False

        # CVD (Cumulative Volume Delta) from Coinbase trade stream
        self.cvd_window: float = 0.0             # net buy BTC volume since window open
        self.cvd_total: float = 0.0              # total BTC volume since window open
        self.cvd_history: deque[tuple[float, float]] = deque(maxlen=5000)  # (ts, delta)

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
            "bias_ok": None,
        }

        # Recommendation (written directly by analyzer)
        self.recommendation: dict = {
            "side": None,        # "YES" | "NO" | None
            "entry_price": None,
            "confidence": 0.0,
            "signal_count": 0,   # how many of 3 indicators agree
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

    async def update_btc(self, price: float) -> None:
        async with self._lock:
            ts = time.time()
            self.btc_price = price
            self.btc_history.append((ts, price))
            self.btc_feed_active = True
            if self.btc_open == 0.0 and self.active_contract:
                self.btc_open = price
            self._update_momentum_velocity(ts, price)
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
            self.prediction_locked = False
        self._dirty.set()

    async def set_btc_open(self, price: float) -> None:
        async with self._lock:
            self.btc_open = price
        self._dirty.set()

    async def update_technicals(
        self, rsi: float, adx: float, bb_position: float, bb_width: float, bias: str
    ) -> None:
        async with self._lock:
            self.tech_rsi = rsi
            self.tech_adx = adx
            self.tech_bb_position = bb_position
            self.tech_bb_width = bb_width
            self.pre_window_bias = bias
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

    def lock_entry_prediction(self) -> None:
        """Freeze predicted_direction the first time the window enters entry_open phase."""
        if not self.prediction_locked and self.predicted_direction != "NEUTRAL":
            self.prediction_locked_direction = self.predicted_direction
            self.prediction_locked = True

    async def update_open_interest(self, oi: float) -> None:
        async with self._lock:
            self.open_interest = oi
        self._dirty.set()

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
        self._dirty.set()

    # ── Serialisation ─────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        now = time.time()
        ob = self.orderbook
        return {
            "ts": now,
            # BTC
            "btc_price": self.btc_price,
            "btc_history": list(self.btc_history)[-120:],
            "btc_feed_active": self.btc_feed_active,
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
            "session_start_ts": self.session_start_ts,
            "last_resolution_msg": self.last_resolution_msg,
            # Log
            "event_log": list(self.event_log)[:50],
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
