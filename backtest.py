"""
Backtest — exact live strategy simulation
=========================================
Mirrors the live bot rules precisely:

  Entry    GBM > 60% → YES  |  GBM < 40% → NO
           First qualifying minute in window (minutes 3–12)
           30¢ ≤ entry price ≤ 75¢  (price filter)
           One trade per window

  Exits    1. TP:          bid reaches entry + 10¢  (checked each minute)
           2. GBM neutral: GBM crosses 50%          (checked each minute)
           3. Time:        minute 13 (120 s left)   — never settles

  Sizing   Flat $50 per trade regardless of balance
  Bankroll Starts at $500; grows/shrinks with each trade

Requires  logs/candle_cache.json  +  logs/btc_1m_cache.json
Run       python backtest.py
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

CACHE_FILE = Path("logs/candle_cache.json")
BTC_FILE   = Path("logs/btc_1m_cache.json")

# ── Strategy params (match live bot) ─────────────────────────────────────────
SIGMA          = 0.80
YEAR_S         = 365.25 * 24 * 3600
STARTING_BANKROLL = 25.0
TRADE_SIZE_USD = 5.0
TP_CENTS       = 10.0
GBM_YES_MIN    = 60.0
GBM_NO_MAX     = 40.0
GBM_NEUTRAL    = 50.0
MIN_PRICE_C    = 30.0
MAX_PRICE_C    = 75.0
ENTRY_MIN      = 3
TIME_EXIT_MIN  = 13
MIN_GAP_C      = 8.0          # ¢ — GBM must exceed market price by this much to enter
SLIPPAGE_C     = 3.0          # ¢ worse than displayed bid on IOC exits (GBM neutral/time exits)


def _ncdf(x: float) -> float:
    return math.erfc(-x / math.sqrt(2.0)) / 2.0


def gbm(btc: float, btc_open: float, tau_s: float) -> float:
    if btc_open <= 0 or tau_s <= 0:
        return 100.0 if btc >= btc_open else 0.0
    vol = SIGMA * math.sqrt(tau_s / YEAR_S)
    if vol <= 0:
        return 100.0 if btc >= btc_open else 0.0
    z = ((btc - btc_open) / btc_open) / vol
    return max(5.0, min(95.0, _ncdf(z) * 100.0))


def main() -> None:
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

    # ── Simulate every window ─────────────────────────────────────────────────
    trades: list[dict] = []
    no_btc = 0

    for w in sorted(candles, key=lambda x: x["open_ts"]):
        open_ts = w["open_ts"]
        by_min  = {m["minute"]: m for m in w["minutes"]}

        btc_open = btc_at(open_ts)
        if btc_open is None:
            no_btc += 1
            continue

        # Find first valid entry minute
        entry = None
        for mn in range(ENTRY_MIN, TIME_EXIT_MIN):
            c = by_min.get(mn)
            if c is None:
                continue
            btc_now = btc_at(open_ts + mn * 60)
            if btc_now is None:
                continue

            fv    = gbm(btc_now, btc_open, (15 - mn) * 60)
            yes_a = c["yes_ask"]
            yes_b = c["yes_bid"]

            if fv > GBM_YES_MIN:
                px   = yes_a
                edge = fv - px * 100  # GBM fv (¢) minus market ask (¢)
                if MIN_PRICE_C / 100 <= px <= MAX_PRICE_C / 100 and edge >= MIN_GAP_C:
                    entry = {"side": "YES", "mn": mn, "px": px, "fv": fv}
                    break
            elif fv < GBM_NO_MAX:
                px   = 1.0 - yes_b
                edge = (100 - fv) - px * 100  # NO GBM fv (¢) minus NO ask (¢)
                if MIN_PRICE_C / 100 <= px <= MAX_PRICE_C / 100 and edge >= MIN_GAP_C:
                    entry = {"side": "NO", "mn": mn, "px": px, "fv": fv}
                    break

        if entry is None:
            continue

        side      = entry["side"]
        entry_px  = entry["px"]
        tp_target = entry_px + TP_CENTS / 100.0

        # Simulate exit
        exit_px     = None
        exit_reason = None

        for mn in range(entry["mn"] + 1, 16):
            c = by_min.get(mn)
            sell_px = None
            if c:
                sell_px = c["yes_bid"] if side == "YES" else (1.0 - c["yes_ask"])

            if mn >= TIME_EXIT_MIN:
                # IOC exit — apply slippage
                raw = sell_px if sell_px is not None else entry_px
                exit_px     = max(0.01, raw - SLIPPAGE_C / 100.0)
                exit_reason = "time"
                break

            if sell_px is None:
                continue

            if sell_px >= tp_target:
                exit_px     = tp_target  # limit sell — no slippage
                exit_reason = "tp"
                break

            btc_now = btc_at(open_ts + mn * 60)
            if btc_now is not None:
                fv = gbm(btc_now, btc_open, (15 - mn) * 60)
                if (side == "YES" and fv <= GBM_NEUTRAL) or \
                   (side == "NO"  and fv >= GBM_NEUTRAL):
                    # IOC exit — apply slippage
                    exit_px     = max(0.01, sell_px - SLIPPAGE_C / 100.0)
                    exit_reason = "gbm_neutral"
                    break

        if exit_px is None:
            last    = max(by_min)
            c       = by_min[last]
            raw     = c["yes_bid"] if side == "YES" else (1.0 - c["yes_ask"])
            exit_px = max(0.01, raw - SLIPPAGE_C / 100.0)
            exit_reason = "time"

        n   = max(1, int(TRADE_SIZE_USD / entry_px))
        pnl = round(n * (exit_px - entry_px), 2)
        date = datetime.fromtimestamp(open_ts, tz=timezone.utc).strftime("%Y-%m-%d")

        trades.append({
            "date":   date,
            "side":   side,
            "entry":  round(entry_px * 100, 1),
            "exit":   round(exit_px  * 100, 1),
            "reason": exit_reason,
            "n":      n,
            "pnl":    pnl,
            "fv":     round(entry["fv"], 1),
        })

    if not trades:
        print("No trades found.")
        return

    # ── Day-by-day P&L ───────────────────────────────────────────────────────
    by_day: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        by_day[t["date"]].append(t)

    # Track drawdown at trade level for accuracy
    balance   = STARTING_BANKROLL
    peak      = STARTING_BANKROLL
    max_dd    = 0.0
    for t in trades:
        balance = round(balance + t["pnl"], 2)
        peak    = max(peak, balance)
        max_dd  = max(max_dd, peak - balance)

    # Build day rows with end-of-day balance
    balance   = STARTING_BANKROLL
    day_rows  = []
    for date in sorted(by_day):
        day_trades = by_day[date]
        day_pnl    = sum(t["pnl"] for t in day_trades)
        day_wins   = sum(1 for t in day_trades if t["pnl"] > 0)
        day_losses = len(day_trades) - day_wins
        balance    = round(balance + day_pnl, 2)
        day_rows.append({
            "date":    date,
            "trades":  len(day_trades),
            "wins":    day_wins,
            "losses":  day_losses,
            "pnl":     day_pnl,
            "balance": balance,
        })

    # ── Print day-by-day table ────────────────────────────────────────────────
    dates   = sorted(by_day)
    date_start = dates[0]
    date_end   = dates[-1]

    print(f"\n{'━'*72}")
    print(f"  BACKTEST  {date_start} → {date_end}  |  "
          f"${STARTING_BANKROLL:.0f} bankroll  |  ${TRADE_SIZE_USD:.0f}/trade")
    print(f"{'━'*72}")
    print(f"  {'Date':<12} {'Trades':>6} {'W/L':>6}  {'Day P&L':>9}  {'Balance':>9}  {'Bar'}")
    print(f"  {'─'*10} {'─'*6} {'─'*6}  {'─'*9}  {'─'*9}  {'─'*20}")

    for r in day_rows:
        bar_val = r["pnl"]
        if bar_val >= 0:
            bar = "█" * min(int(bar_val / 10), 20)
            bar_str = f"\033[32m+{bar}\033[0m"
        else:
            bar = "█" * min(int(abs(bar_val) / 10), 20)
            bar_str = f"\033[31m-{bar}\033[0m"
        sign = "+" if r["pnl"] >= 0 else ""
        print(f"  {r['date']:<12} {r['trades']:>6} {r['wins']:>3}W{r['losses']:>2}L"
              f"  {sign}${r['pnl']:>7.2f}  ${r['balance']:>8.2f}  {bar_str}")

    # ── Summary stats ─────────────────────────────────────────────────────────
    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    total_pnl   = sum(t["pnl"] for t in trades)
    losing_days = sum(1 for r in day_rows if r["pnl"] < 0)
    n_days      = len(day_rows)

    by_reason: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        by_reason[t["reason"]].append(t["pnl"])

    print(f"\n{'━'*72}")
    print(f"  SUMMARY")
    print(f"{'━'*72}")
    print(f"  Period          {date_start}  →  {date_end}  ({n_days} trading days)")
    print(f"  Total trades    {len(trades)}  ({len(trades)/n_days:.1f}/day)")
    print(f"  Win rate        {len(wins)/len(trades)*100:.1f}%  ({len(wins)}W / {len(losses)}L)")
    if wins:
        print(f"  Avg win         +${sum(t['pnl'] for t in wins)/len(wins):.2f}")
    if losses:
        avg_loss = sum(t['pnl'] for t in losses) / len(losses)
        print(f"  Avg loss        ${avg_loss:.2f}")
    if wins and losses:
        wr = sum(t['pnl'] for t in wins) / len(wins)
        lr = abs(sum(t['pnl'] for t in losses) / len(losses))
        print(f"  Win:Loss ratio  {wr/lr:.2f}x")
    print(f"  Total P&L       ${total_pnl:+.2f}")
    print(f"  Avg $/day       ${total_pnl/n_days:+.2f}")
    print(f"  Start balance   ${STARTING_BANKROLL:.2f}")
    print(f"  End balance     ${balance:.2f}")
    print(f"  Return          {(balance - STARTING_BANKROLL) / STARTING_BANKROLL * 100:+.1f}%")
    print(f"  Losing days     {losing_days} / {n_days}")
    print(f"  Max drawdown    ${max_dd:.2f}")
    print(f"\n  Exit breakdown:")
    for reason, pnls in sorted(by_reason.items()):
        w = sum(1 for p in pnls if p > 0)
        print(f"    {reason:14s}  {len(pnls):4d} trades  "
              f"{w/len(pnls)*100:.0f}% win  "
              f"avg ${sum(pnls)/len(pnls):+.2f}")
    print(f"\n  Note: {no_btc} windows skipped (no BTC price data in cache)")
    print(f"  Data covers {n_days} days — cache is capped at 60 days.")
    print(f"  For ~6 months set DAYS=180 in fetch_data.py and re-run it.\n")


if __name__ == "__main__":
    main()
