# Kalshi BTC 15-Min Trading Bot

Automated trading bot for Kalshi BTC 15-minute binary markets. Uses a drift-adjusted GBM fair-value model as the primary signal, with BTC slope and RSI/BB technical bias as secondary context. Places simulated or real orders when the model has a clear edge.

Switch between paper (simulated) and live (real money) by changing one line in `.env`.

---

## How Kalshi BTC contracts work

Each `KXBTC15M` contract is a binary that pays **$1.00 if BTC closes at or above the window-open price**, $0.00 otherwise.

- **Buy YES at 40¢** → profit 60¢ if BTC closes up, lose 40¢ if not
- **Buy NO at 35¢** → profit 65¢ if BTC closes down, lose 35¢ if not

Kalshi settles using CF Benchmarks' BRTI (averaged over the 60 seconds before close) — not Coinbase spot. The bot queries the Kalshi settlement API for the official result.

---

## How the bot decides to trade

The bot has two independent decision-makers that run simultaneously:

### Executor (places actual trades)

1. **Technical bias** (RSI/BB from the last 20 Coinbase 1-min candles, locked at window open) must have a directional view — `up` or `down`. ADX < 15 forces neutral → no trade.
2. **GBM fair value** must broadly agree — GBM > 55% for YES, GBM < 45% for NO.

When both agree, the bot enters at market price using **10% of the executor bankroll**. When the bias switches direction mid-window, the current position is closed and a new one is opened in the opposite direction.

### Recommendation panel (dashboard display)

Decision hierarchy — first signal that fires wins:

1. **GBM fair value** — if GBM < 35% → recommend NO; if GBM > 65% → recommend YES
2. **BTC slope** — if GBM is neutral and slope is strong enough (> 0.30 $/s), slope drives the recommendation
3. **No signal** → WAIT

Technical bias is **informational only** in the recommendation panel — it is shown in the basis and counted in the signal score, but cannot block a GBM or slope recommendation. This is intentional: the 81% RSI/BB accuracy was measured from a small in-sample dataset and is likely overfit; GBM (82% over 99 windows) is more validated.

---

## The GBM model

The GBM (Geometric Brownian Motion) model prices the probability that BTC closes above the window-open strike. It incorporates:

- Current BTC price vs strike
- Time remaining in the window
- Current BTC velocity (slope of recent price) — so a fast-rising BTC scores higher even if still below strike
- Volatility from Deribit DVOL (implied vol), or rolling realized vol as fallback

Example: BTC is -$80 from strike with 350s left but rising at $1.20/s. The market prices YES at 28¢. The drift-adjusted GBM says 52¢. The recommendation panel won't fire here (GBM not past 65%/35%), but if GBM crosses 65% the bot recommends YES.

---

## Technical bias (RSI/BB)

Fetched every **15 seconds between windows** (locked during active windows) from Coinbase Exchange 1-minute candles — last 20 candles (~20 minutes of data). Three indicators computed:

| Indicator | Bullish condition | Bearish condition |
|-----------|------------------|------------------|
| RSI(14) | < 40 (oversold → expect bounce up) | > 60 (overbought → expect reversal down) |
| Bollinger Band position | < 0.4 (near lower band → oversold) | > 0.6 (near upper band → overbought) |
| ADX(14) | must be ≥ 15 for any signal to count | < 15 → all signals suppressed |

This uses **mean-reversion** logic — oversold conditions predict a bounce UP in the next 15 minutes. Confirmed on historical data; the opposite (momentum-following) interpretation tested at 19% accuracy.

**ADX < 15 means no trend** — RSI and BB signals are unreliable in flat/choppy markets so bias is forced to neutral.

The bias is locked at window discovery and does not update mid-window. This prevents intra-window BTC crashes from flipping the bias on a bounce signal rather than a genuine directional change.

---

## Recommendation panel signals

| Signal | Source | Role |
|--------|--------|------|
| **GBM fair value** | Live BTC + DVOL | Primary — drives recommendation when < 35% or > 65% |
| **BTC slope** | Coinbase spot price history | Fallback — drives when GBM neutral and slope > 0.30 $/s |
| **Technical bias** | Coinbase 1-min candles (20-candle lookback) | Informational — shown in basis, cannot block GBM/slope |
| BTC momentum | Coinbase spot | Informational |
| CVD (order flow) | Coinbase trade stream | Informational |
| Funding rate | OKX perp | Informational |
| Orderbook imbalance | Kalshi order book | Informational |
| Kalshi mid momentum | Kalshi mid price history | Informational |

**Hard gate**: entry price must be ≥ 30¢. Below this the market is pricing near-certainty and there is no value to capture.

**GBM confidence gate**: technicals are suppressed entirely (not shown as conflicting) when GBM is below 20% or above 80% — at those extremes, BTC is so far from the strike that a general RSI/BB bounce signal is irrelevant.

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
      maybe_trade()                 enter/reverse based on pre-window bias + GBM agreement
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
| `MIN_GBM_MARKET_GAP_CENTS` | `8.0` | Warning threshold: GBM vs Kalshi mid gap (shown as ⚠, does not block) |
| `MIN_ENTRY_PRICE_CENTS` | `30.0` | Hard lower bound on entry price — below this market is near-certain |
| `MAX_ENTRY_PRICE_CENTS` | `85.0` | Upper bound (executor only — recommendation panel does not enforce this) |
| `MAX_ENTRY_WINDOW_S` | `480.0` | Entry window indicator threshold (seconds remaining) |
| `MIN_ENTRY_WINDOW_S` | `120.0` | Minimum entry window threshold (seconds remaining) |
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
- **Two bankrolls.** The model bankroll (`logs/bankroll.json`) tracks hypothetical P&L from every directional prediction. The executor bankroll (`logs/executor_bankroll.json`) tracks only actual trades placed. They diverge because the model predicts every window but the bot only trades when bias and GBM agree.
- **Technicals edge.** The `technicals_discovery.csv` file accumulates discovery-time bias readings vs resolutions. Meaningful accuracy assessment requires 30–50 directional rows. The 20-candle lookback (~20 minutes) reflects the immediate pre-window momentum — the prior 100-candle (~90 minute) lookback was too slow to capture recent directional shifts.
- **Two strategies.** The executor and recommendation panel use different primary signals and thresholds on purpose. The executor's pre-window bias + GBM confirmation has been empirically profitable. The recommendation panel's GBM-primary approach is more conservative (requires GBM < 35% or > 65%). Do not merge them until side-by-side accuracy data (50+ windows) justifies it.
