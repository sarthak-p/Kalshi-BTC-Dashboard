# Kalshi BTC 15-Min Trading Bot

Automated trading bot for Kalshi BTC 15-minute binary markets. Runs a drift-adjusted GBM fair-value model, votes across seven independent signals, and places real or simulated orders when a genuine edge over the market price is detected.

Switch between paper (simulated) and live (real money) by changing one line in `.env`.

---

## How Kalshi BTC contracts work

Each `KXBTC15M` contract is a binary that pays **$1.00 if BTC closes at or above the window-open price**, $0.00 otherwise.

- **Buy YES at 40¢** → profit 60¢ if BTC closes up, lose 40¢ if not
- **Buy NO at 35¢** → profit 65¢ if BTC closes down, lose 35¢ if not

Kalshi settles using CF Benchmarks' BRTI (averaged over the 60 seconds before close) — not Coinbase spot. The bot queries the Kalshi settlement API for the official result.

Once a position is placed it rides to settlement — there is no liquid secondary market to exit into. The stop-loss feature (see below) is the only mid-window risk management available.

---

## The edge

Most Kalshi participants price contracts based on where BTC **is right now**. This model prices them based on where BTC **is going** — by incorporating BTC's current velocity (slope) into the GBM fair value.

Example: BTC is -$80 from strike with 350s left, but rising at $1.20/s. The crowd sees -$80 and prices YES at 28¢. The drift-adjusted GBM says YES is worth 52¢ (BTC will cross the strike in ~67 seconds at this rate). That 24¢ mispricing gap is the edge. The bot buys YES at 28¢.

Two hard filters enforce that a real edge exists before any order is placed:

1. **Commitment rate** — `|BTC move| / seconds_remaining ≥ 0.15 $/s`. Prevents trading on small, undecided moves where the market can easily reverse.
2. **GBM-market gap** — our fair value must differ from the Kalshi mid by ≥ 8¢. If the market is already pricing what our model sees, there is nothing to exploit.
3. **Entry price cap** — never pay more than 65¢ for any contract. Above 65¢ you risk 65¢+ to win 35¢ or less; one reversal wipes multiple potential gains.

---

## Signal system

The bot votes across seven signals. Four are **core votes**; three are **confirmatory** (can only strengthen the leading side, never create or flip a recommendation).

| # | Signal | Source | Threshold |
|---|--------|--------|-----------|
| 1 | **GBM fair value** | Live BTC + DVOL | > 58% → YES, < 42% → NO |
| 2 | **BTC momentum** | Coinbase spot | > $44 move → bullish; < −$44 → bearish |
| 3 | **Technical bias** | Coinbase 1-min candles | RSI/BB consensus, neutral if ADX < 20 |
| 4 | **CVD (order flow)** | Coinbase trade stream | Net buy volume > 8% of window total |
| 5 | **Funding rate** *(bonus)* | OKX perp | Crowded longs → NO lean; crowded shorts → YES lean |
| 6 | **Orderbook imbalance** *(bonus)* | Kalshi order book | Bid-heavy → YES; ask-heavy → NO |
| 7 | **Kalshi mid momentum** *(bonus)* | Kalshi mid price | Rising slope > 0.05¢/s → YES |

**Vote threshold**: ≥ 3 core signals aligned in trending markets (ADX ≥ 20), all 4 in choppy markets (ADX < 20). Technicals only count when they agree with whatever GBM + momentum already say — they cannot flip the direction on their own.

**60-second flip lock**: once a side is recommended, it is held for 60 seconds to prevent oscillation. The lock releases immediately if GBM drops to ≤ 10% (floor against YES lock) or ≥ 90% (floor against NO lock).

---

## When the bot places an order

All conditions must be true simultaneously on the same analysis tick (runs every 50 ms):

1. Phase is **`entry_open`** — between 240 s and 600 s remaining (4–10 min before close)
2. The 4-signal vote produces a clear side (YES or NO)
3. Commitment rate filter passes (`|move| / tau ≥ 0.15 $/s`)
4. GBM-market gap filter passes (`|fv − mid| ≥ 8¢`)
5. Entry price is within 8–65¢
6. Kelly sizing produces ≥ 1 contract
7. This contract has not already been traded this window (one order per 15-min window)

**Kelly position sizing** (half-Kelly, 15% bankroll cap):
```
kelly_pct = (p × profit_per_dollar − (1−p)) / profit_per_dollar
```
where `p` is the session model accuracy (falls back to 0.91 if no session data yet). Half-Kelly is used to reduce variance. The position is capped at 15% of the executor bankroll per trade.

---

## Stop-loss

Because positions cannot be exited freely on Kalshi, the bot monitors open positions every tick and will sell mid-window if GBM strongly flips against the trade.

**Stop-loss fires when all four guards pass:**

| Guard | Threshold |
|-------|-----------|
| Position is open | status == open |
| GBM strongly opposes | ≤ 15% YES for a YES position; ≥ 85% YES for a NO position |
| Time remaining | ≥ 180 s (3 min) — not worth the spread cost with less time |
| Sell price floor | ≥ 8¢ — don't exit into a one-sided book for scraps |

One stop-loss is allowed per contract per session. After a stop-loss, the trade lock is cleared and the bot can immediately re-enter in the opposite direction — but only if the opposite trade passes all the same edge filters (commitment rate, GBM gap, price cap, Kelly sizing).

Event log example:
```
🛑 STOP-LOSS YES  42 × 75.0¢ → 13.0¢  PnL -$26.04  balance $185.95
📄 PAPER NO  28 contracts @ 35.0¢  cost $9.80  balance $185.95
```

---

## Trading modes

Set `TRADING_MODE` in `.env`:

| Mode | Behaviour |
|------|-----------|
| `paper` | Simulated fills at the current ask. No real orders. P&L tracked in `logs/executor_bankroll.json`. |
| `live` | Real market orders via Kalshi REST API. Balance fetched from Kalshi at startup and after each settlement. |

In live mode the executor bankroll is always the real Kalshi available balance — Kelly sizing automatically reflects your actual account.

---

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env — set KALSHI_ENV, KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_B64, TRADING_MODE
# Set TRADING_MODE=paper to run without real money first

python main.py
```

Dashboard: `http://127.0.0.1:8000`

---

## Kalshi API setup

1. Create an API key from the Kalshi dashboard.
2. Generate an RSA key pair:
   ```bash
   openssl genrsa -out private.pem 2048
   openssl rsa -in private.pem -pubout -out public.pem
   ```
3. Upload `public.pem` to Kalshi.
4. Base64-encode the private key for `.env`:
   ```bash
   base64 -i private.pem | tr -d '\n'
   ```
5. Confirm `BTC_SERIES_TICKER` matches the current Kalshi series (`KXBTC15M`).

---

## Architecture

```
main.py
  → EventLogger.flush_loop()        async CSV flush every 5 s
  → StateManager.broadcast_loop()   WebSocket push to dashboard on every state change
  → KalshiFeed.run()                REST contract discovery + WebSocket orderbook
  → BtcFeed.run()                   Coinbase BTC-USD price (ticker) + CVD (trade stream)
  → Analyzer.run()
      _analysis_loop()              GBM + 7-signal vote + edge filters (every 50 ms)
      _bias_refresher()             RSI/BB + DVOL + OKX basis/funding (every 60 s)
      _window_resolver()            settlement + accuracy tracking (every 1 s)
  → Executor
      maybe_stop_loss()             mid-window risk cut on GBM flip
      maybe_trade()                 order placement on edge detection
  → FastAPI/Uvicorn                 dashboard HTTP + WebSocket server
```

All components share a single `StateManager` in-memory hub. Feeds write into it, the analyzer reads from it, and the dashboard streams snapshots over WebSocket every time state changes.

---

## Strike price resolution

At contract discovery the bot resolves the BTC window-open strike in priority order:

1. Numeric fields from the Kalshi API (`floor_strike`, `cap_strike`, `strike`)
2. Regex parse of subtitle/title text (e.g. "Above $81,775.15")
3. Ticker suffix (e.g. `KXBTC15M-26MAY2016-T81775.15`)
4. Coinbase Exchange historical candle for the window-open timestamp
5. In-memory BTC price history, or the current live price as a last resort

---

## Configuration reference

| Variable | Default | Description |
|----------|---------|-------------|
| `KALSHI_ENV` | required | `demo` or `prod` |
| `KALSHI_API_KEY_ID` | — | Kalshi API key ID |
| `KALSHI_PRIVATE_KEY_B64` | — | Base64-encoded PEM private key |
| `BTC_SERIES_TICKER` | `KXBTC15M` | Series ticker for contract auto-discovery |
| `TRADING_MODE` | `paper` | `paper` (simulated) or `live` (real orders) |
| `BANKROLL` | `250.00` | Starting bankroll in dollars (paper mode fallback if API fetch fails) |
| `BTC_SIGMA` | `0.80` | Fallback annualized vol for GBM when DVOL unavailable |
| `MOMENTUM_ENTRY_USD` | `40.0` | Min BTC move from strike — 1.1× hysteresis applied to avoid threshold jitter |
| `MIN_COMMITMENT_RATE` | `0.15` | Min `\|BTC move\| / tau` in $/s — filters undecided moves early in the window |
| `MIN_GBM_MARKET_GAP_CENTS` | `8.0` | Min gap between GBM fair value and Kalshi mid — ensures a real mispricing edge |
| `MIN_ENTRY_PRICE_CENTS` | `8.0` | Hard lower bound on entry price — below this the market is near-certain |
| `MAX_ENTRY_PRICE_CENTS` | `65.0` | Hard upper bound — above this you risk more than you can win |
| `MAX_ENTRY_WINDOW_S` | `600.0` | Entry window opens when ≤ this many seconds remain |
| `MIN_ENTRY_WINDOW_S` | `240.0` | Entry window closes when < this many seconds remain |
| `MOMENTUM_THRESHOLD_USD` | `150.0` | BTC move in 10 s that triggers a 30-second velocity-pause flag |
| `NEW_WINDOW_SETTLE_S` | `15.0` | Grace period after contract discovery before monitoring data counts |
| `MIN_OPEN_INTEREST` | `500` | Thin-market flag threshold (contracts) |
| `MAX_LINE_CROSSINGS` | `2` | Max times Kalshi mid may cross 50¢ — displayed in Analysis Conditions |
| `MIN_DIRECTION_CONSISTENCY` | `0.6` | Min fraction of Kalshi mid steps trending away from 50¢ — informational |
| `BINANCE_SYMBOL` | `BTC-USD` | Coinbase product ID for candle fetch |
| `DASHBOARD_HOST` | `127.0.0.1` | Dashboard bind host |
| `DASHBOARD_PORT` | `8000` | Dashboard port |

---

## Logged data

| File | Contents |
|------|----------|
| `logs/session_<ts>.csv` | Every analysis event this session (recommendation changes, errors, fills) |
| `logs/predictions.csv` | Cross-session prediction outcomes with entry price, bet size, P&L |
| `logs/lifetime_stats.json` | Persisted prediction accuracy counters across all sessions |
| `logs/bankroll.json` | Model accuracy bankroll (tracks hypothetical P&L from every prediction) |
| `logs/executor_bankroll.json` | Real bot bankroll (tracks actual paper/live trade P&L only) |

---

## Notes

- **Fees.** Kalshi taker fees ≈ 7% × p × (1−p) per contract, where p is the price. At 40¢ entry, round-trip taker cost is ~1.7¢ per contract. The bot does not currently deduct fees from Kelly sizing — factor this into any profitability analysis.
- **Settlement accuracy.** The bot queries Kalshi's API for the official BRTI-based result. Falls back to a Coinbase-price estimate if the API doesn't return within 2 minutes, tagged `[estimated]`.
- **GBM sigma source.** Uses Deribit DVOL (implied vol) when available — more stable than 10-minute realized vol from tick data. Falls back to rolling realized vol.
- **External data.** DVOL from Deribit public API (no auth). Basis and funding from OKX public API (no auth). Both are geo-accessible from the US.
- **Two bankrolls.** The model bankroll (`logs/bankroll.json`) tracks hypothetical P&L from every directional prediction. The executor bankroll (`logs/executor_bankroll.json`) tracks only actual trades placed. They diverge because the model predicts every window; the bot only trades when edge filters pass.
