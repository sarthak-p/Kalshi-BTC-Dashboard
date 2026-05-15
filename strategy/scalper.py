"""
Market analyzer — computes GBM fair-value, technicals, and a trade recommendation
every tick. No orders are placed; this is a decision-support tool.

Recommendation logic (3-signal vote):
  1. GBM model  — fv > 55 → YES lean, fv < 45 → NO lean
  2. BTC move   — |change| > MOMENTUM_ENTRY_USD and direction → bullish/bearish
  3. Tech bias  — pre-window RSI/BB bias: up/down/neutral

2 or 3 signals agreeing (with at most 1 opposing) → recommend that side + best ask price.
CVD is treated as neutral when it diverges from price direction (absorption signal).
Also logs market resolution + model accuracy at each window close.
"""
from __future__ import annotations

import asyncio
import datetime
import math
import time
from typing import Optional

from config import Settings
from logger.event_logger import EventLogger
from state.state_manager import Orderbook, StateManager

_YEAR_SECONDS = 365.25 * 24 * 3600


def _norm_cdf(x: float) -> float:
    return math.erfc(-x / math.sqrt(2.0)) / 2.0


def _rolling_realized_vol(
    history: list[tuple[float, float]],
    fallback: float,
    lookback_s: float = 600.0,
) -> float:
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
    drift_usd_per_s: float = 0.0,
) -> float:
    """GBM probability that BTC closes at or above the window open (0–100 cents).

    drift_usd_per_s: current BTC velocity in $/s (positive = rising). Shifts the
    z-score so the model accounts for where price is heading, not just where it is.
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
    drift_pct = (drift_usd_per_s / btc_open) * tau_seconds
    z = (btc_change_pct + drift_pct) / expected_vol_pct
    return max(5.0, min(95.0, _norm_cdf(z) * 100.0))


def _btc_slope(history: list[tuple[float, float]]) -> float:
    """Weighted blend of 90-s and 300-s BTC slopes in $/sec. Returns 0 if insufficient data.

    Requires at least 60 s of actual history span before returning a non-zero slope so
    that session startup and post-velocity-pause noise don't pollute the drift term.
    """
    if not history:
        return 0.0
    now = history[-1][0]
    history_span = now - history[0][0]
    if history_span < 60.0:
        return 0.0

    def _slope(secs: float) -> float | None:
        pts = [(ts, p) for ts, p in history if ts >= now - secs]
        if len(pts) < 5:
            return None
        dt = pts[-1][0] - pts[0][0]
        if dt < 30.0:
            return None
        return (pts[-1][1] - pts[0][1]) / dt

    s90  = _slope(90.0)
    s300 = _slope(300.0)

    if s90 is None and s300 is None:
        return 0.0
    if s90 is None:
        return s300
    if s300 is None:
        return s90
    return (s90 + s300 * 2.0) / 3.0


def _predict_btc_close(
    history: list[tuple[float, float]],
    current_price: float,
    tau_seconds: float,
) -> float:
    if not history or tau_seconds <= 0:
        return current_price
    slope = _btc_slope(history)
    if slope == 0.0:
        return current_price
    raw = current_price + slope * tau_seconds
    return max(current_price - 500.0, min(current_price + 500.0, raw))

def _cvd_signal(cvd_window: float, cvd_total: float, btc_change: float = 0.0) -> Optional[str]:
    """
    YES/NO based on CVD direction and whether it confirms or diverges from price.
    
    Confirmed momentum: CVD and price agree → strong signal.
    Absorption: CVD and price diverge → contrarian signal (large participant absorbing flow).
    """
    if cvd_total < 2.0:
        return None
    ratio = cvd_window / cvd_total

    # Confirmed momentum
    if ratio > 0.15 and btc_change >= 0:
        return "YES"   # buying + rising = confirmed bull momentum
    if ratio < -0.15 and btc_change <= 0:
        return "NO"    # selling + falling = confirmed bear momentum

    # Absorption signals (divergence = large participant in control)
    if ratio < -0.15 and btc_change > 0:
        return "YES"   # selling but price rising = absorption, strong buyer
    if ratio > 0.15 and btc_change < 0:
        return "NO"    # buying but price falling = distribution, strong seller

    return None


def _compute_recommendation(
    fv: float,
    btc_change: float,
    bias: str,
    ob: Orderbook,
    momentum_usd: float,
    drift_usd_per_s: float = 0.0,
    cvd_window: float = 0.0,
    cvd_total: float = 0.0,
    funding_pct: float = 0.0,
    kalshi_mid_history: list = None,
    tau_seconds: float = 0.0,
    min_commitment_rate: float = 0.20,
    min_gbm_market_gap_cents: float = 8.0,
    min_entry_price_cents: float = 8.0,
    max_entry_price_cents: float = 65.0,
    min_slope_usd_per_s: float = 0.30,
) -> dict:
    basis = []
    edge_gate_blocked = False

    # ── Primary signals: GBM, slope, technicals ───────────────────────────────

    # GBM fair-value
    if fv > 55:
        model_side = "YES"
        basis.append(f"GBM: {fv:.0f}% → UP")
    elif fv < 45:
        model_side = "NO"
        basis.append(f"GBM: {fv:.0f}% → DOWN")
    else:
        model_side = None
        basis.append(f"GBM: {fv:.0f}% (neutral)")

    # BTC slope — pre-window momentum, best early-window signal
    if drift_usd_per_s >= min_slope_usd_per_s:
        slope_side = "YES"
        basis.append(f"Slope: +${drift_usd_per_s:.2f}/s (uptrend)")
    elif drift_usd_per_s <= -min_slope_usd_per_s:
        slope_side = "NO"
        basis.append(f"Slope: ${drift_usd_per_s:.2f}/s (downtrend)")
    else:
        slope_side = None
        basis.append(f"Slope: ${drift_usd_per_s:+.2f}/s (flat)")

    # Technicals (RSI/BB/ADX)
    # Technicals are only meaningful when GBM is uncertain (20–80%).
    # Outside that range BTC is already far from the strike — GBM encodes
    # that gap directly, and a general RSI/BB bounce signal is irrelevant.
    if 20.0 < fv < 80.0 and bias == "up":
        bias_side = "YES"
        basis.append("Technicals: bullish (RSI/BB)")
    elif 20.0 < fv < 80.0 and bias == "down":
        bias_side = "NO"
        basis.append("Technicals: bearish (RSI/BB)")
    elif bias in ("up", "down"):
        bias_side = None
        basis.append(f"Technicals: {bias} — ignored (GBM {fv:.0f}% already decisive)")
    else:
        bias_side = None

    # Decision hierarchy:
    #   1. GBM has a signal → GBM drives; technicals are informational only
    #   2. GBM neutral, slope has a signal → slope drives; technicals are informational only
    #   3. Both neutral → no recommendation
    # Technicals never veto — they had veto power before, but the 81% accuracy
    # was derived from a small in-sample reversal and likely overfit.
    if model_side is not None:
        side = model_side
        if bias_side is not None and bias_side != model_side:
            basis.append(f"Technicals: {bias} (conflicts with GBM — informational)")
        elif bias_side is None:
            basis.append("Technicals: neutral (GBM driving)")
    elif slope_side is not None:
        side = slope_side
        if bias_side is not None and bias_side != slope_side:
            basis.append(f"Technicals: {bias} (conflicts with slope — informational)")
        elif bias_side is None:
            basis.append("Technicals: neutral (slope driving)")
        else:
            basis.append(f"Technicals: {bias} confirms slope")
    else:
        side = None
        if bias_side is not None:
            basis.append(f"Technicals: {bias} — GBM+slope neutral")
        else:
            basis.append("Technicals: neutral")

    # Entry price for the recommended side
    if side == "YES":
        entry_price = ob.best_ask()
    elif side == "NO":
        bb = ob.best_bid()
        entry_price = (100.0 - bb) if bb is not None else None
    else:
        entry_price = None

    # ── Supporting signals (informational — shown in dashboard) ───────────────
    if btc_change >= momentum_usd * 1.1:
        btc_side = "YES"
        basis.append(f"BTC: +${btc_change:.0f} bullish")
    elif btc_change <= -momentum_usd * 1.1:
        btc_side = "NO"
        basis.append(f"BTC: -${abs(btc_change):.0f} bearish")
    else:
        btc_side = None
        basis.append(f"BTC: ${btc_change:+.0f} (< ${momentum_usd * 1.1:.0f} threshold)")

    cvd_side = _cvd_signal(cvd_window, cvd_total, btc_change)
    if cvd_side == "YES":
        ratio_pct = round(cvd_window / cvd_total * 100) if cvd_total > 0 else 0
        basis.append(f"CVD: +{abs(ratio_pct)}% net buying")
    elif cvd_side == "NO":
        ratio_pct = round(abs(cvd_window / cvd_total) * 100) if cvd_total > 0 else 0
        basis.append(f"CVD: -{ratio_pct}% net selling")
    else:
        basis.append("CVD: neutral / insufficient volume")

    if funding_pct > 0.01:
        funding_side = "NO"
        basis.append(f"Funding: +{funding_pct:.4f}% (crowded longs)")
    elif funding_pct < -0.01:
        funding_side = "YES"
        basis.append(f"Funding: {funding_pct:.4f}% (crowded shorts)")
    else:
        funding_side = None
        basis.append(f"Funding: {funding_pct:.4f}% (neutral)")

    imb = ob.imbalance()
    if imb is not None and imb > 0.20:
        imbalance_side = "YES"
        basis.append(f"OB imbalance: {imb:+.2f} (bid-heavy)")
    elif imb is not None and imb < -0.20:
        imbalance_side = "NO"
        basis.append(f"OB imbalance: {imb:+.2f} (ask-heavy)")
    else:
        imbalance_side = None
        basis.append(f"OB imbalance: {f'{imb:+.2f}' if imb is not None else 'n/a'} (neutral)")

    kalshi_momentum_side = None
    if kalshi_mid_history and len(kalshi_mid_history) >= 10:
        now_ts = kalshi_mid_history[-1][0]
        recent = [(ts, m) for ts, m in kalshi_mid_history if ts >= now_ts - 300.0]
        if len(recent) >= 5:
            slope = (recent[-1][1] - recent[0][1]) / max(recent[-1][0] - recent[0][0], 1.0)
            if slope > 0.05:
                kalshi_momentum_side = "YES"
                basis.append(f"Kalshi mid: rising ({slope:+.3f}¢/s)")
            elif slope < -0.05:
                kalshi_momentum_side = "NO"
                basis.append(f"Kalshi mid: falling ({slope:+.3f}¢/s)")
            else:
                basis.append(f"Kalshi mid: flat ({slope:+.3f}¢/s)")

    # Count supporting signals that agree with the recommended side (informational)
    supporting = [btc_side, cvd_side, funding_side, imbalance_side, kalshi_momentum_side, slope_side]
    if side is not None:
        agree_count = sum(1 for s in supporting if s == side)
        disagree_count = sum(1 for s in supporting if s is not None and s != side)
        if agree_count or disagree_count:
            basis.append(f"Supporting: {agree_count} confirm, {disagree_count} oppose")
    else:
        agree_count = 0

    # ── Warnings (informational — don't block recommendation) ─────────────────
    if side is not None and tau_seconds > 30.0:
        rate = abs(btc_change) / tau_seconds
        if rate < min_commitment_rate:
            basis.append(
                f"⚠ Low commitment: ${abs(btc_change):.0f} over {tau_seconds:.0f}s "
                f"({rate:.2f}$/s)"
            )

    if side is not None:
        mid_price = ob.mid()
        if mid_price is not None:
            gap = fv - mid_price
            gap_val = gap if side == "YES" else -gap
            if gap_val < min_gbm_market_gap_cents:
                basis.append(
                    f"⚠ Small edge: GBM {fv:.0f}¢ vs market {mid_price:.0f}¢ "
                    f"(gap {gap_val:+.1f}¢)"
                )

    # ── Hard gate: only block near-zero entries (model has no useful edge) ──────
    if side is not None and entry_price is not None:
        if entry_price < min_entry_price_cents:
            basis.append(
                f"⛔ Entry {entry_price:.1f}¢ below floor {min_entry_price_cents:.0f}¢ — poor risk/reward"
            )
            side = None
            entry_price = None
            edge_gate_blocked = True

    return {
        "side": side,
        "entry_price": round(entry_price, 1) if entry_price is not None else None,
        "confidence": round((2 + agree_count) / 7.0, 2) if side else 0.0,
        "signal_count": agree_count,
        "basis": basis,
        "edge_gate_blocked": edge_gate_blocked,
    }

class Analyzer:
    def __init__(self, state: StateManager, cfg: Settings, logger: EventLogger, executor=None):
        self.state    = state
        self.cfg      = cfg
        self.logger   = logger
        self.executor = executor

    async def run(self) -> None:
        await asyncio.gather(
            self._analysis_loop(),
            self._bias_refresher(),
            self._window_resolver(),
        )

    # ── Analysis loop (every 50 ms) ───────────────────────────────────────────

    async def _analysis_loop(self) -> None:
        while True:
            await self._analyze()
            await asyncio.sleep(0.05)

    async def _analyze(self) -> None:
        btc: float = self.state.btc_price
        btc_open: float = self.state.btc_open
        ob: Orderbook = self.state.orderbook

        if not self.state.active_contract or btc <= 0 or btc_open <= 0:
            self.state.analysis["phase"] = "waiting"
            return

        now = time.time()
        tau_seconds = max(0.0, self.state.window_close_ts - now)
        history = list(self.state.btc_history)

        if self.state.dvol > 0:
            sigma = self.state.dvol / 100.0   # Deribit DVOL is more stable than realized vol
        else:
            sigma = _rolling_realized_vol(history, fallback=self.cfg.btc_sigma)
        slope = _btc_slope(history)
        fv = fair_value_yes_cents(btc, btc_open, tau_seconds, sigma, drift_usd_per_s=slope)
        predicted_close = _predict_btc_close(history, btc, tau_seconds)
        await self.state.update_prediction(fv, predicted_close)

        # Phase
        if tau_seconds > self.cfg.max_entry_window_s:
            phase = "monitoring"
        elif tau_seconds >= self.cfg.min_entry_window_s:
            phase = "entry_open"
            self.state.lock_entry_prediction()   # freeze on first entry_open tick
        elif tau_seconds > 30.0:
            phase = "too_late"
        else:
            phase = "closing"

        btc_change = btc - btc_open
        side = "yes" if btc_change > 0 else "no"

        yes_ask = ob.best_ask() or 0.0
        no_ask = (100.0 - ob.best_bid()) if ob.best_bid() is not None else 0.0
        entry_price = yes_ask if side == "yes" else no_ask
        price_in_range = (
            self.cfg.min_entry_price_cents <= entry_price <= self.cfg.max_entry_price_cents
        ) if entry_price > 0 else False

        # Monitoring-window checks (line crossings, direction consistency)
        monitoring_start = self.state.window_discovered_ts + self.cfg.new_window_settle_s
        monitoring_mids = [(ts, m) for ts, m in self.state.kalshi_mid_history
                           if ts >= monitoring_start]

        line_crossings = None
        crossings_ok = None
        direction_score = None
        direction_ok = None

        if len(monitoring_mids) >= 10:
            crossings = sum(
                1 for i in range(1, len(monitoring_mids))
                if (monitoring_mids[i][1] - 50.0) * (monitoring_mids[i - 1][1] - 50.0) < 0
            )
            line_crossings = crossings
            crossings_ok = crossings <= self.cfg.max_line_crossings

            mid = ob.mid()
            if mid is not None and abs(mid - 50.0) < 20.0:
                recent_mids = [(ts, m) for ts, m in monitoring_mids if ts >= now - 120.0]
                if len(recent_mids) >= 6:
                    step = len(recent_mids) // 6
                    steps_away = 0
                    for i in range(5):
                        m0 = recent_mids[i * step][1]
                        m1 = recent_mids[(i + 1) * step][1]
                        if side == "yes" and m1 > m0:
                            steps_away += 1
                        elif side == "no" and m1 < m0:
                            steps_away += 1
                    direction_score = round(steps_away / 5.0, 2)
                    direction_ok = direction_score >= self.cfg.min_direction_consistency
            else:
                direction_ok = True  # price deeply committed, check skipped

        # Write analysis conditions (direct write — sole writer)
        self.state.analysis.update({
            "phase": phase,
            "side": side,
            "btc_move_ok": abs(btc_change) >= self.cfg.momentum_entry_usd,
            "price_in_range": price_in_range,
            "entry_price": round(entry_price, 1) if entry_price > 0 else None,
            "line_crossings": line_crossings,
            "crossings_ok": crossings_ok,
            "direction_score": direction_score,
            "direction_ok": direction_ok,
        })

        # Recommendation (direct write)
        self.state.recommendation = _compute_recommendation(
            fv=fv,
            btc_change=btc_change,
            bias=self.state.pre_window_bias,
            ob=ob,
            momentum_usd=self.cfg.momentum_entry_usd,
            drift_usd_per_s=slope,
            cvd_window=self.state.cvd_window,
            cvd_total=self.state.cvd_total,
            funding_pct=self.state.funding_rate_pct,
            kalshi_mid_history=list(self.state.kalshi_mid_history),
            tau_seconds=tau_seconds,
            min_commitment_rate=self.cfg.min_commitment_rate,
            min_gbm_market_gap_cents=self.cfg.min_gbm_market_gap_cents,
            min_entry_price_cents=self.cfg.min_entry_price_cents,
            max_entry_price_cents=self.cfg.max_entry_price_cents,
            min_slope_usd_per_s=self.cfg.btc_slope_signal_threshold,
        )

        if self.state.recommendation["side"] != getattr(self.state, '_last_logged_rec_side', 'UNSET'):
            self.state._last_logged_rec_side = self.state.recommendation["side"]
            await self.logger.log("recommendation", {
                "side": self.state.recommendation["side"],
                "entry_price": self.state.recommendation["entry_price"],
                "signal_count": self.state.recommendation["signal_count"],
                "basis": self.state.recommendation["basis"],
            })

        now_ts = time.time()
        locked = getattr(self.state, 'recommendation_locked_side', None)
        lock_ts = getattr(self.state, 'recommendation_lock_ts', 0.0)
        current_side = self.state.recommendation["side"]

        if self.state.active_contract != getattr(self.state, '_last_locked_contract', None):
            self.state.recommendation_locked_side = None
            self.state.recommendation_lock_ts = 0.0
            self.state._last_locked_contract = self.state.active_contract
            locked = None

        # Break the 60-second flip lock early when GBM crosses the stop-loss threshold —
        # same ≤35%/≥65% boundary used by the executor so display and execution stay aligned.
        gbm_strongly_opposes = (
            (locked == "YES" and fv <= 35.0) or
            (locked == "NO"  and fv >= 65.0)
        )

        if current_side is not None:
            if locked is None:
                self.state.recommendation_locked_side = current_side
                self.state.recommendation_lock_ts = now_ts
            elif current_side != locked:
                if now_ts - lock_ts >= 60.0 or gbm_strongly_opposes:
                    self.state.recommendation_locked_side = current_side
                    self.state.recommendation_lock_ts = now_ts
                else:
                    self.state.recommendation["side"] = locked
                    self.state.recommendation["basis"].append(
                        f"⚠ Flip suppressed — locked {locked} for {now_ts - lock_ts:.0f}s"
                    )
        elif current_side is None and locked is not None:
            edge_blocked = self.state.recommendation.get("edge_gate_blocked", False)
            if now_ts - lock_ts < 60.0 and not gbm_strongly_opposes and not edge_blocked:
                self.state.recommendation["side"] = locked
                self.state.recommendation["basis"].append(
                    f"⚠ Flip suppressed — locked {locked} for {now_ts - lock_ts:.0f}s"
                )

        if self.state.recommendation["side"] != getattr(self.state, '_last_logged_rec_side', 'UNSET'):
            self.state._last_logged_rec_side = self.state.recommendation["side"]
            await self.logger.log("recommendation", {
                "side": self.state.recommendation["side"],
                "entry_price": self.state.recommendation["entry_price"],
                "signal_count": self.state.recommendation["signal_count"],
                "basis": self.state.recommendation["basis"],
            })

        if self.executor:
            await self.executor.maybe_trade()

        self.state._dirty.set()

    # ── Technicals refresh (every 60 s) ───────────────────────────────────────

    async def _bias_refresher(self) -> None:
        while True:
            await self._refresh_bias()
            await asyncio.sleep(15.0)

    async def _refresh_bias(self) -> None:
        from strategy.technicals import fetch_bias, fetch_market_sentiment
        # Only update RSI/BB/ADX bias between windows — mid-window crashes or bounces
        # would otherwise silently flip pre_window_bias and change the executor's decision.
        if not self.state.active_contract:
            bias = await fetch_bias(
                symbol=self.cfg.binance_symbol,
                interval=self.cfg.binance_klines_interval,
                limit=35,
                min_adx=self.cfg.min_adx_threshold,
            )
            if bias is not None:
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

        sentiment = await fetch_market_sentiment()
        if sentiment is not None:
            if sentiment.dvol > 0:
                await self.state.update_dvol(sentiment.dvol)
            await self.state.update_market_sentiment(sentiment.basis_pct, sentiment.funding_pct)
            await self.logger.log("market_sentiment", {
                "dvol": sentiment.dvol,
                "basis_pct": sentiment.basis_pct,
                "funding_pct": sentiment.funding_pct,
            })

    # ── Window resolver (every 1 s) ───────────────────────────────────────────

    async def _window_resolver(self) -> None:
        """At each window close: snapshot state and hand off to _settle_window."""
        seen_open: set[str] = set()
        resolved: set[str] = set()
        while True:
            await asyncio.sleep(1.0)
            contract = self.state.active_contract
            close_ts = self.state.window_close_ts
            if not contract or close_ts <= 0:
                continue
            now = time.time()
            if now < close_ts:
                seen_open.add(contract)
                continue
            if contract not in seen_open or contract in resolved:
                continue
            resolved.add(contract)

            # Snapshot everything now — state will mutate as the next window opens.
            asyncio.ensure_future(self._settle_window(
                ticker=contract,
                btc_at_close=self.state.btc_price,
                btc_open=self.state.btc_open,
                predicted_dir=self.state.prediction_locked_direction,
                prediction_yes_pct=self.state.prediction_yes_pct,
                pre_window_bias=self.state.pre_window_bias,
                bias_at_discovery=self.state.bias_at_discovery,
                predicted_resolution=self.state.predicted_resolution,
                tech_adx=self.state.tech_adx,
            ))

    async def _settle_window(
        self,
        ticker: str,
        btc_at_close: float,
        btc_open: float,
        predicted_dir: str,
        prediction_yes_pct: float,
        pre_window_bias: str,
        bias_at_discovery: str = "neutral",
        predicted_resolution: str = "NEUTRAL",
        tech_adx: float = 0.0,
    ) -> None:
        """
        Poll Kalshi's settlement API for the official result.

        Kalshi resolves using CF Benchmarks' BRTI (not Coinbase spot), so we
        must query the API to get an accurate outcome. settlement_timer_seconds=1
        means the result is usually available within seconds of close; we poll
        up to 2 minutes before falling back to a Coinbase-price estimate.
        """
        from feeds.kalshi_ws import fetch_kalshi_settlement

        kalshi_result: Optional[str] = None
        for _ in range(24):  # 24 × 5 s = 2 minutes
            kalshi_result = await fetch_kalshi_settlement(ticker, self.cfg)
            if kalshi_result is not None:
                break
            await asyncio.sleep(5.0)

        if kalshi_result is not None:
            resolved_yes: Optional[bool] = kalshi_result == "yes"
            resolution = kalshi_result.upper()
            result_source = "Kalshi"
        else:
            resolved_yes = btc_at_close >= btc_open if btc_open > 0 else None
            resolution = "YES" if resolved_yes else "NO" if resolved_yes is not None else "?"
            result_source = "estimated"

        # Settle any open executor position for this window, then re-sync balance
        if resolution in ("YES", "NO"):
            await self.state.settle_position(ticker, resolution)
            if self.executor:
                await self.executor.sync_balance()

        btc_chg = btc_at_close - btc_open if btc_open > 0 else 0.0
        chg_sign = "+" if btc_chg >= 0 else ""

        prediction_correct: Optional[bool] = None
        if resolved_yes is not None and predicted_dir != "NEUTRAL":
            prediction_correct = (predicted_dir == "UP") == resolved_yes

        resolution_pred_correct: Optional[bool] = None
        if resolved_yes is not None and predicted_resolution != "NEUTRAL":
            resolution_pred_correct = (predicted_resolution == "YES") == resolved_yes

        pred_label = ""
        if prediction_correct is not None:
            pred_label = f"  model={predicted_dir} [{'CORRECT' if prediction_correct else 'WRONG'}]"
        if resolution_pred_correct is not None:
            pred_label += f"  slope={'CORRECT' if resolution_pred_correct else 'WRONG'}"

        resolution_msg = (
            f"{ticker}  BTC {btc_at_close:.2f}  "
            f"({chg_sign}{btc_chg:.2f})  → {resolution} [{result_source}]{pred_label}"
        )
        await self.state.log_event(f"Window closed: {resolution_msg}")
        await self.state.set_last_resolution(resolution_msg)

        await self.logger.log("market_resolved", {
            "ticker": ticker,
            "btc_open": round(btc_open, 2) if btc_open > 0 else None,
            "btc_close": round(btc_at_close, 2),
            "btc_change": round(btc_chg, 2),
            "resolution": resolution,
            "result_source": result_source,
            "predicted_direction": predicted_dir,
            "prediction_yes_pct": round(prediction_yes_pct, 1),
            "pre_window_bias": pre_window_bias,
            "prediction_correct": prediction_correct,
            "predicted_resolution": predicted_resolution,
            "resolution_pred_correct": resolution_pred_correct,
            "adx": round(tech_adx, 1),
        })

        # ── Technicals discovery-time log ─────────────────────────────────────────
        if resolution in ("YES", "NO"):
            import csv as _csv, os as _os
            _path = _os.path.join("logs", "technicals_discovery.csv")
            _write_header = not _os.path.exists(_path)
            with open(_path, "a", newline="") as _f:
                _w = _csv.writer(_f)
                if _write_header:
                    _w.writerow(["date_utc", "ticker", "bias_at_discovery", "resolution"])
                import datetime as _dt
                _w.writerow([
                    _dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
                    ticker,
                    bias_at_discovery,
                    resolution,
                ])

        # ── CSV log ───────────────────────────────────────────────────────────────
        self.logger.log_prediction({
            "session_ts":              int(self.state.session_start_ts),
            "date_utc":                datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
            "ticker":                  ticker,
            "btc_open":                round(btc_open, 2) if btc_open > 0 else "",
            "btc_close":               round(btc_at_close, 2),
            "btc_change":              round(btc_chg, 2),
            "resolution":              resolution,
            "result_source":           result_source,
            "predicted_direction":     predicted_dir,
            "prediction_yes_pct":      round(prediction_yes_pct, 1),
            "pre_window_bias":         pre_window_bias,
            "prediction_correct":      prediction_correct,
            "predicted_resolution":    predicted_resolution,
            "resolution_pred_correct": resolution_pred_correct,
            "adx":                     round(tech_adx, 1),
        })

        # ── Accuracy tracking ─────────────────────────────────────────────────────
        if prediction_correct is not None:
            await self.state.record_prediction_outcome(prediction_correct)
        if resolution_pred_correct is not None:
            await self.state.record_resolution_prediction_outcome(resolution_pred_correct)