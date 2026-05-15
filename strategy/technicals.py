"""
Pre-window BTC technical analysis using Coinbase Exchange 1-min OHLCV candles (no auth required).

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

import logging
import math
from dataclasses import dataclass
from typing import Optional

import httpx

# Coinbase Exchange public candle API (no auth, US-accessible)
# Format: [time, low, high, open, close, volume]  — newest candle first
_COINBASE_CANDLES = "https://api.exchange.coinbase.com/products/{symbol}/candles"

_DERIBIT_DVOL = "https://www.deribit.com/api/v2/public/get_volatility_index_data"
_OKX_FUNDING  = "https://www.okx.com/api/v5/public/funding-rate"

_log = logging.getLogger(__name__)


@dataclass
class MarketSentiment:
    dvol: float          # Deribit DVOL annualized % (e.g. 55.2)
    basis_pct: float     # (futures − spot) / spot × 100
    funding_pct: float   # Binance funding rate as % (e.g. 0.01)


@dataclass
class TechnicalBias:
    rsi: float          # 0–100  (< 40 bullish, > 60 bearish)
    adx: float          # 0–100  (> 25 trending, < 20 choppy — user likes 20s)
    bb_position: float  # 0 = at lower band, 1 = at upper band
    bb_width: float     # (upper − lower) / sma  — wider = more volatile
    bias: str           # "up" | "down" | "neutral"


async def fetch_bias(
    symbol: str = "BTC-USD",
    interval: str = "60",
    limit: int = 50,
    min_adx: float = 15.0,
) -> Optional[TechnicalBias]:
    """Fetch Coinbase Exchange candles and compute directional bias. Returns None on any error."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                _COINBASE_CANDLES.format(symbol=symbol),
                params={"granularity": int(interval)},
            )
            resp.raise_for_status()
            candles = resp.json()
    except Exception as exc:
        _log.warning("fetch_bias: Coinbase candle fetch failed — %s: %s", type(exc).__name__, exc)
        return None

    # Coinbase returns newest-first; reverse to get chronological order, then take last `limit`
    candles = list(reversed(candles))[-limit:]

    if len(candles) < 30:
        return None

    # Coinbase candle format: [time, low, high, open, close, volume]
    highs  = [float(c[2]) for c in candles]
    lows   = [float(c[1]) for c in candles]
    closes = [float(c[4]) for c in candles]

    rsi             = _rsi(closes)
    adx             = _adx(highs, lows, closes)
    bb_pos, bb_wid  = _bb_position(closes)
    bias            = _classify(rsi, bb_pos, adx, min_adx=min_adx)
    return TechnicalBias(
        rsi=round(rsi, 1),
        adx=round(adx, 1),
        bb_position=round(bb_pos, 3),
        bb_width=round(bb_wid, 4),
        bias=bias,
    )


async def fetch_market_sentiment() -> Optional[MarketSentiment]:
    """Fetch Deribit DVOL and OKX perp basis + funding. Returns None only if both fail."""
    dvol      = await _fetch_dvol()
    sentiment = await _fetch_okx_sentiment()
    if dvol is None and sentiment is None:
        return None
    return MarketSentiment(
        dvol=dvol or 0.0,
        basis_pct=sentiment[0] if sentiment else 0.0,
        funding_pct=sentiment[1] if sentiment else 0.0,
    )


async def _fetch_dvol() -> Optional[float]:
    """Returns the latest Deribit BTC DVOL value (annualized %, e.g. 55.2)."""
    import time as _time
    now_ms   = int(_time.time() * 1000)
    start_ms = now_ms - 3_600_000  # 1 hour back
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                _DERIBIT_DVOL,
                params={
                    "currency": "BTC",
                    "resolution": "3600",
                    "start_timestamp": start_ms,
                    "end_timestamp": now_ms,
                },
            )
            resp.raise_for_status()
            data = resp.json().get("result", {}).get("data", [])
            if data:
                return float(data[-1][4])  # close of latest hourly candle
    except Exception as exc:
        _log.warning("_fetch_dvol: %s: %s", type(exc).__name__, exc)
    return None


async def _fetch_okx_sentiment() -> Optional[tuple[float, float]]:
    """Returns (basis_pct, funding_rate_pct) from OKX BTC-USDT-SWAP. US-accessible."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(_OKX_FUNDING, params={"instId": "BTC-USDT-SWAP"})
            resp.raise_for_status()
            data = resp.json().get("data", [])
            if not data:
                return None
            d = data[0]
            funding_pct = float(d["fundingRate"]) * 100.0
            basis_pct   = float(d["premium"]) * 100.0   # (markPrice − indexPrice) / indexPrice
            return basis_pct, funding_pct
    except Exception as exc:
        _log.warning("_fetch_okx_sentiment: %s: %s", type(exc).__name__, exc)
    return None


# ── Indicator calculations ────────────────────────────────────────────────────

def _classify(rsi: float, bb_pos: float, adx: float = 25.0, min_adx: float = 15.0) -> str:
    if adx < min_adx:
        return "neutral"   # ranging market — RSI/BB signals not reliable below this ADX
    # Momentum-following (live data: oversold = continuation DOWN 80%, overbought = continuation UP 58%)
    # RSI < 40 + near lower BB → strong downtrend → expect continuation DOWN
    # RSI > 60 + near upper BB → strong uptrend → expect continuation UP
    bull = (1 if rsi > 60 else 0) + (1 if bb_pos > 0.6 else 0)
    bear = (1 if rsi < 40 else 0) + (1 if bb_pos < 0.4 else 0)
    if bull >= 1 and bull > bear:
        return "up"
    if bear >= 1 and bear > bull:
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
