"""
Momentum scalper for KXBTC15M.

Signal logic:
  - Monitor the first ~7 min of the 15-min window (no entries until ≤ MAX_ENTRY_WINDOW_S remain).
  - After monitoring, buy the side (YES/NO) that matches the sustained BTC direction.
  - Entry price must be in the confirmed-winner zone (MIN_ENTRY_PRICE_CENTS–MAX_ENTRY_PRICE_CENTS: 60–85¢).
  - BTC must have moved at least MOMENTUM_ENTRY_USD from the window open.
  - GBM fair-value model must broadly agree with the BTC direction (fv > 50 for YES, < 50 for NO).
  - Exits handled by the trader: +TAKE_PROFIT_CENTS take-profit, -STOP_LOSS_CENTS stop loss,
    and a hard FORCE_EXIT_TAU_S close of all positions before resolution.

Every 50 ms the scalper also:
  - Computes the GBM fair-value probability (YES wins %) — shown as the model prediction.
  - Extrapolates the current BTC velocity to estimate the price at window close.
"""
from __future__ import annotations

import asyncio
import math
import time
import uuid
from typing import Optional

from config import Settings
from logger.event_logger import EventLogger
from state.state_manager import Orderbook, Signal, StateManager

_YEAR_SECONDS = 365.25 * 24 * 3600


def _norm_cdf(x: float) -> float:
    return math.erfc(-x / math.sqrt(2.0)) / 2.0


def _rolling_realized_vol(
    history: list[tuple[float, float]],
    fallback: float,
    lookback_s: float = 600.0,
) -> float:
    """Annualized realized vol from recent BTC price history, clamped to [0.20, 2.50]."""
    if not history:
        return fallback
    cutoff = history[-1][0] - lookback_s
    recent = [(ts, p) for ts, p in history if ts >= cutoff]
    if len(recent) < 10:
        return fallback
    sq_sum = sum(
        math.log(recent[i][1] / recent[i - 1][1]) ** 2
        for i in range(1, len(recent))
        if recent[i - 1][1] > 0
    )
    span_years = (recent[-1][0] - recent[0][0]) / _YEAR_SECONDS
    if span_years <= 0:
        return fallback
    return max(0.20, min(2.50, math.sqrt(sq_sum / span_years)))


def fair_value_yes_cents(
    btc: float,
    btc_open: float,
    tau_seconds: float,
    sigma: float,
) -> float:
    """GBM probability that BTC closes at or above the window open (0–100 cents)."""
    if btc_open <= 0:
        return 50.0
    btc_change = btc - btc_open
    if tau_seconds <= 0.0:
        return 100.0 if btc_change >= 0 else 0.0
    btc_change_pct = btc_change / btc_open
    expected_vol_pct = sigma * math.sqrt(tau_seconds / _YEAR_SECONDS)
    if expected_vol_pct <= 0:
        return 100.0 if btc_change >= 0 else 0.0
    z = btc_change_pct / expected_vol_pct
    return max(5.0, min(95.0, _norm_cdf(z) * 100.0))


def _predict_btc_close(
    history: list[tuple[float, float]],
    current_price: float,
    tau_seconds: float,
) -> float:
    """Estimate BTC price at window close via a blend of medium and long-term velocity.

    Uses two lookback windows — 90s and 300s — and weights the longer one more
    heavily so that short-term noise doesn't whipsaw the forecast.  The result
    is clamped to ±$500 of current price so spikes can't produce absurd numbers.
    """
    if not history or tau_seconds <= 0:
        return current_price
    now = history[-1][0]

    def _slope(secs: float) -> float | None:
        pts = [(ts, p) for ts, p in history if ts >= now - secs]
        if len(pts) < 5:
            return None
        dt = pts[-1][0] - pts[0][0]
        return (pts[-1][1] - pts[0][1]) / dt if dt > 0 else None

    s90  = _slope(90.0)
    s300 = _slope(300.0)

    if s90 is None and s300 is None:
        return current_price
    if s90 is None:
        slope = s300
    elif s300 is None:
        slope = s90
    else:
        # Weight longer window 2:1 to dampen short-term noise
        slope = (s90 + s300 * 2.0) / 3.0

    raw = current_price + slope * tau_seconds
    return max(current_price - 500.0, min(current_price + 500.0, raw))


class Scalper:
    def __init__(self, state: StateManager, cfg: Settings, logger: EventLogger):
        self.state = state
        self.cfg = cfg
        self.logger = logger
        self._last_signal_ts: float = 0.0
        self._last_blocked_ts: dict[str, float] = {}

    async def run(self) -> None:
        await asyncio.gather(
            self._evaluation_loop(),
            self._bias_refresher(),
        )

    async def _evaluation_loop(self) -> None:
        while True:
            if not self.state.kill_switch:
                await self._evaluate()
            await asyncio.sleep(0.05)

    async def _bias_refresher(self) -> None:
        """Fetch Binance technical indicators once per minute and update state."""
        while True:
            await self._refresh_bias()
            await asyncio.sleep(60.0)

    async def _refresh_bias(self) -> None:
        from strategy.technicals import fetch_bias
        bias = await fetch_bias(
            symbol=self.cfg.binance_symbol,
            interval=self.cfg.binance_klines_interval,
        )
        if bias is None:
            return
        await self.state.update_technicals(
            bias.rsi, bias.adx, bias.bb_position, bias.bb_width, bias.bias
        )
        await self.logger.log("technicals", {
            "rsi": bias.rsi,
            "adx": bias.adx,
            "bb_pos": bias.bb_position,
            "bb_width": bias.bb_width,
            "bias": bias.bias,
        })

    async def _evaluate(self) -> Optional[Signal]:
        btc: float = self.state.btc_price
        btc_open: float = self.state.btc_open
        contract: Optional[str] = self.state.active_contract
        window_close: float = self.state.window_close_ts
        ob: Orderbook = self.state.orderbook

        if not contract or btc <= 0 or btc_open <= 0:
            return None

        now = time.time()
        tau_seconds = max(0.0, window_close - now)
        history = list(self.state.btc_history)

        # ── Always update the prediction panel ───────────────────────────────
        sigma = _rolling_realized_vol(history, fallback=self.cfg.btc_sigma)
        fv = fair_value_yes_cents(btc, btc_open, tau_seconds, sigma)
        predicted_close = _predict_btc_close(history, btc, tau_seconds)
        await self.state.update_prediction(fv, predicted_close)

        # ── Trading gates ─────────────────────────────────────────────────────
        if len(self.state.open_positions) >= self.cfg.max_concurrent_positions:
            return None
        if self.state.velocity_pause:
            return None
        if now - self.state.window_discovered_ts < self.cfg.new_window_settle_s:
            return None
        if self.state.open_interest < self.cfg.min_open_interest:
            await self._log_blocked("thin_market", oi=self.state.open_interest, min_oi=self.cfg.min_open_interest)
            return None

        # ── One-and-done: sit out after a winning trade this window ───────────
        if self.cfg.one_and_done and self.state.window_won:
            return None

        mid = ob.mid()
        if mid is None:
            return None

        # ── BTC direction ─────────────────────────────────────────────────────
        btc_change = btc - btc_open
        side = "yes" if btc_change > 0 else "no"

        yes_ask = ob.best_ask() or 0.0
        no_ask = (100.0 - ob.best_bid()) if ob.best_bid() is not None else 0.0
        entry_price = yes_ask if side == "yes" else no_ask

        # ── "Away from the line" checks — replicating the human edge ─────────
        #
        # The manual strategy watches two things on the Kalshi contract price:
        #   1. How many times has it crossed 50¢ during monitoring?
        #      Few crossings = price committed to one side = good signal.
        #   2. Is it consistently moving FURTHER from 50¢, or oscillating?
        #      Steady march away = "slow and methodical" = tradeable.
        #      Whipsawing = skip.

        monitoring_start = self.state.window_discovered_ts + self.cfg.new_window_settle_s
        monitoring_mids = [(ts, m) for ts, m in self.state.kalshi_mid_history
                           if ts >= monitoring_start]

        if len(monitoring_mids) >= 10:
            # 1. Line crossing count: how many times did mid cross 50¢?
            crossings = sum(
                1 for i in range(1, len(monitoring_mids))
                if (monitoring_mids[i][1] - 50.0) * (monitoring_mids[i - 1][1] - 50.0) < 0
            )
            if crossings > self.cfg.max_line_crossings:
                await self._log_blocked("price_too_choppy", crossings=crossings,
                                        max=self.cfg.max_line_crossings)
                return None

            # 2. Direction consistency over the last 2 min: what fraction of
            #    30-second steps moved the mid FURTHER from 50¢?
            recent_mids = [(ts, m) for ts, m in monitoring_mids if ts >= now - 120.0]
            if len(recent_mids) >= 6:
                step = len(recent_mids) // 6
                steps_away = 0
                for i in range(5):
                    m0 = recent_mids[i * step][1]
                    m1 = recent_mids[(i + 1) * step][1]
                    # Moving further from 50¢ in the trade direction?
                    if side == "yes" and m1 > m0:
                        steps_away += 1
                    elif side == "no" and m1 < m0:
                        steps_away += 1
                consistency = steps_away / 5.0
                if consistency < self.cfg.min_direction_consistency:
                    await self._log_blocked("direction_inconsistent",
                                            consistency=round(consistency, 2),
                                            min=self.cfg.min_direction_consistency)
                    return None

        # ── Slow-market filter: block entries during erratic price swings ────
        recent_mids_60 = [(ts, m) for ts, m in self.state.kalshi_mid_history
                          if ts >= now - 60.0]
        if len(recent_mids_60) >= 5:
            mid_range = max(m for _, m in recent_mids_60) - min(m for _, m in recent_mids_60)
            if mid_range > self.cfg.kalshi_mid_max_range_cents:
                await self._log_blocked("market_too_erratic", range_cents=round(mid_range, 1))
                return None

        # ── Pre-window bias gate: skip if technicals contradict direction ─────
        if self.cfg.bias_gate_enabled:
            bias_dir = self.state.pre_window_bias
            if side == "yes" and bias_dir == "down":
                await self._log_blocked("bias_disagrees", side="yes", bias=bias_dir,
                                        rsi=round(self.state.tech_rsi, 1))
                return None
            if side == "no" and bias_dir == "up":
                await self._log_blocked("bias_disagrees", side="no", bias=bias_dir,
                                        rsi=round(self.state.tech_rsi, 1))
                return None

        # ── Single entry window: after monitoring phase ───────────────────────
        #
        # No entries for the first ~7 min (while BTC direction is forming).
        # Only trade when [min_entry_window_s, max_entry_window_s] seconds remain.

        if not (self.cfg.min_entry_window_s <= tau_seconds <= self.cfg.max_entry_window_s):
            return None

        # BTC must show clear directional momentum from the monitoring period
        if abs(btc_change) < self.cfg.momentum_entry_usd:
            await self._log_blocked(
                "momentum_insufficient",
                move=round(abs(btc_change), 2),
                required=self.cfg.momentum_entry_usd,
            )
            return None

        # Entry price must be in the confirmed-winner range (buy low, sell high)
        if not (self.cfg.min_entry_price_cents <= entry_price <= self.cfg.max_entry_price_cents):
            await self._log_blocked("entry_price_out_of_range", price=round(entry_price, 1))
            return None

        # GBM model must broadly agree with BTC direction
        if side == "yes" and fv <= 50.0:
            await self._log_blocked("model_disagrees", side="yes", fv=round(fv, 1))
            return None
        if side == "no" and fv >= 50.0:
            await self._log_blocked("model_disagrees", side="no", fv=round(fv, 1))
            return None

        # ── Debounce ──────────────────────────────────────────────────────────
        if now - self._last_signal_ts < self.cfg.signal_debounce_s:
            return None

        # ── One position per direction per contract ───────────────────────────
        if any(p.market_ticker == contract and p.side == side for p in self.state.open_positions):
            return None

        # ── Confidence ────────────────────────────────────────────────────────
        move_score = min(1.0, abs(btc_change) / (self.cfg.momentum_entry_usd * 3.0))
        depth = sum(ob.yes_bids.values()) + sum(ob.yes_asks.values())
        depth_score = min(1.0, depth / 500.0)
        confidence = move_score * 0.7 + depth_score * 0.3

        if confidence < self.cfg.confidence_threshold:
            await self._log_blocked("confidence_below_threshold", conf=round(confidence, 3), threshold=self.cfg.confidence_threshold)
            return None

        self._last_signal_ts = now
        gap_pct = abs(fv - mid) / 100.0
        sig = Signal(
            id=uuid.uuid4().hex[:8],
            timestamp=now,
            market_ticker=contract,
            side=side,
            btc_price=btc,
            kalshi_mid=mid,
            fair_value=round(fv, 2),
            gap_pct=round(gap_pct, 4),
            confidence=round(confidence, 3),
            yes_ask=round(yes_ask, 1),
            no_ask=round(no_ask, 1),
        )
        await self.state.add_signal(sig)
        await self.logger.log("signal", {
            "id": sig.id,
            "side": side,
            "btc": f"{btc:.2f}",
            "btc_open": f"{btc_open:.2f}",
            "btc_change": f"{btc_change:+.2f}",
            "entry_price": f"{entry_price:.1f}",
            "fv": f"{fv:.2f}",
            "mid": f"{mid:.2f}",
            "conf": f"{confidence:.3f}",
            "sigma": f"{sigma:.3f}",
            "predicted_close": f"{predicted_close:.2f}",
            "tau_s": f"{tau_seconds:.0f}",
        })
        return sig

    async def _log_blocked(self, reason: str, **data) -> None:
        now = time.time()
        if now - self._last_blocked_ts.get(reason, 0.0) < 10.0:
            return
        self._last_blocked_ts[reason] = now
        await self.logger.log("signal_blocked", {"reason": reason, **data})
