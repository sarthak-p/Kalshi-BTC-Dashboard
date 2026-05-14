# Kalshi BTC 15-Min Analysis Dashboard

Real-time market analysis dashboard for Kalshi BTC 15-minute binary markets. Monitors active contracts, runs a GBM fair-value model, and generates trade recommendations — but does **not** place orders.

## What it does

Each Kalshi `KXBTCD` contract is a binary: pays $1 if BTC closes at or above the window open price, $0 otherwise.

The bot connects to Kalshi (orderbook + contract metadata), Coinbase (live BTC price, 1-min candles, and trade flow), Deribit (implied volatility index), and OKX (futures basis + funding rate), then continuously analyzes each 15-minute window using seven signals — four core votes and three confirmatory:

**1. GBM Fair Value**
Uses a Geometric Brownian Motion model to estimate the probability that BTC closes above the window-open strike. Inputs: current BTC price, strike, time remaining, and **Deribit DVOL** (implied volatility index — more stable than realized vol). Output: `fair_value_yes_pct` (0–100). Updates every 50ms. Falls back to 10-minute rolling realized vol if DVOL is unavailable.

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
| ADX(14) < 20 | Forces bias to neutral regardless of RSI/BB |

Two or more points in the same direction → bias, unless ADX < 20 (choppy market), in which case technicals return neutral regardless.

**4. CVD — Cumulative Volume Delta (Coinbase trade stream)**
Tracks every Coinbase spot trade during the window. Buyer-initiated trades (hitting the ask) add to CVD; seller-initiated trades (hitting the bid) subtract. If net buying exceeds 8% of total window volume → bullish signal. If net selling exceeds 8% → bearish signal. Resets at each new window open.

CVD distinguishes confirmed momentum (CVD and price agree) from absorption (divergence). Selling while price rises signals a large buyer absorbing retail flow — treated as bullish, not neutral. Buying while price falls signals distribution — bearish.

**5. Funding Rate** — confirmatory only. Crowded longs (>0.01%) → bearish lean. Crowded shorts (<-0.01%) → bullish lean.

**6. Orderbook Imbalance** — confirmatory only. Bid-heavy (>0.20) → YES. Ask-heavy (<-0.20) → NO.

**7. Kalshi Mid Momentum** — confirmatory only. 5-min slope >0.05¢/s → YES. <-0.05¢/s → NO.

**Recommendation — the one signal to act on**
Requires **3 of 4 core signals** in trending markets (ADX ≥ 20), **4 of 4** in choppy. Bonus signals strengthen conviction only — cannot create or flip a recommendation. Flips suppressed for 60 seconds. Suppressed entirely if line crossings exceed MAX_LINE_CROSSINGS. In choppy conditions ADX also suppresses the technicals signal, effectively requiring GBM, BTC momentum, and CVD to align before firing. Shows: side, entry price (best ask or implied NO ask), signal count (X/4), and the reason each signal voted.

**Only enter when the Recommendation fires AND the phase shows `ENTRY OPEN`.** During `MONITORING` (> 8 min left) the signals are forming — do not act. During `TOO LATE` (< 2 min) it's too late to enter with meaningful upside.

**Market Context (informational, not in the vote)**
Refreshed every 60s alongside technicals:
- **DVOL** — Deribit BTC implied vol index. Lower = calmer market, higher = wider expected swings. Used as GBM sigma input.
- **Futures Basis** — OKX perp mark price vs. index price. Positive (contango) = leveraged market leaning bullish. Negative (backwardation) = leaning bearish.
- **Funding Rate** — OKX perp funding. Positive = longs paying shorts (market crowded long, slight bearish lean). Negative = shorts paying longs (bearish crowding, slight bullish lean).

**Monitoring conditions tracked**
The Analysis Conditions panel shows the entry preconditions in real time:
- `btc_move_ok` — BTC has moved ≥ $30 from the window open
- `price_in_range` — entry price is 60–85¢ (confirmed direction, room to run)
- `crossings_ok` — Kalshi mid has crossed 50¢ ≤ 2 times (steady, not choppy)
- `direction_ok` — ≥60% of recent Kalshi mid steps moved further from 50¢
- `bias_ok` — RSI/BB technicals agree with BTC direction
- `phase` — `monitoring` (> 8 min left) / `entry_open` (2–8 min) / `too_late` / `closing`

## How to use it

1. Open the dashboard at `http://127.0.0.1:8000` once the bot is running.
2. Watch the **Window Phase** in Market Conditions. Do nothing during `MONITORING`.
3. When phase flips to **ENTRY OPEN**, look at the **Recommendation** box.
   - `BUY YES` or `BUY NO` with 3/4 or 4/4 signals → enter that side.
   - `WAIT` → signals are split, skip this window.
4. Optional confirmation: glance at **CVD**. If it agrees with the recommendation (green for YES, red for NO), setup is stronger. If it conflicts, it's a weaker signal.
5. After entering, stop watching. The signals will keep updating — that's noise once you're in. Check the resolution after window close.

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

At each window close the bot queries `GET /markets/{ticker}` on the Kalshi REST API for the official settlement result. Kalshi resolves using CF Benchmarks' Bitcoin Real Time Index (BRTI) — averaged over the 60 seconds before close — not Coinbase spot price. With `settlement_timer_seconds=1`, the result is typically available within seconds of close.

The bot polls every 5 seconds for up to 2 minutes. If Kalshi doesn't return a result in that window, it falls back to estimating from the live Coinbase price. Each logged resolution is tagged `[Kalshi]` or `[estimated]`.

Prediction outcomes are persisted in `logs/predictions.csv` across sessions, and lifetime accuracy counts in `logs/lifetime_stats.json` (`pred_total`, `pred_correct`).

## Architecture

```text
main.py
  → EventLogger.flush_loop()       async CSV flush every 5s
  → StateManager.broadcast_loop()  WebSocket push to dashboard
  → KalshiFeed.run()               REST contract discovery + WS orderbook
  → BtcFeed.run()                  Coinbase BTC-USD price (WS ticker) + CVD (WS trades)
  → Analyzer.run()
      _analysis_loop()             GBM model + 7-signal recommendation with commitment lock (every 50ms)
      _bias_refresher()            RSI/BB + Deribit DVOL + OKX basis/funding (every 60s)
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
| `BTC_SIGMA` | `0.80` | Fallback annualized vol for GBM when DVOL unavailable |
| `MAX_ENTRY_WINDOW_S` | `480` | Phase switches to `entry_open` when ≤ this many seconds remain |
| `MIN_ENTRY_WINDOW_S` | `120` | Phase switches to `too_late` when < this many seconds remain |
| `MIN_ENTRY_PRICE_CENTS` | `60` | Lower bound of the "in-range" price check (¢) |
| `MAX_ENTRY_PRICE_CENTS` | `85` | Upper bound of the "in-range" price check (¢) |
| `MOMENTUM_ENTRY_USD` | `30` | Min BTC move — 1.1× hysteresis applied |
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
| `ADX_CHOPPY_THRESHOLD` | `20` | ADX below this forces technicals to neutral and raises signal requirement to 3/4 |

## Project Layout

```text
main.py                      asyncio entry point
config.py                    settings (reads .env)
state/state_manager.py       shared in-memory hub, dataclasses, WebSocket broadcast
feeds/kalshi_ws.py           Kalshi REST/WS feed, auth, contract discovery, orderbook
feeds/btc_feed.py            Coinbase BTC-USD price + CVD from trade stream (WebSocket)
strategy/scalper.py          Analyzer: GBM model, 4-signal recommendation, window resolver
strategy/technicals.py       RSI/BB (Coinbase), DVOL (Deribit), basis/funding (OKX)
logger/event_logger.py       in-memory event buffer and async CSV flush
dashboard/app.py             FastAPI routes and WebSocket broadcast
dashboard/static/index.html  browser dashboard
logs/session_<ts>.csv        per-session event log
logs/predictions.csv         cross-session prediction outcomes
logs/lifetime_stats.json     persisted prediction accuracy counters
```

## Notes

- **No execution.** The bot is a read-only analysis tool. It subscribes to Kalshi's orderbook and Coinbase's price feed but never submits orders.
- **Resolution accuracy.** The bot queries Kalshi's settlement API for the official outcome (CF Benchmarks BRTI, not Coinbase spot). If the API call fails, it falls back to a Coinbase-price estimate.
- **GBM sigma source.** The model uses Deribit DVOL (implied vol) as sigma when available — it's more stable than 10-minute realized vol from tick data. Falls back to rolling realized vol if the Deribit fetch fails.
- **External data sources.** DVOL is from Deribit's public API (no auth). Basis and funding rate are from OKX's public API (no auth). Binance and Bybit are geo-blocked in the US.
- **Fees.** Kalshi taker fees use the formula 7% × p × (1-p) per side, where p is contract price. At 60–85¢ entry range, round-trip taker cost is ~2–3.4¢ per contract. Maker (limit) orders are ~75% cheaper. Factor this into any profitability analysis.
