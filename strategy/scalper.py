"""
Market analyzer — computes GBM fair-value, technicals, and a trade recommendation
every tick. No orders are placed; this is a decision-support tool.

Recommendation logic (3-signal vote):
  1. GBM model  — fv > 55 → YES lean, fv < 45 → NO lean
  2. BTC move   — |change| > MOMENTUM_ENTRY_USD and direction → bullish/bearish
  3. Tech bias  — pre-window RSI/BB bias: up/down/neutral

2 or 3 signals agreeing → recommend that side + best ask price.
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
        slope = (s90 + s300 * 2.0) / 3.0

    raw = current_price + slope * tau_seconds
    return max(current_price - 500.0, min(current_price + 500.0, raw))


def _compute_recommendation(
    fv: float,
    btc_change: float,
    bias: str,
    ob: Orderbook,
    momentum_usd: float,
) -> dict:
    basis = []

    if fv > 55:
        model_side = "YES"
        basis.append(f"GBM: {fv:.0f}% → UP")
    elif fv < 45:
        model_side = "NO"
        basis.append(f"GBM: {fv:.0f}% → DOWN")
    else:
        model_side = None
        basis.append(f"GBM: {fv:.0f}% (neutral)")

    if btc_change >= momentum_usd:
        btc_side = "YES"
        basis.append(f"BTC: +${btc_change:.0f} bullish")
    elif btc_change <= -momentum_usd:
        btc_side = "NO"
        basis.append(f"BTC: -${abs(btc_change):.0f} bearish")
    else:
        btc_side = None
        basis.append(f"BTC: ${btc_change:+.0f} (< ${momentum_usd:.0f} threshold)")

    if bias == "up":
        bias_side = "YES"
        basis.append("Technicals: bullish (RSI/BB)")
    elif bias == "down":
        bias_side = "NO"
        basis.append("Technicals: bearish (RSI/BB)")
    else:
        bias_side = None
        basis.append("Technicals: neutral")

    signals = [model_side, btc_side, bias_side]
    yes_count = signals.count("YES")
    no_count  = signals.count("NO")

    if yes_count >= 2 and yes_count > no_count:
        side = "YES"
        entry_price = ob.best_ask()
    elif no_count >= 2 and no_count > yes_count:
        side = "NO"
        bb = ob.best_bid()
        entry_price = (100.0 - bb) if bb is not None else None
    else:
        side = None
        entry_price = None

    return {
        "side": side,
        "entry_price": round(entry_price, 1) if entry_price is not None else None,
        "confidence": round(max(yes_count, no_count) / 3.0, 2),
        "signal_count": max(yes_count, no_count),
        "basis": basis,
    }


class Analyzer:
    def __init__(self, state: StateManager, cfg: Settings, logger: EventLogger):
        self.state = state
        self.cfg = cfg
        self.logger = logger

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

        sigma = _rolling_realized_vol(history, fallback=self.cfg.btc_sigma)
        fv = fair_value_yes_cents(btc, btc_open, tau_seconds, sigma)
        predicted_close = _predict_btc_close(history, btc, tau_seconds)
        await self.state.update_prediction(fv, predicted_close)

        # Phase
        if tau_seconds > self.cfg.max_entry_window_s:
            phase = "monitoring"
        elif tau_seconds >= self.cfg.min_entry_window_s:
            phase = "entry_open"
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

        # Bias check
        bias_dir = self.state.pre_window_bias
        if bias_dir == "neutral":
            bias_ok = None
        elif (side == "yes" and bias_dir == "up") or (side == "no" and bias_dir == "down"):
            bias_ok = True
        else:
            bias_ok = False

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
            "bias_ok": bias_ok,
        })

        # Recommendation (direct write)
        self.state.recommendation = _compute_recommendation(
            fv=fv,
            btc_change=btc_change,
            bias=bias_dir,
            ob=ob,
            momentum_usd=self.cfg.momentum_entry_usd,
        )
        self.state._dirty.set()

    # ── Technicals refresh (every 60 s) ───────────────────────────────────────

    async def _bias_refresher(self) -> None:
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

    # ── Window resolver (every 1 s) ───────────────────────────────────────────

    async def _window_resolver(self) -> None:
        """At each window close: log actual resolution vs model prediction."""
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

            btc_at_close = self.state.btc_price
            btc_open = self.state.btc_open
            resolved_yes = btc_at_close >= btc_open if btc_open > 0 else None

            resolution = "YES" if resolved_yes else "NO" if resolved_yes is not None else "?"
            btc_chg = btc_at_close - btc_open if btc_open > 0 else 0.0
            chg_sign = "+" if btc_chg >= 0 else ""

            predicted_dir = self.state.predicted_direction
            prediction_yes_pct = self.state.prediction_yes_pct
            pre_window_bias = self.state.pre_window_bias

            prediction_correct: Optional[bool] = None
            if resolved_yes is not None and predicted_dir != "NEUTRAL":
                prediction_correct = (predicted_dir == "UP") == resolved_yes

            pred_label = ""
            if prediction_correct is not None:
                pred_label = f"  model={predicted_dir} [{'CORRECT' if prediction_correct else 'WRONG'}]"

            resolution_msg = (
                f"{contract}  BTC {btc_at_close:.2f}  "
                f"({chg_sign}{btc_chg:.2f})  → {resolution}{pred_label}"
            )
            await self.state.log_event(f"Window closed: {resolution_msg}")
            await self.state.set_last_resolution(resolution_msg)

            await self.logger.log("market_resolved", {
                "ticker": contract,
                "btc_open": round(btc_open, 2) if btc_open > 0 else None,
                "btc_close": round(btc_at_close, 2),
                "btc_change": round(btc_chg, 2),
                "resolution": resolution,
                "predicted_direction": predicted_dir,
                "prediction_yes_pct": round(prediction_yes_pct, 1),
                "pre_window_bias": pre_window_bias,
                "prediction_correct": prediction_correct,
            })

            self.logger.log_prediction({
                "session_ts": int(self.state.session_start_ts),
                "date_utc": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
                "ticker": contract,
                "floor_strike": round(btc_open, 2) if btc_open > 0 else "",
                "btc_open": round(btc_open, 2) if btc_open > 0 else "",
                "btc_close": round(btc_at_close, 2),
                "btc_change": round(btc_chg, 2),
                "resolution": resolution,
                "predicted_direction": predicted_dir,
                "prediction_yes_pct": round(prediction_yes_pct, 1),
                "pre_window_bias": pre_window_bias,
                "prediction_correct": prediction_correct,
            })

            if prediction_correct is not None:
                await self.state.record_prediction_outcome(prediction_correct)
