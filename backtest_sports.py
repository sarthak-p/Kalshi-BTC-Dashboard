"""
Sports Underdog Scalping Backtest — Kalshi
==========================================
Strategy:
  Entry    Buy YES when yes_ask ≤ 40¢  (underdog contracts)
  Exit 1   Scalp: yes_bid ≥ entry × 1.20  → +20% profit
  Exit 2   Settlement YES               → full $1.00 payout
  Exit 3   Settlement NO                → expires worthless (-100%)

Reports:
  - Overall win rate (scalp + settlement YES)
  - Average / median / percentile hold time for scalp exits
  - % that expire worthless
  - Expected value per dollar risked
  - Win rate by entry-price bucket

Data pipeline:
  1. python backtest_sports.py --fetch-series  → enumerate all Sports series → logs/sports_series.json
  2. python backtest_sports.py --fetch-markets → pull settled markets for game-result series → logs/sports_markets.json
  3. python backtest_sports.py --fetch-candles → pull 1-hr candles for each market → logs/sports_candles.json
  4. python backtest_sports.py                 → simulate and print report

Or run all phases at once:
  python backtest_sports.py --fetch-all

Tune the strategy:
  python backtest_sports.py --entry-max 0.35 --markup 1.15
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from config import Settings
from feeds.kalshi_ws import _make_rest_headers

# ── Cache files ──────────────────────────────────────────────────────────────
LOG_DIR              = ROOT / "logs"
SERIES_FILE          = LOG_DIR / "sports_series.json"
SPORTS_MARKETS_FILE  = LOG_DIR / "sports_markets.json"
SPORTS_CANDLES_FILE  = LOG_DIR / "sports_candles.json"

# ── Strategy params ──────────────────────────────────────────────────────────
MAX_ENTRY_ASK   = 0.40   # buy if yes_ask ≤ this
MIN_ENTRY_ASK   = 0.00   # buy if yes_ask ≥ this (0 = no floor)
PROFIT_CENTS    = 20     # flat profit target in cents (sell at entry + PROFIT_CENTS / 100)
ENTRY_HOURS_MAX = None   # only enter within this many hours of market open (None = no limit)
CANDLE_MIN      = 60     # 1-hour candles  (sports markets span hours–days)
RATE_LIMIT_S    = 0.15   # ~6 req/s, well under Kalshi's 10 req/s limit

# ── Sport filter keywords (--sport flag) ─────────────────────────────────────
SPORT_FILTERS: dict[str, tuple[str, ...]] = {
    "tennis":   ("ATP", "WTA", "TENNIS", "WIMBLEDON", "USOPEN", "AUSOPEN",
                 "FRENCHOPEN", "ROLANDGARROS", "GRANDSLAM", "ATPRETURN",
                 "KXMOMEN", "KXWOMEN"),
    "soccer":   ("SOCCER", "FOOTBALL", "EPL", "UEFA", "MLS", "LIGUE", "BUNDESLIGA",
                 "SERIEA", "LALIGA", "CHAMPIONSLEAGUE", "WORLDCUP", "EUROCUP"),
    "nfl":      ("NFL", "SUPERBOWL"),
    "nba":      ("NBA", "BASKETBALL"),
    "mlb":      ("MLB", "BASEBALL"),
    "nhl":      ("NHL", "HOCKEY"),
}

# ── Game-result series identification ────────────────────────────────────────
# A series whose ticker contains any of these substrings is likely a per-game
# win/loss/draw market — exactly what the underdog-scalp strategy targets.
GAME_KEYWORDS = (
    "GAME", "MATCH", "SERIES", "WIN", "WINNER",
    "SPREAD", "MONEYLINE", "RESULT",
)


# ── HTTP helper ───────────────────────────────────────────────────────────────

async def _get(cfg: Settings, url: str, params: dict = {}) -> dict:
    path = urlparse(url).path
    headers = _make_rest_headers(cfg, "GET", path)
    async with httpx.AsyncClient(timeout=25.0) as client:
        r = await client.get(url, headers=headers, params=params)
        r.raise_for_status()
        return r.json()


# ── Series ticker extraction ──────────────────────────────────────────────────

def _series_from_ticker(ticker: str) -> str:
    """KXUELGAME-26MAY20SCFAVL-TIE  →  KXUELGAME"""
    return ticker.split("-")[0]


def _is_game_series(series_ticker: str, title: str,
                    sport_keywords: tuple[str, ...] | None = None) -> bool:
    t   = series_ticker.upper()
    ttl = title.upper()
    if sport_keywords and not any(kw in t or kw in ttl for kw in sport_keywords):
        return False
    return any(kw in t or kw in ttl for kw in GAME_KEYWORDS)


# ── Phase 1: enumerate sports series ─────────────────────────────────────────

async def fetch_sports_series(cfg: Settings) -> list[dict]:
    url = f"{cfg.kalshi_rest_base}/series"
    sports: list[dict] = []
    cursor = None
    page = 0

    print("Enumerating Sports series…")
    while True:
        params: dict = {"limit": 200}
        if cursor:
            params["cursor"] = cursor
        page += 1

        try:
            data = await _get(cfg, url, params)
        except Exception as e:
            print(f"  Error page {page}: {e}")
            break

        batch = data.get("series", [])
        if not batch:
            break

        for s in batch:
            if s.get("category") == "Sports":
                sports.append(s)

        if page % 25 == 0:
            print(f"  Page {page}: {len(sports)} sports series so far")

        cursor = data.get("cursor")
        if not cursor:
            break

        await asyncio.sleep(RATE_LIMIT_S)

    return sports


# ── Phase 2: fetch settled markets for game-result series ────────────────────

async def fetch_markets_for_series(cfg: Settings, series_list: list[dict],
                                   sport_keywords: tuple[str, ...] | None = None) -> list[dict]:
    base = cfg.kalshi_rest_base
    markets: list[dict] = []
    game_series = [s for s in series_list
                   if _is_game_series(s["ticker"], s.get("title", ""), sport_keywords)]

    sport_label = "/".join(sport_keywords) if sport_keywords else "all sports"
    print(f"\n{len(game_series)} game-result series identified for {sport_label} "
          f"(from {len(series_list)} total sports series).")

    for i, s in enumerate(game_series):
        ticker = s["ticker"]
        cursor = None
        while True:
            params: dict = {"series_ticker": ticker, "status": "settled", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            try:
                data = await _get(cfg, f"{base}/markets", params)
            except Exception:
                break
            batch = data.get("markets", [])
            for m in batch:
                if m.get("result") in ("yes", "no"):
                    markets.append(m)
            cursor = data.get("cursor")
            if not cursor:
                break
            await asyncio.sleep(RATE_LIMIT_S)

        if (i + 1) % 50 == 0:
            print(f"  {i+1}/{len(game_series)} series scanned — {len(markets)} markets")

        await asyncio.sleep(RATE_LIMIT_S)

    return markets


# ── Phase 3: fetch candlestick data ──────────────────────────────────────────

async def _fetch_candles(cfg: Settings, series: str, ticker: str,
                         open_ts: int, close_ts: int) -> list[dict]:
    url = f"{cfg.kalshi_rest_base}/series/{series}/markets/{ticker}/candlesticks"
    try:
        data = await _get(cfg, url, {
            "start_ts":       open_ts,
            "end_ts":         close_ts,
            "period_interval": CANDLE_MIN,
        })
        return data.get("candlesticks", [])
    except Exception:
        return []


async def fetch_all_candles(cfg: Settings,
                            markets: list[dict]) -> list[dict]:
    output: list[dict] = []
    total = len(markets)

    print(f"\nFetching {CANDLE_MIN}-min candles for {total} markets…")
    for i, m in enumerate(markets):
        ticker = m["ticker"]
        series = _series_from_ticker(ticker)

        try:
            open_ts  = int(datetime.fromisoformat(
                m["open_time"].replace("Z", "+00:00")).timestamp())
            raw_close = m.get("close_time") or m.get("expiration_time") or ""
            close_ts = int(datetime.fromisoformat(
                raw_close.replace("Z", "+00:00")).timestamp())
        except Exception:
            continue

        candles = await _fetch_candles(cfg, series, ticker, open_ts, close_ts)
        if candles:
            output.append({
                "ticker":    ticker,
                "result":    m.get("result"),
                "title":     m.get("title", ""),
                "open_time": m.get("open_time"),
                "candles":   candles,
            })

        if (i + 1) % 100 == 0:
            print(f"  {i+1}/{total} — {len(output)} with candle data")

        await asyncio.sleep(RATE_LIMIT_S)

    return output


# ── Strategy simulation ───────────────────────────────────────────────────────

def simulate_market(item: dict) -> dict | None:
    """
    Simulate one trade on a market's candle history.

    Entry:    first candle where yes_ask ≤ MAX_ENTRY_ASK
    Target:   yes_bid ≥ entry × SELL_MARKUP  (20% profit scalp)
    Fallback: hold to settlement → YES (jackpot) or NO (worthless)
    """
    settlement = (item.get("result") or "").lower()
    candles = sorted(item.get("candles", []),
                     key=lambda c: c.get("end_period_ts", 0))

    entry_px:    float | None = None
    entry_ts:    int   | None = None
    sell_target: float | None = None

    for c in candles:
        try:
            yes_ask = float(c["yes_ask"]["close_dollars"])
            yes_bid = float(c["yes_bid"]["close_dollars"])
            ts      = int(c["end_period_ts"])
        except (KeyError, TypeError, ValueError):
            continue

        if yes_ask <= 0 or yes_bid <= 0:
            continue

        # Enforce pre-game-only window if requested
        if ENTRY_HOURS_MAX is not None and entry_px is None:
            try:
                open_ts = int(datetime.fromisoformat(
                    item["open_time"].replace("Z", "+00:00")).timestamp())
                if (ts - open_ts) / 3600 > ENTRY_HOURS_MAX:
                    continue
            except Exception:
                pass

        if entry_px is None:
            if MIN_ENTRY_ASK <= yes_ask <= MAX_ENTRY_ASK:
                entry_px    = yes_ask
                entry_ts    = ts
                sell_target = entry_px + PROFIT_CENTS / 100
        else:
            if yes_bid >= sell_target:
                hold_min = (ts - entry_ts) / 60.0
                pnl_pct = (sell_target - entry_px) / entry_px * 100
                return {
                    "exit":     "scalp",
                    "entry_c":  round(entry_px * 100, 1),
                    "exit_c":   round(sell_target * 100, 1),
                    "hold_min": round(hold_min, 1),
                    "pnl_pct":  round(pnl_pct, 1),
                    "result":   settlement,
                    "ticker":   item["ticker"],
                }

    if entry_px is None:
        return None

    if settlement == "yes":
        pnl_pct = (1.00 - entry_px) / entry_px * 100
        return {
            "exit":     "settlement_yes",
            "entry_c":  round(entry_px * 100, 1),
            "exit_c":   100.0,
            "hold_min": None,
            "pnl_pct":  round(pnl_pct, 1),
            "result":   settlement,
            "ticker":   item["ticker"],
        }
    else:
        return {
            "exit":     "worthless",
            "entry_c":  round(entry_px * 100, 1),
            "exit_c":   0.0,
            "hold_min": None,
            "pnl_pct":  -100.0,
            "result":   settlement,
            "ticker":   item["ticker"],
        }


# ── Reporting ─────────────────────────────────────────────────────────────────

def report(trades: list[dict]) -> None:
    if not trades:
        print("No qualifying trades found.")
        return

    scalp      = [t for t in trades if t["exit"] == "scalp"]
    settle_yes = [t for t in trades if t["exit"] == "settlement_yes"]
    worthless  = [t for t in trades if t["exit"] == "worthless"]
    total      = len(trades)

    win_rate       = (len(scalp) + len(settle_yes)) / total * 100
    worthless_rate = len(worthless) / total * 100
    avg_entry_c    = sum(t["entry_c"] for t in trades) / total

    hold_times = sorted(t["hold_min"] for t in scalp if t["hold_min"] is not None)

    avg_settle_return = (
        sum(t["pnl_pct"] for t in settle_yes) / len(settle_yes) / 100
        if settle_yes else 0.0
    )
    ev = (
        len(scalp)      / total *  0.20
      + len(settle_yes) / total *  avg_settle_return
      + len(worthless)  / total * -1.00
    )

    W = 66
    entry_range = (f"{MIN_ENTRY_ASK*100:.0f}-{MAX_ENTRY_ASK*100:.0f}¢"
                   if MIN_ENTRY_ASK > 0 else f"≤{MAX_ENTRY_ASK*100:.0f}¢")
    hours_label = (f"  entry ≤{ENTRY_HOURS_MAX:.0f}h"
                   if ENTRY_HOURS_MAX is not None else "")
    print(f"\n{'━'*W}")
    print(f"  SPORTS UNDERDOG SCALP  "
          f"(buy {entry_range}, sell @+{PROFIT_CENTS}¢{hours_label})")
    print(f"{'━'*W}")
    print(f"  Qualifying trades     {total}")
    print(f"  Avg entry price       {avg_entry_c:.1f}¢")
    print()
    print(f"  ── Exit breakdown ─────────────────────────────────────────")
    print(f"  Scalp  (+20%)         {len(scalp):>5}  ({len(scalp)/total*100:5.1f}%)")
    if settle_yes:
        avg_sy = sum(t["pnl_pct"] for t in settle_yes) / len(settle_yes)
        print(f"  Settlement YES        {len(settle_yes):>5}  ({len(settle_yes)/total*100:5.1f}%)  "
              f"avg +{avg_sy:.0f}%")
    else:
        print(f"  Settlement YES        {0:>5}  ({0.0:5.1f}%)")
    print(f"  Expires worthless     {len(worthless):>5}  ({worthless_rate:5.1f}%)")
    print()
    print(f"  ── Key stats ──────────────────────────────────────────────")
    print(f"  Win rate              {win_rate:5.1f}%")
    print(f"  Expires worthless     {worthless_rate:5.1f}%")

    if hold_times:
        avg_h = sum(hold_times) / len(hold_times)
        p25   = hold_times[max(0, int(len(hold_times) * 0.25))]
        p50   = hold_times[len(hold_times) // 2]
        p75   = hold_times[min(len(hold_times)-1, int(len(hold_times) * 0.75))]
        p90   = hold_times[min(len(hold_times)-1, int(len(hold_times) * 0.90))]
        print(f"  Avg scalp hold        {avg_h:>6.0f} min  ({avg_h/60:.1f} h)")
        print(f"  Median scalp hold     {p50:>6.0f} min  ({p50/60:.1f} h)")
        print(f"  p25/p50/p75/p90       {p25:.0f}m / {p50:.0f}m / {p75:.0f}m / {p90:.0f}m")
    else:
        print(f"  Avg scalp hold        N/A")

    print(f"  Expected value/trade  {ev*100:+.1f}%  per $ risked")
    print(f"{'━'*W}")

    # Breakdown by entry-price bucket
    buckets: dict[str, list] = defaultdict(list)
    for t in trades:
        lo = int(t["entry_c"] // 10) * 10
        buckets[f"{lo}-{lo+9}¢"].append(t)

    if len(buckets) > 1:
        print(f"\n  ── Win rate by entry price ────────────────────────────────")
        for label in sorted(buckets):
            ts_ = buckets[label]
            wr_ = sum(1 for t in ts_ if t["exit"] != "worthless") / len(ts_) * 100
            print(f"  {label:>9}  {len(ts_):>5} trades  {wr_:5.1f}% win")

    print()


# ── CLI entry points ──────────────────────────────────────────────────────────

def _cache_paths(sport: str | None) -> tuple[Path, Path, Path]:
    """Return (series, markets, candles) cache paths, namespaced by sport."""
    suffix = f"_{sport}" if sport else ""
    return (
        LOG_DIR / f"sports_series{suffix}.json",
        LOG_DIR / f"sports_markets{suffix}.json",
        LOG_DIR / f"sports_candles{suffix}.json",
    )


async def run_fetch_series(cfg: Settings, sport: str | None) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    series_file, _, _ = _cache_paths(sport)
    series = await fetch_sports_series(cfg)
    series_file.write_text(json.dumps(series))
    print(f"Saved {len(series)} sports series → {series_file}")


async def run_fetch_markets(cfg: Settings, sport: str | None) -> None:
    series_file, markets_file, _ = _cache_paths(sport)
    if not series_file.exists():
        print(f"Run --fetch-series first.")
        return
    series = json.loads(series_file.read_text())
    sport_keywords = SPORT_FILTERS.get(sport) if sport else None
    markets = await fetch_markets_for_series(cfg, series, sport_keywords)
    markets_file.write_text(json.dumps(markets))
    print(f"Saved {len(markets)} markets → {markets_file}")


async def run_fetch_candles(cfg: Settings, sport: str | None) -> None:
    _, markets_file, candles_file = _cache_paths(sport)
    if not markets_file.exists():
        print("Run --fetch-markets first.")
        return
    markets = json.loads(markets_file.read_text())
    output = await fetch_all_candles(cfg, markets)
    candles_file.write_text(json.dumps(output))
    print(f"Saved {len(output)} candle records → {candles_file}")


def run_backtest(sport: str | None) -> None:
    _, _, candles_file = _cache_paths(sport)
    if not candles_file.exists():
        print(f"No candle cache at {candles_file}. Run with --fetch-all first.")
        sys.exit(1)

    data = json.loads(candles_file.read_text())
    print(f"Loaded {len(data)} markets from cache.")

    trades: list[dict] = []
    no_entry = 0
    for item in data:
        trade = simulate_market(item)
        if trade:
            trades.append(trade)
        else:
            no_entry += 1

    print(f"Simulated {len(trades)} trades "
          f"({no_entry} had no qualifying entry ≤ {MAX_ENTRY_ASK*100:.0f}¢).")
    report(trades)


async def main() -> None:
    global MAX_ENTRY_ASK, MIN_ENTRY_ASK, PROFIT_CENTS, ENTRY_HOURS_MAX

    valid_sports = list(SPORT_FILTERS.keys())
    parser = argparse.ArgumentParser()
    parser.add_argument("--fetch-all",     action="store_true", help="Run all fetch phases")
    parser.add_argument("--fetch-series",  action="store_true", help="Phase 1: enumerate sports series")
    parser.add_argument("--fetch-markets", action="store_true", help="Phase 2: fetch settled markets")
    parser.add_argument("--fetch-candles", action="store_true", help="Phase 3: fetch candlestick data")
    parser.add_argument("--sport", choices=valid_sports, default=None,
                        help=f"Limit to one sport: {valid_sports}")
    parser.add_argument("--entry-max",     type=float, default=MAX_ENTRY_ASK,
                        help=f"Max yes_ask to enter, dollars (default {MAX_ENTRY_ASK})")
    parser.add_argument("--entry-min",     type=float, default=MIN_ENTRY_ASK,
                        help=f"Min yes_ask to enter, dollars (default {MIN_ENTRY_ASK})")
    parser.add_argument("--profit-cents",  type=int,   default=PROFIT_CENTS,
                        help=f"Flat profit target in cents, e.g. 20 means sell at entry+20¢ (default {PROFIT_CENTS})")
    parser.add_argument("--entry-hours",   type=float, default=None,
                        help="Only enter within this many hours of market open (pre-game filter)")
    args = parser.parse_args()

    MAX_ENTRY_ASK   = args.entry_max
    MIN_ENTRY_ASK   = args.entry_min
    PROFIT_CENTS    = args.profit_cents
    ENTRY_HOURS_MAX = args.entry_hours
    sport         = args.sport

    cfg = Settings()

    if args.fetch_all or args.fetch_series:
        await run_fetch_series(cfg, sport)
    if args.fetch_all or args.fetch_markets:
        await run_fetch_markets(cfg, sport)
    if args.fetch_all or args.fetch_candles:
        await run_fetch_candles(cfg, sport)

    if not any([args.fetch_all, args.fetch_series, args.fetch_markets, args.fetch_candles]):
        run_backtest(sport)


if __name__ == "__main__":
    asyncio.run(main())
