"""
Backtest — Hold-to-Settlement Strategy
=======================================
Mirrors the current live bot rules precisely:

  Entry window  Minutes 10–12 (300 s → 120 s left in the 15-min window)
  Lock trigger  GBM holds YES (>60%) or NO (<40%) for 2 consecutive minutes
                (1-min candle data approximates the 20-second stability timer)
  Entry price   Small gap (<15¢): post limit at bid+1¢
                Large gap (≥15¢): take the ask
                Hard ceiling 85¢ — if computed limit > 85¢, post resting limit
                at exactly 85¢ and simulate fill only if bid touches ≤85¢ later
                in the same window.
  Slope guard   Skip if slope opposes lock direction by >0.10 $/s at entry tick
  Exit          Hold to settlement — YES pays $1.00, NO pays $0.00
  Sizing        Flat TRADE_SIZE_USD per trade (converted to integer contracts)
  Bankroll      Starts at STARTING_BANKROLL; updated each trade

Requires  logs/candle_cache.json  +  logs/btc_1m_cache.json
Run       python backtest_settlement.py
"""
from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

CACHE_FILE = Path("logs/candle_cache.json")
BTC_FILE   = Path("logs/btc_1m_cache.json")

# ── Strategy params ───────────────────────────────────────────────────────────
SIGMA             = 0.35        # matches live bot: DVOL ~35% used when tau > 3 min
YEAR_S            = 365.25 * 24 * 3600
STARTING_BANKROLL = 150.0
TRADE_SIZE_USD    = 15.0

GBM_YES_MIN    = 60.0           # fv must exceed this to consider YES
GBM_NO_MAX     = 40.0           # fv must be below this to consider NO
LOCK_MINUTES   = 1              # consecutive minutes GBM must hold side before locking
ENTRY_START    = 10             # minute index where entry window opens (300 s left)
ENTRY_END      = 13             # minute index where entry window closes (120 s left)
LARGE_GAP_C    = 0.0           # ¢ — take ask instead of posting maker
CEILING_C      = 85.0           # ¢ — hard cap; above this post resting limit at ceiling
FLOOR_C        = 20.0           # ¢ — skip if entry price below this (illiquid / GBM noise)
SLOPE_OPPOSE_THRESHOLD = 0.10   # $/s — block if slope actively opposes direction (overridable via --slope-threshold)


# ── GBM ───────────────────────────────────────────────────────────────────────

def _ncdf(x: float) -> float:
    return math.erfc(-x / math.sqrt(2.0)) / 2.0


SLOPE_CAP_S = 90.0  # live bot caps slope projection at 90 s

def gbm_fv(btc: float, btc_open: float, tau_s: float, slope: float = 0.0) -> float:
    if btc_open <= 0 or tau_s <= 0:
        return 100.0 if btc >= btc_open else 0.0
    vol = SIGMA * math.sqrt(tau_s / YEAR_S)
    if vol <= 0:
        return 100.0 if btc >= btc_open else 0.0
    drift = slope * min(tau_s, SLOPE_CAP_S)
    z = ((btc + drift - btc_open) / btc_open) / vol
    return max(5.0, min(95.0, _ncdf(z) * 100.0))


# ── Main simulation ───────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-slope-filter", action="store_true",
                        help="Disable the slope-opposing guard")
    parser.add_argument("--no-ceiling",      action="store_true",
                        help="Remove 85¢ ceiling — take trades at whatever the ask is")
    parser.add_argument("--min-gap", type=float, default=None,
                        help="Min GBM-vs-market edge in cents to take a trade (e.g. 0 = positive edge only)")
    parser.add_argument("--no-reversal-guard", action="store_true",
                        help="Disable the early-window GBM reversal guard")
    parser.add_argument("--entry-start", type=int, default=ENTRY_START,
                        help=f"Minute index where entry window opens (default {ENTRY_START} = {15-ENTRY_START} min left)")
    parser.add_argument("--strong-tier", type=float, default=15.0,
                        help="Min |fv-50| to lock (default 15 = 65%% YES / 35%% NO threshold)")
    parser.add_argument("--lock-minutes", type=int, default=LOCK_MINUTES,
                        help=f"Consecutive minutes GBM must hold before locking (default {LOCK_MINUTES})")
    parser.add_argument("--slope-threshold", type=float, default=SLOPE_OPPOSE_THRESHOLD,
                        help=f"$/s slope opposition threshold (default {SLOPE_OPPOSE_THRESHOLD})")
    parser.add_argument("--hours", type=str, default=None,
                        help="Only include windows in this UTC hour range, e.g. 13-21")
    args = parser.parse_args()

    no_ceiling = args.no_ceiling
    entry_start = args.entry_start
    strong_tier = args.strong_tier
    hour_range: tuple[int, int] | None = None
    if args.hours:
        start_h, end_h = map(int, args.hours.split("-"))
        hour_range = (start_h, end_h)
    use_slope_filter = not args.no_slope_filter

    print("Loading data...")
    candles = json.loads(CACHE_FILE.read_text())
    btc_raw = json.loads(BTC_FILE.read_text())
    btc_by_ts: dict[int, float] = {r["ts"]: r["close"] for r in btc_raw}

    def btc_at(ts: int) -> float | None:
        for d in (0, -60, 60, -120, 120):
            p = btc_by_ts.get(ts + d)
            if p:
                return p
        return None

    trades:  list[dict] = []
    skipped: dict[str, int] = defaultdict(int)
    no_btc = 0

    for w in sorted(candles, key=lambda x: x["open_ts"]):
        if hour_range is not None:
            wh = datetime.fromtimestamp(w["open_ts"], tz=timezone.utc).hour
            if not (hour_range[0] <= wh < hour_range[1]):
                continue
        open_ts    = w["open_ts"]
        settlement = w["settlement"]          # "yes" | "no"
        by_min     = {m["minute"]: m for m in w["minutes"]}

        btc_open = btc_at(open_ts)
        if btc_open is None:
            no_btc += 1
            continue

        # ── Scan entry window for a lock ─────────────────────────────────────
        stable_side:  str | None = None
        stable_count: int        = 0
        entry:        dict | None = None
        early_fv:     float | None = None   # GBM at window open (minute 10)

        for mn in range(entry_start, ENTRY_END):
            c = by_min.get(mn)
            if c is None:
                stable_side, stable_count = None, 0
                continue

            btc_now = btc_at(open_ts + mn * 60)
            if btc_now is None:
                stable_side, stable_count = None, 0
                continue

            tau_s      = (15 - mn) * 60
            btc_prev   = btc_at(open_ts + (mn - 1) * 60)
            slope      = ((btc_now - btc_prev) / 60.0) if btc_prev else 0.0
            fv         = gbm_fv(btc_now, btc_open, tau_s, slope)

            # Record GBM at entry window open for reversal guard
            if early_fv is None:
                early_fv = fv

            raw_side = None
            if fv >= GBM_YES_MIN:
                raw_side = "YES"
            elif fv <= GBM_NO_MAX:
                raw_side = "NO"

            if raw_side is None or raw_side != stable_side:
                stable_side  = raw_side
                stable_count = 1 if raw_side else 0
            else:
                stable_count += 1

            if stable_count < args.lock_minutes:
                continue

            # ── Strong tier: |fv-50| >= 15 (fv>=65% YES, fv<=35% NO) ─────────
            # Failure doesn't reset the timer — bot retries next tick
            if abs(fv - 50.0) < strong_tier:
                skipped["strong_tier"] += 1
                continue

            # ── Reversal guard: early-window GBM must not oppose direction ────
            # early_fv must be >= 40% for YES lock, <= 60% for NO lock
            if not args.no_reversal_guard and early_fv is not None:
                reversal_blocked = (
                    (raw_side == "YES" and early_fv < 40.0) or
                    (raw_side == "NO"  and early_fv > 60.0)
                )
                if reversal_blocked:
                    skipped["reversal"] += 1
                    break  # prediction_locked_yes_pct fixed for the whole window

            # ── GBM reversed check (executor guard after lock) ────────────────
            if (raw_side == "YES" and fv < 45.0) or (raw_side == "NO" and fv > 55.0):
                skipped["gbm_reversed"] += 1
                continue

            # ── Slope guard (off by default) ──────────────────────────────────
            slope_opposes = (
                (raw_side == "YES" and slope < -args.slope_threshold) or
                (raw_side == "NO"  and slope >  args.slope_threshold)
            )
            if use_slope_filter and slope_opposes:
                skipped["slope"] += 1
                break

            # ── Compute limit price ───────────────────────────────────────────
            yes_ask = c["yes_ask"]
            yes_bid = c["yes_bid"]
            mid     = (yes_ask + yes_bid) / 2.0

            if raw_side == "YES":
                gap_c    = (fv - mid * 100)
                taker_px = yes_ask
                maker_px = min(yes_bid + 0.01, yes_ask - 0.01)
            else:
                # NO side: price in cents of the NO contract
                no_ask   = 1.0 - yes_bid
                no_bid   = 1.0 - yes_ask
                gap_c    = ((100 - fv) - (1.0 - mid) * 100)
                taker_px = no_ask
                maker_px = min(no_bid + 0.01, no_ask - 0.01)

            limit_px = taker_px if abs(gap_c) >= LARGE_GAP_C / 100 else maker_px

            # ── GBM-market gap filter ─────────────────────────────────────────
            if args.min_gap is not None and gap_c < args.min_gap:
                skipped["gap_filter"] += 1
                break

            # ── 20¢ floor ─────────────────────────────────────────────────────
            if limit_px * 100 < FLOOR_C:
                skipped["floor"] += 1
                break

            # ── 85¢ ceiling ───────────────────────────────────────────────────
            resting_at_ceiling = False
            if not no_ceiling and limit_px * 100 > CEILING_C:
                limit_px           = CEILING_C / 100.0
                resting_at_ceiling = True

            entry = {
                "mn":                mn,
                "side":              raw_side,
                "limit_px":          limit_px,
                "fv":                fv,
                "gap_c":             gap_c,
                "resting_at_ceiling": resting_at_ceiling,
            }
            break

        if entry is None:
            skipped["no_lock"] += 1
            continue

        # ── Simulate fill ─────────────────────────────────────────────────────
        fill_px: float | None = None
        side     = entry["side"]
        limit_px = entry["limit_px"]

        if not entry["resting_at_ceiling"]:
            # Normal limit — assume immediate fill at the posted price
            fill_px = limit_px
        else:
            # Resting at ceiling: fill only if market comes down to ≤ ceiling
            for mn2 in range(entry["mn"], ENTRY_END + 1):
                c2 = by_min.get(mn2)
                if c2 is None:
                    continue
                check_bid = c2["yes_bid"] if side == "YES" else (1.0 - c2["yes_ask"])
                if check_bid <= limit_px + 0.001:
                    fill_px = limit_px
                    break
            if fill_px is None:
                skipped["ceiling_no_fill"] += 1
                continue

        # ── Compute PnL ───────────────────────────────────────────────────────
        n_contracts = max(1, int(TRADE_SIZE_USD / fill_px))
        cost        = round(n_contracts * fill_px, 2)

        # Kalshi taker fee: 7% × p × (1-p) per contract
        fee = round(n_contracts * 0.07 * fill_px * (1.0 - fill_px), 4)

        won = (side == "YES" and settlement == "yes") or \
              (side == "NO"  and settlement == "no")

        proceeds = round(n_contracts * 1.0, 2) if won else 0.0
        pnl      = round(proceeds - cost - fee, 2)

        date = datetime.fromtimestamp(open_ts, tz=timezone.utc).strftime("%Y-%m-%d")
        trades.append({
            "date":    date,
            "side":    side,
            "entry_c": round(fill_px * 100, 1),
            "fv":      round(entry["fv"], 1),
            "gap_c":   round(entry["gap_c"], 1),
            "n":       n_contracts,
            "cost":    cost,
            "won":     won,
            "pnl":     pnl,
            "ceiling": entry["resting_at_ceiling"],
        })

    if not trades:
        print("No trades found.")
        return

    # ── Day-by-day P&L ───────────────────────────────────────────────────────
    by_day: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        by_day[t["date"]].append(t)

    balance  = STARTING_BANKROLL
    peak     = STARTING_BANKROLL
    max_dd   = 0.0
    for t in trades:
        balance = round(balance + t["pnl"], 2)
        peak    = max(peak, balance)
        max_dd  = max(max_dd, peak - balance)

    balance   = STARTING_BANKROLL
    day_rows  = []
    for date in sorted(by_day):
        day_trades = by_day[date]
        day_pnl    = round(sum(t["pnl"] for t in day_trades), 2)
        day_wins   = sum(1 for t in day_trades if t["won"])
        balance    = round(balance + day_pnl, 2)
        day_rows.append({
            "date": date, "trades": len(day_trades),
            "wins": day_wins, "pnl": day_pnl, "balance": balance,
        })

    # ── Print report ─────────────────────────────────────────────────────────
    dates      = sorted(by_day)
    date_start = dates[0]
    date_end   = dates[-1]
    n_days     = len(day_rows)
    wins       = [t for t in trades if t["won"]]
    losses     = [t for t in trades if not t["won"]]
    total_pnl  = sum(t["pnl"] for t in trades)

    slope_label   = "no slope filter" if not use_slope_filter else "slope filter ON"
    ceiling_label = "no ceiling" if no_ceiling else f"≤{CEILING_C:.0f}¢ ceiling"
    gap_label     = f"gap≥{args.min_gap:+.0f}¢" if args.min_gap is not None else "no gap filter"
    reversal_label = "no reversal guard" if args.no_reversal_guard else "reversal guard ON"
    entry_label   = f"entry@min{entry_start}({15-entry_start}min)" if entry_start != ENTRY_START else "entry@min10(5min)"
    tier_label    = f"tier={50+strong_tier:.0f}%" if strong_tier != 15.0 else "tier=65%"
    lock_label    = f"lock={args.lock_minutes}min"
    slope_thresh_label = f"slope>{args.slope_threshold:.2f}"
    print(f"\n{'━'*72}")
    print(f"  BACKTEST  {date_start} → {date_end}  |  "
          f"σ={SIGMA}  ${TRADE_SIZE_USD:.0f}/trade  [{slope_label}  {ceiling_label}  {gap_label}  {reversal_label}  {entry_label}  {tier_label}  {lock_label}  {slope_thresh_label}]")
    print(f"{'━'*72}")
    print(f"  {'Date':<12} {'Trades':>6} {'W/L':>6}  {'Day P&L':>9}  {'Balance':>9}  Bar")
    print(f"  {'─'*10} {'─'*6} {'─'*6}  {'─'*9}  {'─'*9}  {'─'*20}")

    for r in day_rows:
        bar_val = r["pnl"]
        n_w, n_l = r["wins"], r["trades"] - r["wins"]
        if bar_val >= 0:
            bar = "\033[32m+" + "█" * min(int(abs(bar_val) / 2), 20) + "\033[0m"
        else:
            bar = "\033[31m-" + "█" * min(int(abs(bar_val) / 2), 20) + "\033[0m"
        sign = "+" if r["pnl"] >= 0 else ""
        print(f"  {r['date']:<12} {r['trades']:>6} {n_w:>3}W{n_l:>2}L"
              f"  {sign}${r['pnl']:>7.2f}  ${r['balance']:>8.2f}  {bar}")

    print(f"\n{'━'*72}")
    print(f"  SUMMARY")
    print(f"{'━'*72}")
    print(f"  Period          {date_start}  →  {date_end}  ({n_days} trading days)")
    print(f"  Total trades    {len(trades)}  ({len(trades)/n_days:.1f}/day)")
    print(f"  Win rate        {len(wins)/len(trades)*100:.1f}%  ({len(wins)}W / {len(losses)}L)")
    if wins:
        print(f"  Avg win         +${sum(t['pnl'] for t in wins)/len(wins):.2f}")
    if losses:
        print(f"  Avg loss        ${sum(t['pnl'] for t in losses)/len(losses):.2f}")
    if wins and losses:
        wr = sum(t["pnl"] for t in wins) / len(wins)
        lr = abs(sum(t["pnl"] for t in losses) / len(losses))
        print(f"  Win:Loss ratio  {wr/lr:.2f}x")
    print(f"  Total P&L       ${total_pnl:+.2f}")
    print(f"  Avg $/day       ${total_pnl/n_days:+.2f}")
    print(f"  Start balance   ${STARTING_BANKROLL:.2f}")
    print(f"  End balance     ${balance:.2f}")
    print(f"  Return          {(balance-STARTING_BANKROLL)/STARTING_BANKROLL*100:+.1f}%")
    print(f"  Max drawdown    ${max_dd:.2f}")
    print(f"  Losing days     {sum(1 for r in day_rows if r['pnl'] < 0)} / {n_days}")

    # Entry price distribution
    by_bucket: dict[str, list] = defaultdict(list)
    for t in trades:
        lo = int(t["entry_c"] // 10) * 10
        by_bucket[f"{lo}-{lo+9}¢"].append(t)
    print(f"\n  ── Win rate by entry price ────────────────────────────────────")
    for label in sorted(by_bucket):
        ts_ = by_bucket[label]
        wr_ = sum(1 for t in ts_ if t["won"]) / len(ts_) * 100
        avg_pnl = sum(t["pnl"] for t in ts_) / len(ts_)
        print(f"    {label:>9}  {len(ts_):>4} trades  {wr_:5.1f}% win  avg ${avg_pnl:+.2f}")

    # Side breakdown
    yes_t = [t for t in trades if t["side"] == "YES"]
    no_t  = [t for t in trades if t["side"] == "NO"]
    print(f"\n  ── Side breakdown ─────────────────────────────────────────────")
    for label, ts_ in [("YES", yes_t), ("NO", no_t)]:
        if not ts_:
            continue
        wr_ = sum(1 for t in ts_ if t["won"]) / len(ts_) * 100
        pnl_ = sum(t["pnl"] for t in ts_)
        print(f"    {label:>3}  {len(ts_):>4} trades  {wr_:5.1f}% win  total ${pnl_:+.2f}")

    # Ceiling trades
    ceiling_trades = [t for t in trades if t["ceiling"]]
    if ceiling_trades:
        wr_ = sum(1 for t in ceiling_trades if t["won"]) / len(ceiling_trades) * 100
        print(f"\n  ── Resting-limit trades (ask > 85¢, posted @85¢) ─────────────")
        print(f"    {len(ceiling_trades)} fills  {wr_:.1f}% win  "
              f"total ${sum(t['pnl'] for t in ceiling_trades):+.2f}")

    print(f"\n  Skipped: {dict(skipped)}")
    print(f"  Windows with no BTC data: {no_btc}")
    print()


if __name__ == "__main__":
    main()
