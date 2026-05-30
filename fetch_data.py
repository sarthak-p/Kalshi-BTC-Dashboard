"""
Fetches 60 days of KXBTC15M 1-minute candlestick data from Kalshi
and saves to logs/candle_cache.json.

Run once — subsequent analysis scripts load from cache instantly.
Saves progress every 100 windows so Ctrl+C is safe.

Usage: python fetch_data.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse

import httpx

sys.path.insert(0, "/Users/sarthak-p/Desktop/kalshi-btc-bot")
from config import Settings
from feeds.kalshi_ws import _make_rest_headers

SERIES      = "KXBTC15M"
DAYS        = 180
CACHE_FILE  = Path("/Users/sarthak-p/Desktop/kalshi-btc-bot/logs/candle_cache.json")
MARKET_FILE = Path("/Users/sarthak-p/Desktop/kalshi-btc-bot/logs/market_cache.json")
RATE_LIMIT  = 0.10


async def _get(cfg, url, params={}):
    path = urlparse(url).path
    headers = _make_rest_headers(cfg, "GET", path)
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(url, headers=headers, params=params)
        resp.raise_for_status()
        return resp.json()


async def fetch_markets(cfg):
    cutoff = int((datetime.now(timezone.utc) - timedelta(days=DAYS)).timestamp())
    url, markets, cursor, page = f"{cfg.kalshi_rest_base}/markets", [], None, 0
    while True:
        params = {"series_ticker": SERIES, "limit": 200, "status": "settled"}
        if cursor:
            params["cursor"] = cursor
        page += 1
        print(f"  Market list page {page}...", flush=True)
        data  = await _get(cfg, url, params)
        batch = data.get("markets", [])
        stop  = False
        for m in batch:
            try:
                ts = int(datetime.fromisoformat(
                    m["open_time"].replace("Z", "+00:00")).timestamp())
            except Exception:
                continue
            if ts < cutoff:
                stop = True; break
            if m.get("result") in ("yes", "no"):
                markets.append(m)
        cursor = data.get("cursor")
        if stop or not cursor or not batch:
            break
        await asyncio.sleep(RATE_LIMIT)
    return markets


async def fetch_candles(cfg, ticker, open_ts):
    url = f"{cfg.kalshi_rest_base}/series/{SERIES}/markets/{ticker}/candlesticks"
    try:
        data = await _get(cfg, url, {
            "start_ts": open_ts, "end_ts": open_ts + 16 * 60, "period_interval": 1
        })
        return data.get("candlesticks", [])
    except Exception:
        return []


async def main():
    cfg = Settings()

    # Step 1: market list (cached separately)
    if MARKET_FILE.exists():
        markets = json.loads(MARKET_FILE.read_text())
        print(f"Loaded {len(markets)} markets from cache.")
    else:
        print(f"Fetching {DAYS}-day market list...")
        markets = await fetch_markets(cfg)
        MARKET_FILE.write_text(json.dumps(markets))
        print(f"Saved {len(markets)} markets.")

    # Step 2: load partial candle cache if interrupted before
    windows = []
    cached_tickers = set()
    if CACHE_FILE.exists():
        windows = json.loads(CACHE_FILE.read_text())
        cached_tickers = {w["ticker"] for w in windows if "ticker" in w}
        print(f"Resuming from {len(windows)} cached windows.")

    remaining = [m for m in markets if m["ticker"] not in cached_tickers]
    print(f"{len(remaining)} windows still to fetch (~{len(remaining)*RATE_LIMIT/60:.0f} min).\n")

    t0 = time.monotonic()
    for i, m in enumerate(remaining):
        ticker     = m["ticker"]
        settlement = m.get("result", "").lower()
        try:
            open_ts = int(datetime.fromisoformat(
                m["open_time"].replace("Z", "+00:00")).timestamp())
        except Exception:
            continue

        candles = await fetch_candles(cfg, ticker, open_ts)
        if not candles:
            continue

        minutes = []
        for c in candles:
            ts = c.get("end_period_ts", 0)
            mn = int((ts - open_ts) / 60)
            try:
                minutes.append({
                    "minute":  mn,
                    "yes_ask": float(c["yes_ask"]["close_dollars"]),
                    "yes_bid": float(c["yes_bid"]["close_dollars"]),
                    "volume":  float(c.get("volume_fp", 0)),
                })
            except Exception:
                pass

        if minutes:
            windows.append({
                "ticker": ticker, "settlement": settlement,
                "open_ts": open_ts, "minutes": minutes
            })

        done = i + 1
        if done % 100 == 0:
            elapsed   = time.monotonic() - t0
            remaining_s = (len(remaining) - done) / done * elapsed
            print(f"  {done}/{len(remaining)}  (~{remaining_s/60:.1f} min left)  saving...", flush=True)
            CACHE_FILE.write_text(json.dumps(windows))

        await asyncio.sleep(RATE_LIMIT)

    CACHE_FILE.write_text(json.dumps(windows))
    print(f"\nDone. {len(windows)} windows cached to {CACHE_FILE}")


if __name__ == "__main__":
    asyncio.run(main())
