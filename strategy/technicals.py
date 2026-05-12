"""
Pre-window BTC technical analysis using Binance 1-min OHLCV candles (no auth required).

Fetches the last 50 candles and computes RSI(14), ADX(14), and Bollinger Bands(20)
to generate a directional bias before each Kalshi 15-min window.

Bias logic (mirrors manual strategy from the video):
  - RSI < 40  → oversold  → bullish point
  - RSI > 60  → overbought → bearish point
  - BB position < 0.4 (near lower band) → bullish point
  - BB position > 0.6 (near upper band) → bearish point
  - Majority of points → bias direction; tie → neutral
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import httpx

_BINANCE_KLINES = "https://api.binance.com/api/v3/klines"


@dataclass
class TechnicalBias:
    rsi: float          # 0–100  (< 40 bullish, > 60 bearish)
    adx: float          # 0–100  (> 25 trending, < 20 choppy — user likes 20s)
    bb_position: float  # 0 = at lower band, 1 = at upper band
    bb_width: float     # (upper − lower) / sma  — wider = more volatile
    bias: str           # "up" | "down" | "neutral"


async def fetch_bias(
    symbol: str = "BTCUSDT",
    interval: str = "1m",
    limit: int = 50,
) -> Optional[TechnicalBias]:
    """Fetch Binance klines and compute directional bias. Returns None on any error."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                _BINANCE_KLINES,
                params={"symbol": symbol, "interval": interval, "limit": limit},
            )
            resp.raise_for_status()
            candles = resp.json()
    except Exception:
        return None

    if len(candles) < 30:
        return None

    highs  = [float(c[2]) for c in candles]
    lows   = [float(c[3]) for c in candles]
    closes = [float(c[4]) for c in candles]

    rsi             = _rsi(closes)
    adx             = _adx(highs, lows, closes)
    bb_pos, bb_wid  = _bb_position(closes)
    bias            = _classify(rsi, bb_pos)

    return TechnicalBias(
        rsi=round(rsi, 1),
        adx=round(adx, 1),
        bb_position=round(bb_pos, 3),
        bb_width=round(bb_wid, 4),
        bias=bias,
    )


# ── Indicator calculations ────────────────────────────────────────────────────

def _classify(rsi: float, bb_pos: float) -> str:
    bull = (1 if rsi < 40 else 0) + (1 if bb_pos < 0.4 else 0)
    bear = (1 if rsi > 60 else 0) + (1 if bb_pos > 0.6 else 0)
    if bull > bear:
        return "up"
    if bear > bull:
        return "down"
    return "neutral"


def _rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(0.0, d))
        losses.append(max(0.0, -d))
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0.0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_g / avg_l)


def _adx(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int = 14,
) -> float:
    n = len(closes)
    if n < period * 2 + 1:
        return 25.0

    trs, pdms, ndms = [], [], []
    for i in range(1, n):
        tr   = max(highs[i] - lows[i],
                   abs(highs[i] - closes[i - 1]),
                   abs(lows[i]  - closes[i - 1]))
        up   = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        trs.append(tr)
        pdms.append(up   if up > down and up > 0   else 0.0)
        ndms.append(down if down > up and down > 0 else 0.0)

    def _wilder(vals: list[float]) -> list[float]:
        out = [sum(vals[:period])]
        for v in vals[period:]:
            out.append(out[-1] - out[-1] / period + v)
        return out

    str_ = _wilder(trs)
    spdm = _wilder(pdms)
    sndm = _wilder(ndms)

    dxs = []
    for i in range(len(str_)):
        pdi   = 100.0 * spdm[i] / str_[i] if str_[i] > 0 else 0.0
        ndi   = 100.0 * sndm[i] / str_[i] if str_[i] > 0 else 0.0
        denom = pdi + ndi
        dxs.append(100.0 * abs(pdi - ndi) / denom if denom > 0 else 0.0)

    if len(dxs) < period:
        return 25.0

    adx = sum(dxs[:period]) / period
    for dx in dxs[period:]:
        adx = (adx * (period - 1) + dx) / period
    return adx


def _bb_position(closes: list[float], period: int = 20) -> tuple[float, float]:
    """Returns (position 0–1, band_width) where 0=lower band, 1=upper band."""
    if len(closes) < period:
        return 0.5, 0.0
    recent = closes[-period:]
    sma    = sum(recent) / period
    std    = math.sqrt(sum((p - sma) ** 2 for p in recent) / period)
    if std == 0.0:
        return 0.5, 0.0
    upper = sma + 2.0 * std
    lower = sma - 2.0 * std
    width = (upper - lower) / sma if sma > 0 else 0.0
    pos   = (closes[-1] - lower) / (upper - lower)
    return max(0.0, min(1.0, pos)), width
