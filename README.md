# Kalshi BTC 15-Min Trading Bot

Automated trading bot for Kalshi BTC 15-minute binary markets. Uses a drift-adjusted GBM fair-value model as the primary signal, with BTC slope and RSI/BB technical bias as secondary context. Places simulated or real orders whenever the model has a recommendation.

Switch between paper (simulated) and live (real money) by changing one line in `.env`.

---

## How Kalshi BTC contracts work

Each `KXBTC15M` contract is a binary that pays **$1.00 if BTC closes at or above the window-open price**, $0.00 otherwise.

- **Buy YES at 40¢** → profit 60¢ if BTC closes up, lose 40¢ if not
- **Buy NO at 35¢** → profit 65¢ if BTC closes down, lose 35¢ if not

Kalshi settles using CF Benchmarks' BRTI (averaged over the 60 seconds before close) — not Coinbase spot. The bot queries the Kalshi settlement API for the official result.

---

## How the bot decides to trade

The executor follows the recommendation panel directly — both use the same signal hierarchy.

### Decision hierarchy

First signal that fires wins:

1. **GBM fair value** — if GBM < 35% → NO; if GBM > 70% → YES (asymmetric: YES requires stronger signal, data shows YES calls are weaker than NO calls)
2. **BTC slope** — if GBM neutral and |slope| > 0.30 $/s and GBM confirms (> 60% for YES, < 40% for NO) → slope drives
3. **Technical bias=down** — if GBM+slope both neutral and GBM < 40% → NO
4. **No signal** → WAIT / no trade

`bias=up` is **not** a standalone trigger — 50% accuracy in live data. It is shown in the dashboard basis as informational only.

### Trade lifecycle

- **Entry**: at the **8-minute mark** (`entry_open` phase), the model locks its recommendation. The executor places one trade per window based on that locked decision. Orders submit at ask+20¢ (buys) or bid−20¢ (sells) so they cross the spread immediately.
- **Hold**: position sits untouched until window close or a reversal
- **Reversal**: if the recommendation flips to the opposite side and holds for **60 seconds** (flip lock), the executor closes the existing position and enters the new side. Early break only if GBM strongly opposes the locked side (locked YES and GBM ≤ 35%, or locked NO and GBM ≥ 70%).
- **Settlement**: at window close the position is marked won/lost based on the official Kalshi result
- **Unfilled order guard**: if a buy or sell order is confirmed resting (not filled) after 3 seconds, it is cancelled on Kalshi and the position state rolls back — prevents holding two real positions with one in local state

---

## The GBM model

The GBM (Geometric Brownian Motion) model prices the probability that BTC closes above the window-open strike. It incorporates:

- Current BTC price vs strike
- Time remaining in the window
- Current BTC velocity (slope of recent price) — so a fast-rising BTC scores higher even if still below strike
- Volatility from Deribit DVOL (implied vol), or rolling realized vol as fallback

Example: BTC is -$80 from strike with 350s left but rising at $1.20/s. The market prices YES at 28¢. The drift-adjusted GBM says 52¢. The recommendation panel won't fire here (GBM not past 70%/35%), but if GBM crosses 70% the bot recommends YES and the executor buys.

---

## Technical bias (RSI/BB)

Fetched every **15 seconds between windows** (locked during active windows) from Coinbase Exchange 1-minute candles — last 35 candles (~35 minutes of data). Three indicators computed:

| Indicator | Bullish (`up`) | Bearish (`down`) |
|-----------|---------------|-----------------|
| RSI(14) | > 60 (strong uptrend → continuation UP) | < 40 (strong downtrend → continuation DOWN) |
| Bollinger Band position | > 0.6 (near upper band → uptrend) | < 0.4 (near lower band → downtrend) |
| ADX(14) | must be ≥ 15 for any signal to count | < 15 → all signals suppressed |

This uses **momentum-following** logic — live data showed:
- `bias=down` (RSI < 40): **73% accurate** → BTC in a downtrend keeps going down. Standalone NO trigger when GBM+slope are both neutral and GBM < 40%.
- `bias=up` (RSI > 60): **50% accurate** → coin flip. Informational only, never a trade trigger.

Mean-reversion interpretation (oversold = expect bounce) was tested and rejected — the "up" signal was 20% accurate, effectively backwards.

**ADX < 15 means no trend** — RSI and BB signals are unreliable in flat markets so bias is forced to neutral.

The bias is locked at window discovery and does not update mid-window. This prevents intra-window BTC moves from flipping the pre-window reading.

---

## Recommendation panel signals

| Signal | Source | Role |
|--------|--------|------|
| **GBM fair value** | Live BTC + DVOL | Primary — drives when GBM < 35% (NO) or > 70% (YES) |
| **BTC slope** | Coinbase spot price history | Secondary — drives when GBM neutral and \|slope\| > 0.30 $/s |
| **Technical bias=down** | Coinbase 1-min candles (35-candle lookback) | Tertiary — standalone NO trigger when GBM+slope neutral and GBM < 40% |
| **Technical bias=up** | Coinbase 1-min candles | Informational only — 50% accuracy, not a trade trigger |
| BTC momentum | Coinbase spot | Informational |
| CVD (order flow) | Coinbase trade stream | Informational |
| Funding rate | OKX perp | Informational |
| Orderbook imbalance | Kalshi order book | Informational |
| Kalshi mid momentum | Kalshi mid price history | Informational |

**GBM confidence gate**: technicals are suppressed entirely when GBM is below 20% or above 80% — at those extremes, BTC is so far from the strike that a general RSI/BB bounce signal is irrelevant.

The executor places a trade whenever the model produces a recommendation — there is no separate price-range or edge-gap gate blocking execution.

---

## Trading modes

Set `TRADING_MODE` in `.env`:

| Mode | Behaviour |
|------|-----------|
| `paper` | Simulated fills at current market price. P&L tracked in `logs/executor_bankroll.json`. |
| `live` | Real market orders via Kalshi REST API. Balance fetched from Kalshi at startup and after each settlement. |

**To reset the paper balance**, add `PAPER_BANKROLL_RESET=300.0` to `.env` and restart. The balance resets to that value. Remove the line (or set to `0`) on subsequent restarts to resume normal persistence.

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
      _analysis_loop()              GBM fair value + recommendation (every 50 ms)
      _bias_refresher()             RSI/BB (between windows only) + DVOL + OKX basis/funding (every 15 s)
      _window_resolver()            settlement + accuracy tracking (every 1 s)
  → Executor
      maybe_trade()                 enter/reverse based on locked model recommendation
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
| `BANKROLL` | `250.00` | Starting bankroll for model accuracy tracking |
| `PAPER_BANKROLL_RESET` | `0.0` | Set to a positive value to reset paper balance on next startup, then remove |
| `BTC_SIGMA` | `0.80` | Fallback annualized vol for GBM when DVOL unavailable |
| `MOMENTUM_ENTRY_USD` | `20.0` | Min BTC move from strike shown as "bullish/bearish" in signal panel |
| `BTC_SLOPE_SIGNAL_THRESHOLD` | `0.30` | Min \|slope\| in $/s for slope signal to fire (0.30 $/s ≈ $18/min) |
| `MIN_COMMITMENT_RATE` | `0.08` | Warning threshold: `\|BTC move\| / tau` in $/s (shown as ⚠, does not block) |
| `MIN_GBM_MARKET_GAP_CENTS` | `8.0` | Warning threshold: GBM vs Kalshi mid gap (shown as ⚠ in basis, does not block) |
| `MIN_ENTRY_PRICE_CENTS` | `30.0` | Used in dashboard phase indicator — no longer blocks execution |
| `MAX_ENTRY_PRICE_CENTS` | `85.0` | Used in dashboard phase indicator — no longer blocks execution |
| `MAX_ENTRY_WINDOW_S` | `480.0` | Entry window indicator threshold (seconds remaining) — 8-min mark |
| `MIN_ENTRY_WINDOW_S` | `120.0` | "Too late" threshold (seconds remaining) |
| `MOMENTUM_THRESHOLD_USD` | `150.0` | BTC move in 10 s that triggers a 30-second velocity-pause flag |
| `NEW_WINDOW_SETTLE_S` | `15.0` | Grace period after contract discovery before monitoring data counts |
| `MIN_OPEN_INTEREST` | `500` | Thin-market flag threshold (contracts) |
| `MIN_ADX_THRESHOLD` | `15.0` | ADX below this forces technical bias to neutral |
| `BINANCE_SYMBOL` | `BTC-USD` | Coinbase product ID for candle fetch |
| `BINANCE_KLINES_INTERVAL` | `60` | Candle granularity in seconds (Coinbase supports: 60, 300, 900, 3600) |
| `DASHBOARD_HOST` | `127.0.0.1` | Dashboard bind host |
| `DASHBOARD_PORT` | `8000` | Dashboard port |

---

## Logged data

| File | Contents |
|------|----------|
| `logs/session_<ts>.csv` | Every analysis event this session (recommendations, fills, errors) |
| `logs/predictions.csv` | Cross-session prediction outcomes with resolution and model accuracy |
| `logs/technicals_discovery.csv` | Technical bias at window discovery vs actual resolution — used to evaluate whether RSI/BB has genuine predictive value |
| `logs/lifetime_stats.json` | Persisted prediction accuracy counters across all sessions |
| `logs/bankroll.json` | Model accuracy bankroll (hypothetical P&L from every prediction) |
| `logs/executor_bankroll.json` | Bot bankroll (actual paper/live trade P&L only) |

---

## Notes

- **Fees.** Kalshi taker fees ≈ 7% × p × (1−p) per contract. At 40¢ entry, round-trip taker cost is ~1.7¢ per contract. Not deducted from sizing — factor into profitability analysis.
- **Settlement accuracy.** Queries Kalshi's API for the official BRTI-based result. Falls back to a Coinbase-price estimate if the API doesn't return within 2 minutes, tagged `[estimated]`.
- **GBM sigma source.** Uses Deribit DVOL (implied vol) when available. Falls back to rolling 10-minute realized vol from tick data.
- **Two bankrolls.** The model bankroll (`logs/bankroll.json`) tracks hypothetical P&L from every directional prediction. The executor bankroll (`logs/executor_bankroll.json`) tracks only actual trades placed. They diverge because the model predicts every window but only fires a recommendation when GBM or slope thresholds are met.
- **Technicals edge.** The `technicals_discovery.csv` file accumulates discovery-time bias readings vs resolutions. Meaningful accuracy assessment requires 30–50 directional rows.
- **Unified strategy.** The executor follows the recommendation panel directly — both use GBM-primary (< 35% → NO, > 70% → YES) with slope as a fallback. The executor places a trade for every recommendation the model locks at the 8-minute mark, with no additional price-range or edge-gap filters.
- **Position sizing.** Flat $5 per trade regardless of bankroll. At 40¢ entry this buys ~12 contracts; at 60¢ entry ~8 contracts.
