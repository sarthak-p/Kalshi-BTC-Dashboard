"""
Signal generation.

Model: momentum-based fair value for KXBTC15M (up/down binary).
  KXBTC15M resolves YES if BTC at window close >= BTC at window open.
  There is no strike price. Fair value depends on:
    - btc_change: how much BTC has moved since the window opened
    - expected_vol: 1-sigma expected BTC move over the remaining window time
      (= btc_open * annual_sigma * sqrt(tau_seconds / YEAR_SECONDS))

  fair_value_yes = clamp(0.50 + btc_change / expected_vol, 0.05, 0.95) * 100 cents
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
    """Standard normal CDF via math.erfc — no scipy dependency."""
    return math.erfc(-x / math.sqrt(2.0)) / 2.0


def _rolling_realized_vol(
    history: list[tuple[float, float]],
    fallback: float,
    lookback_s: float = 600.0,
) -> float:
    """Annualized realized vol from recent BTC price history.

    Uses the sum-of-squared log returns estimator over the available lookback
    window.  Falls back to `fallback` (the static BTC_SIGMA config value) when
    there is not enough history to produce a reliable estimate.

    Clamped to [0.20, 2.50] to prevent the fair-value model from behaving
    pathologically on data spikes or extremely quiet markets.
    """
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
    vol = math.sqrt(sq_sum / span_years)
    return max(0.20, min(2.50, vol))


def fair_value_yes_cents(
    btc: float,
    btc_open: float,
    tau_seconds: float,
    sigma: float,
) -> float:
    """YES fair value in Kalshi cents (0-100) for the up/down binary.

    Uses a z-score approach so the ratio stays well-behaved at all time horizons:
      z = (btc_change / btc_open) / (sigma * sqrt(tau / year))
      fair_value = norm_cdf(z)
    """
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
    fv = _norm_cdf(z)
    return max(5.0, min(95.0, fv * 100.0))


class Scalper:
    def __init__(
        self,
        state: StateManager,
        cfg: Settings,
        logger: EventLogger,
    ):
        self.state = state
        self.cfg = cfg
        self.logger = logger
        self._last_signal_ts: float = 0.0
        self._last_blocked_ts: dict[str, float] = {}

    async def run(self) -> None:
        while True:
            if not self.state.kill_switch:
                await self._evaluate()
            await asyncio.sleep(0.05)  # 50 ms poll; eval itself is <1 ms

    # ── Hot path ─────────────────────────────────────────────────────────────

    async def _evaluate(self) -> Optional[Signal]:
        # ── Capacity: skip entirely when all position slots are full ─────────
        if len(self.state.open_positions) >= self.cfg.max_concurrent_positions:
            return None

        # ── Velocity pause: price too unstable for fair value model ──────────
        if self.state.velocity_pause:
            return None

        btc: float = self.state.btc_price
        btc_open: float = self.state.btc_open
        contract: Optional[str] = self.state.active_contract
        window_close: float = self.state.window_close_ts
        ob: Orderbook = self.state.orderbook

        if not contract or btc <= 0 or btc_open <= 0:
            return None

        now = time.time()

        # ── New-window settle: block until feeds stabilise after contract rollover ─
        if now - self.state.window_discovered_ts < self.cfg.new_window_settle_s:
            return None

        # ── Thin-market filter ───────────────────────────────────────────────
        if self.state.open_interest < self.cfg.min_open_interest:
            await self._log_blocked(
                "thin_market",
                oi=self.state.open_interest,
                min_oi=self.cfg.min_open_interest,
            )
            return None

        tau_seconds = max(0.0, window_close - now)
        mid = ob.mid()
        if mid is None:
            return None

        sigma = _rolling_realized_vol(
            list(self.state.btc_history),
            fallback=self.cfg.btc_sigma,
        )
        fv = fair_value_yes_cents(btc, btc_open, tau_seconds, sigma)
        gap_abs = fv - mid          # + → YES underpriced; − → NO underpriced
        gap_pct = abs(gap_abs) / 100.0

        # Hard block — log at most once per 10 s per reason to avoid spam
        if gap_pct < self.cfg.signal_threshold:
            await self._log_blocked(
                "gap_below_threshold",
                gap=round(gap_pct, 4),
                threshold=self.cfg.signal_threshold,
            )
            return None

        side = "yes" if gap_abs > 0 else "no"

        # ── Block entries too close to expiry ────────────────────────────────
        # Edge comes from Kalshi lagging BTC repricing early in the window.
        # With < 4 min left the market has already converged — no structural edge.
        if tau_seconds < self.cfg.min_entry_window_s:
            return None

        # ── Entry price range ─────────────────────────────────────────────────
        yes_ask = ob.best_ask() or 0.0
        no_ask = (100.0 - ob.best_bid()) if ob.best_bid() is not None else 0.0
        entry_price = yes_ask if side == "yes" else no_ask

        # Compute is_fade before price check — fade entries bypass the minimum
        # price floor (NO costs only 15-30¢ when YES has spiked to 72¢+, which
        # is precisely when the fade trade has the best risk/reward).
        is_fade = (side == "no" and yes_ask >= self.cfg.fade_extreme_cents) or \
                  (side == "yes" and yes_ask <= (100.0 - self.cfg.fade_extreme_cents))

        min_price = 0.0 if is_fade else self.cfg.min_entry_price_cents
        if not (min_price <= entry_price <= self.cfg.max_entry_price_cents):
            return None

        # Signal debounce
        if now - self._last_signal_ts < self.cfg.signal_debounce_s:
            return None

        # ── One position per direction per contract — no martingaling ─────────
        if any(p.market_ticker == contract and p.side == side
               for p in self.state.open_positions):
            return None

        # ── Directional drift guard ───────────────────────────────────────────
        # Block chasing an adverse move (YES when BTC already fell, NO when already rose).
        # Do NOT block fading an extreme price — that's the other side of the same coin.
        if not is_fade and btc_open > 0 and self.cfg.max_adverse_drift_pct > 0:
            drift = (btc - btc_open) / btc_open
            if side == "yes" and drift < -self.cfg.max_adverse_drift_pct:
                return None
            if side == "no" and drift > self.cfg.max_adverse_drift_pct:
                return None

        # ── Momentum filter ───────────────────────────────────────────────────
        # Skip for fade entries — when YES has spiked to 72¢+ the model correctly
        # says buy NO even though BTC momentum is up. That IS the trade.
        if not is_fade:
            momentum = self.state.momentum_direction
            if momentum == "up" and side == "no":
                return None
            if momentum == "down" and side == "yes":
                return None

        confidence = _calc_confidence(gap_pct, tau_seconds, ob, self.cfg.signal_threshold)
        # Hard block — log at most once per 10 s per reason
        if confidence < self.cfg.confidence_threshold:
            await self._log_blocked(
                "confidence_below_threshold",
                conf=round(confidence, 3),
                threshold=self.cfg.confidence_threshold,
            )
            return None

        self._last_signal_ts = now
        btc_change = round(btc - btc_open, 2)
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
            "fv": f"{fv:.2f}",
            "mid": f"{mid:.2f}",
            "gap_pct": f"{gap_pct:.3f}",
            "conf": f"{confidence:.3f}",
            "sigma": f"{sigma:.3f}",
        })
        return sig


    async def _log_blocked(self, reason: str, **data) -> None:
        """Log a signal_blocked event, debounced to once per 10 s per reason."""
        now = time.time()
        if now - self._last_blocked_ts.get(reason, 0.0) < 10.0:
            return
        self._last_blocked_ts[reason] = now
        await self.logger.log("signal_blocked", {"reason": reason, **data})


# ── Confidence heuristic ─────────────────────────────────────────────────────

def _calc_confidence(
    gap_pct: float,
    tau_seconds: float,
    ob: Orderbook,
    threshold: float,
) -> float:
    # Gap: how much does fair value exceed the entry price?
    gap_score = min(1.0, gap_pct / (threshold * 3.0))

    # Depth: is there enough liquidity to actually fill?
    depth = sum(ob.yes_bids.values()) + sum(ob.yes_asks.values())
    depth_score = min(1.0, depth / 500.0)

    # No time_score — with early-window-only entries, time is already filtered
    # upstream (tau > min_entry_window_s). Adding it here penalised the exact
    # entries we want (high tau = early = low old time_score).
    return gap_score * 0.7 + depth_score * 0.3
