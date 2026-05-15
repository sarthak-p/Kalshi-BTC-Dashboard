# Kalshi BTC 15-Min Trading Bot

Automated trading bot for Kalshi BTC 15-minute binary markets. Uses a drift-adjusted GBM fair-value model combined with RSI/BB technical bias to decide direction, and places simulated or real orders when both signals agree.

Switch between paper (simulated) and live (real money) by changing one line in `.env`.

---

## How Kalshi BTC contracts work

Each `KXBTC15M` contract is a binary that pays **$1.00 if BTC closes at or above the window-open price**, $0.00 otherwise.

- **Buy YES at 40¢** → profit 60¢ if BTC closes up, lose 40¢ if not
- **Buy NO at 35¢** → profit 65¢ if BTC closes down, lose 35¢ if not

Kalshi settles using CF Benchmarks' BRTI (averaged over the 60 seconds before close) — not Coinbase spot. The bot queries the Kalshi settlement API for the official result.

---

## How the bot decides to trade

The executor uses a simple two-signal rule:

1. **Technical bias** (RSI/BB from Coinbase 1-min candles) must have a directional view — `up` or `down`. If ADX < 20 the market is choppy and RSI/BB signals are suppressed → bias stays neutral → no trade.
2. **GBM fair value** must agree with the bias — GBM > 55% for YES, < 45% for NO.

When both agree, the bot enters that side at the current market price using **10% of the executor bankroll**. When bias switches direction mid-window, the current position is closed at market and a new one opened in the opposite direction.

There is no stop-loss — the bias reversal mechanism handles mid-window risk.

---

## The GBM model

The GBM (Geometric Brownian Motion) model prices the probability that BTC closes above the window-open strike. It incorporates:

- Current BTC price vs strike
- Time remaining in the window
- Current BTC velocity (slope of recent price) — so a fast-rising BTC scores higher even if still below strike
- Volatility from Deribit DVOL (implied vol), or rolling realized vol as fallback

Example: BTC is -$80 from strike with 350s left but rising at $1.20/s. The market prices YES at 28¢. The drift-adjusted GBM says 52¢. The bot sees that gap and, if technicals also say bullish, buys YES.

---

## Technical bias (RSI/BB)

Fetched every **15 seconds** from Coinbase Exchange 1-minute candles (no auth required). Three indicators are computed:

| Indicator | Bullish condition | Bearish condition |
|-----------|------------------|------------------|
| RSI(14) | > 60 | < 40 |
| Bollinger Band position | > 0.6 (near upper band) | < 0.4 (near lower band) |
| ADX(14) | must be ≥ 20 for any signal to count | < 20 → all signals suppressed |

**ADX < 20 means the market is choppy** — RSI and BB give false signals in sideways markets so the bias is forced to neutral regardless of RSI/BB values.

Bias logic: if at least one of RSI or BB fires in the same direction (and ADX ≥ 20), that direction wins. If they conflict or both are neutral, bias = neutral.

---

## Recommendation panel

The dashboard recommendation fires when GBM and technicals agree. Supporting signals are shown as informational context:

| Signal | Source | Role |
|--------|--------|------|
| **GBM fair value** | Live BTC + DVOL | Primary — must agree with technicals |
| **Technical bias** | Coinbase 1-min candles | Primary — must agree with GBM |
| BTC momentum | Coinbase spot | Informational |
| CVD (order flow) | Coinbase trade stream | Informational |
| Funding rate | OKX perp | Informational |
| Orderbook imbalance | Kalshi order book | Informational |
| Kalshi mid momentum | Kalshi mid price history | Informational |

**Hard gate** (only thing that blocks the recommendation): entry price must be within 8–65¢. Below 8¢ the market is near-certain with no value to capture. Above 65¢ you risk more than you can win.

Commitment rate and GBM-market gap are shown as **⚠ warnings** in the basis panel but no longer block the recommendation — they are informational context for the trader.

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
      _bias_refresher()             RSI/BB + DVOL + OKX basis/funding (every 15 s)
      _window_resolver()            settlement + accuracy tracking (every 1 s)
  → Executor
      maybe_trade()                 enter/reverse based on bias+GBM agreement
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
| `MIN_COMMITMENT_RATE` | `0.08` | Warning threshold: `\|BTC move\| / tau` in $/s (shown as ⚠, does not block) |
| `MIN_GBM_MARKET_GAP_CENTS` | `8.0` | Warning threshold: GBM vs Kalshi mid gap (shown as ⚠, does not block) |
| `MIN_ENTRY_PRICE_CENTS` | `8.0` | Hard lower bound on entry price — below this market is near-certain |
| `MAX_ENTRY_PRICE_CENTS` | `65.0` | Hard upper bound — above this you risk more than you can win |
| `MAX_ENTRY_WINDOW_S` | `480.0` | Entry window opens when ≤ this many seconds remain |
| `MIN_ENTRY_WINDOW_S` | `120.0` | Entry window closes when < this many seconds remain |
| `MOMENTUM_THRESHOLD_USD` | `150.0` | BTC move in 10 s that triggers a 30-second velocity-pause flag |
| `NEW_WINDOW_SETTLE_S` | `15.0` | Grace period after contract discovery before monitoring data counts |
| `MIN_OPEN_INTEREST` | `500` | Thin-market flag threshold (contracts) |
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
- **Two bankrolls.** The model bankroll (`logs/bankroll.json`) tracks hypothetical P&L from every directional prediction. The executor bankroll (`logs/executor_bankroll.json`) tracks only actual trades placed. They diverge because the model predicts every window but the bot only trades when bias and GBM agree.
- **Technicals edge.** The `technicals_discovery.csv` file is accumulating discovery-time bias readings vs resolutions. Meaningful accuracy assessment requires 30–50 directional rows. Resolution-time analysis (where RSI reflects the price move that already happened) is circular and should not be used to evaluate predictive accuracy.
