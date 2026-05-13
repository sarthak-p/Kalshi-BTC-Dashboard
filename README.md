# Kalshi BTC 15-Min Analysis Dashboard

Real-time market analysis dashboard for Kalshi BTC 15-minute binary markets. Monitors active contracts, runs a GBM fair-value model, and generates trade recommendations — but does **not** place orders.

## What it does

Each Kalshi `KXBTCD` contract is a binary: pays $1 if BTC closes at or above the window open price, $0 otherwise.

The bot connects to both Kalshi (orderbook + contract metadata) and Coinbase (live BTC price + 1-min candles), then continuously analyzes each 15-minute window using three signals:

**1. GBM Fair Value**
Uses a Geometric Brownian Motion model to estimate the probability that BTC closes above the window-open strike. Inputs: current BTC price, strike, time remaining, and a rolling 10-minute realized volatility. Output: `fair_value_yes_pct` (0–100). Updates every 50ms.

**2. BTC Momentum**
Measures the dollar move from the window open. If `|move| >= MOMENTUM_ENTRY_USD` ($30 default), it contributes a directional signal.

**3. Pre-Window Technicals (Coinbase, refreshed every 60s)**
Fetches 50 one-minute candles from Coinbase Exchange and computes:

| Indicator | Signal |
|---|---|
| RSI(14) < 40 | Bullish point |
| RSI(14) > 60 | Bearish point |
| BB(20) position < 0.4 (near lower band) | Bullish point |
| BB(20) position > 0.6 (near upper band) | Bearish point |

Two or more points in the same direction → bias. Requires 2 signals to avoid single-indicator noise.

**Recommendation**
When 2 or 3 signals agree on a direction, the dashboard shows: side (YES/NO), entry price (best ask or implied NO ask), confidence (signal_count / 3), and the basis for each signal.

**Monitoring conditions tracked**
The dashboard also continuously checks the analysis preconditions that would apply to a discretionary entry:
- `btc_move_ok` — BTC has moved ≥ $30 from the window open
- `price_in_range` — Kalshi mid is 60–85¢ (confirmed direction, room to run)
- `crossings_ok` — Kalshi mid has crossed 50¢ ≤ 2 times (steady, not choppy)
- `direction_ok` — ≥60% of recent Kalshi mid steps moved further from 50¢
- `bias_ok` — RSI/BB technicals agree with direction
- `phase` — `monitoring` (> 8 min left) / `entry_open` (2–8 min) / `too_late` / `closing`

## Quick Start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env — at minimum set KALSHI_ENV, KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_B64

python main.py
```

Dashboard: `http://127.0.0.1:8000`

The Kalshi credentials are needed only for orderbook data. No orders are placed.

## Kalshi Setup

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
5. Confirm `BTC_SERIES_TICKER` matches the current Kalshi series name (`KXBTCD` by default).

## Resolution vs. Model Accuracy

At each window close the bot computes resolution using its own live Coinbase price at the moment it detects expiry. This is **not** pulled from Kalshi's settlement API — if BTC is near the strike at close, the bot's resolved result may differ from Kalshi's actual settlement. Treat the tracked prediction accuracy as approximate.

Prediction outcomes (and accuracy stats) are persisted in `logs/predictions.csv` across sessions, and lifetime counts are stored in `logs/lifetime_stats.json`.

## Architecture

```text
main.py
  → EventLogger.flush_loop()       async CSV flush every 5s
  → StateManager.broadcast_loop()  WebSocket push to dashboard
  → KalshiFeed.run()               REST contract discovery + WS orderbook
  → BtcFeed.run()                  Coinbase BTC-USD reference price (WS)
  → Analyzer.run()
      _analysis_loop()             GBM model + analysis conditions (every 50ms)
      _bias_refresher()            Coinbase candle fetch + RSI/BB (every 60s)
      _window_resolver()           logs resolution vs. prediction (every 1s)
  → FastAPI/Uvicorn                dashboard server + WS broadcast
```

All components share a single `StateManager` in-memory hub. Feeds write into it, the analyzer reads from it, and the dashboard streams snapshots over WebSocket.

## Strike Price (BTC Window Open)

At contract discovery the bot resolves the strike in priority order:
1. Numeric fields from the Kalshi API (`floor_strike`, `cap_strike`, `strike`)
2. Regex parse of subtitle/title text (e.g. "Above $81,775.15")
3. Ticker suffix (e.g. `KXBTC15M-26MAY2016-T81775.15`)
4. Coinbase Exchange historical candle for the window-open timestamp
5. In-memory BTC price history, or falling back to the current live price

## Configuration Reference

| Variable | Default | Description |
|---|---|---|
| `KALSHI_ENV` | required | `demo` or `prod` |
| `KALSHI_API_KEY_ID` | empty | Kalshi API key ID |
| `KALSHI_PRIVATE_KEY_B64` | empty | Base64-encoded PEM private key |
| `BTC_SERIES_TICKER` | `KXBTCD` | Series ticker for contract auto-discovery |
| `BTC_SIGMA` | `0.80` | Fallback annualized volatility for GBM model (0–2.5) |
| `MAX_ENTRY_WINDOW_S` | `480` | Phase switches to `entry_open` when ≤ this many seconds remain |
| `MIN_ENTRY_WINDOW_S` | `120` | Phase switches to `too_late` when < this many seconds remain |
| `MIN_ENTRY_PRICE_CENTS` | `60` | Lower bound of the "in-range" price check (¢) |
| `MAX_ENTRY_PRICE_CENTS` | `85` | Upper bound of the "in-range" price check (¢) |
| `MOMENTUM_ENTRY_USD` | `30` | Min BTC move from window open to trigger momentum signal |
| `MAX_LINE_CROSSINGS` | `2` | Max times Kalshi mid may cross 50¢ before `crossings_ok` fails |
| `MIN_DIRECTION_CONSISTENCY` | `0.6` | Min fraction of recent Kalshi mid steps trending away from 50¢ |
| `KALSHI_MID_MAX_RANGE_CENTS` | `22` | Max ¢ range in last 60s before market is flagged erratic |
| `BIAS_GATE_ENABLED` | `true` | Flags `bias_ok=false` when RSI/BB contradicts BTC direction |
| `MIN_OPEN_INTEREST` | `500` | Thin-market flag threshold |
| `NEW_WINDOW_SETTLE_S` | `15` | Grace period after contract discovery before monitoring data counts |
| `MOMENTUM_THRESHOLD_USD` | `150` | BTC move in 10s that triggers a 30s velocity pause flag |
| `BINANCE_SYMBOL` | `BTC-USD` | Coinbase product ID for candle fetch (legacy env name) |
| `COINBASE_WS_URL` | Coinbase WS | Override Coinbase WebSocket URL |
| `DASHBOARD_HOST` | `127.0.0.1` | Dashboard bind host |
| `DASHBOARD_PORT` | `8000` | Dashboard port |

## Project Layout

```text
main.py                      asyncio entry point
config.py                    settings (reads .env)
state/state_manager.py       shared in-memory hub, dataclasses, WebSocket broadcast
feeds/kalshi_ws.py           Kalshi REST/WS feed, auth, contract discovery, orderbook
feeds/btc_feed.py            Coinbase BTC-USD live price (WebSocket)
strategy/scalper.py          Analyzer: GBM model, recommendation, window resolver
strategy/technicals.py       Coinbase candle fetch, RSI / ADX / BB computation
logger/event_logger.py       in-memory event buffer and async CSV flush
dashboard/app.py             FastAPI routes and WebSocket broadcast
dashboard/static/index.html  browser dashboard
logs/session_<ts>.csv        per-session event log
logs/predictions.csv         cross-session prediction outcomes
logs/lifetime_stats.json     persisted prediction accuracy counters
```

## Notes

- **No execution.** The bot is a read-only analysis tool. It subscribes to Kalshi's orderbook and Coinbase's price feed but never submits orders.
- **Resolution accuracy.** The bot computes its own resolution from the live Coinbase price at window expiry, not from Kalshi's settlement API. Near-the-money closes may be recorded incorrectly.
- **GBM is a model.** The fair-value estimate assumes log-normal price diffusion with realized vol from the last 10 minutes. It will misprice during trend continuation and low-vol regimes.
- **Fees.** Kalshi charges ~7% taker fee per trade leg. Each round trip costs ~14% of notional. Factor this into any profitability analysis.
