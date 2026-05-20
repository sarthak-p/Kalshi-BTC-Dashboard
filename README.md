# Kalshi BTC 15-Min Trading Bot

Automated trading bot for Kalshi BTC 15-minute binary markets. Uses a drift-adjusted GBM fair-value model as the primary signal, with BTC slope and RSI/BB technical bias as secondary signals. Locks a recommendation at the 8-minute mark and places a fill when the lock fires.

---

## How Kalshi BTC contracts work

Each `KXBTC15M` contract is a binary that pays **$1.00 if BTC closes at or above the window-open price**, $0.00 otherwise.

- **Buy YES at 40¢** → profit 60¢ if BTC closes up, lose 40¢ if not
- **Buy NO at 35¢** → profit 65¢ if BTC closes down, lose 35¢ if not

Kalshi settles using CF Benchmarks' BRTI — a volume-weighted average of Coinbase, Kraken, Bitstamp, itBit, Gemini, LMAX, and Crypto.com prices over the 60 seconds before close. The bot queries the Kalshi settlement API for the official result.

---

## How the bot decides to trade

### Signal hierarchy

1. **GBM fair value** — if GBM > 60% → YES; if GBM < 40% → NO
2. **BTC slope** — if GBM neutral and |slope| ≥ 0.30 $/s and GBM confirms (> 55% for YES, < 45% for NO) → slope drives
3. **Technical bias=down** — if GBM+slope both neutral and GBM < 40% → NO
4. **No signal** → no trade

`bias=up` is never a standalone trade trigger — informational only.

### GBM slope drift cap

The GBM model uses current BTC velocity (slope) as a drift term to shift the fair-value probability. The slope is projected over **at most 90 seconds**, not the full remaining window time. Projecting over the full 7–8 minutes remaining inflates GBM unrealistically — a mild +0.25 $/s slope over 450 s implies +$112 of BTC movement, which pushes a below-strike position from ~30% to ~64% YES. Capped at 90 s, the drift contribution is realistic and the model stays aligned with the market.

### Trade lifecycle

**Lock** fires when all of the following hold simultaneously inside the entry window (9:00–2:00 mark):

1. The raw signal has held the **same side for 30 continuous seconds**. The timer resets to zero if the signal goes neutral, so YES → neutral → YES does not accumulate time.
2. Slope confirms lock direction (`slope ≥ 0.05` for YES, `slope ≤ −0.05` for NO). **Bypassed** when |GBM − 50| > 25 (model already extremely confident), or when the Kraken perp basis strongly confirms the direction (basis > $80 for YES, < −$80 for NO).
3. |GBM − 50| ≥ 10 (conviction guard — no coin-flip locks).



**Flip suppression** — once a side locks in the recommendation, it holds for 60 seconds before flipping. Early unlock when GBM crosses 45% (YES locked) or 55% (NO locked), indicating a genuine reversal.

**Fill** — simulated at the current best ask (YES) or `100 − best bid` (NO) at execution time. Position size is dynamic: base $150, scaled by GBM-market gap and confirming signal count, capped at $100–$200.

**Settlement** — at window close the position is marked won/lost based on the official Kalshi result.

---

## The GBM model

GBM (Geometric Brownian Motion) computes the probability that BTC closes at or above the window-open strike. Inputs:

- Current BTC price vs strike (equal-weighted average of Coinbase, Kraken, Bitstamp, and Gemini)
- Time remaining in the window
- Current BTC velocity (slope of recent price) — shifts the z-score so a rising BTC scores higher even if still below strike; capped at 90 s projection
- Volatility: Deribit DVOL (implied vol) when tau > 3 min; 5-min rolling realized vol when tau < 3 min (sharper near close)

---

## BTC price feed

The bot averages prices from four exchanges in real time — all BRTI constituents with accessible public feeds:

| Exchange | Feed | Notes |
|----------|------|-------|
| Coinbase | `wss://advanced-trade-ws.coinbase.com` | Primary — also drives CVD and slope history |
| Kraken | `wss://ws.kraken.com` | XBT/USD trade feed (v1 API) |
| Bitstamp | `wss://ws.bitstamp.net` | live\_trades\_btcusd |
| Gemini | `wss://api.gemini.com/v1/marketdata/BTCUSD` | trade events, WebSocket |

`btc_price` is the equal-weighted average of whichever exchanges have a non-zero price. This covers 4 of the 7 BRTI constituent exchanges; itBit was dropped (API abandoned/cert expired), LMAX and Crypto.com have no accessible public feeds.

---

## Technical bias (RSI/BB)

Fetched every **15 seconds between windows** (locked during active windows) from Coinbase Exchange 1-minute candles — last 35 candles.

| Indicator | Bullish (`up`) | Bearish (`down`) |
|-----------|---------------|-----------------|
| RSI(14) | > 60 | < 40 |
| Bollinger Band position | > 0.6 (near upper band) | < 0.4 (near lower band) |
| ADX(14) | must be ≥ 15 for any signal to count | < 15 → all signals suppressed |

- `bias=down` (bearish) may drive a NO trade when GBM+slope are neutral and GBM < 40%
- `bias=up` (bullish) is informational only — never triggers a trade independently

The bias is locked at window discovery and does not update mid-window.

---

## Live data feeds

| Feed | Source | Update rate | Purpose |
|------|--------|-------------|---------|
| BTC price + CVD | Coinbase Advanced Trade WS | Tick | GBM input, CVD signal, slope history |
| BTC price | Kraken WS v1 | Tick | Multi-exchange price average |
| BTC price | Bitstamp WS | Tick | Multi-exchange price average |
| BTC price | Gemini WS v1 | Tick | Multi-exchange price average (BRTI constituent) |
| Kalshi orderbook | Kalshi WS | Tick | Entry price, OB imbalance |
| Perp mark/index | Kraken `futures.kraken.com/ws/v1` (PI_XBTUSD) | Tick | Perp basis lead signal; slope-gate bypass |
| DVOL | Deribit REST | Every 15 s | GBM vol input |
| Funding rate | OKX REST | Every 15 s | Informational signal |
| Taker ratio | `fstream.binance.com/ws/btcusdt@aggTrade` | Real-time | 5-min rolling buy/sell ratio; informational |
| Depth imbalance | `fstream.binance.com/ws/btcusdt@depth20@100ms` | 100 ms | Top-10 bid/ask imbalance; informational |
| Liquidations | `fstream.binance.com/ws/btcusdt@forceOrder` | Tick | 2-min rolling long/short liq USD; informational |

All Binance feeds use `fstream.binance.com` (futures streaming endpoint), which is accessible from the US.

---

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env — set KALSHI_ENV, KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_B64

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
  → EventLogger.flush_loop()          async CSV flush every 5 s
  → StateManager.broadcast_loop()     WebSocket push to dashboard on every state change
  → KalshiFeed.run()                  REST contract discovery + WebSocket orderbook
  → BtcFeed.run()                     Coinbase BTC-USD price (ticker) + CVD (trade stream)
  → KrakenFeed.run()                  Kraken XBT/USD price
  → BitstampFeed.run()                Bitstamp BTC/USD price
  → GeminiFeed.run()                  Gemini BTC/USD price (WebSocket v1 trade feed)
  → KrakenPerpFeed.run()              Kraken PI_XBTUSD mark/index → 30 s smoothed perp basis
  → FuturesTakerFeed.run()            Binance Futures aggTrade → rolling 5-min taker ratio
  → BinanceDepthFeed.run()            Binance Futures depth20@100ms → bid/ask imbalance
  → BinanceLiqFeed.run()              Binance Futures forceOrder → 2-min rolling liquidations
  → Analyzer.run()
      _analysis_loop()                GBM fair value + recommendation (every 50 ms)
      _bias_refresher()               RSI/BB/ADX + DVOL + OKX funding (every 15 s, throughout window)
      _window_resolver()              settlement + accuracy tracking (every 1 s)
  → Executor
      maybe_trade()                   fill based on locked model recommendation
  → FastAPI/Uvicorn                   dashboard HTTP + WebSocket server
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
| `BTC_SERIES_TICKER` | `KXBTCD` | Series ticker for contract auto-discovery |
| `BANKROLL` | `250.00` | Starting paper bankroll |
| `BTC_SIGMA` | `0.80` | Fallback annualized vol for GBM when DVOL unavailable |
| `MAX_ENTRY_WINDOW_S` | `540.0` | Entry window opens when seconds remaining crosses this (9:00 mark) |
| `MIN_ENTRY_WINDOW_S` | `120.0` | Too-late threshold — entry window closes below this (2:00 mark) |
| `MOMENTUM_ENTRY_USD` | `20.0` | Min BTC move from strike to show as bullish/bearish in signal panel |
| `BTC_SLOPE_SIGNAL_THRESHOLD` | `0.30` | Min \|slope\| in $/s for slope signal to fire (≈ $18/min) |
| `MOMENTUM_THRESHOLD_USD` | `150.0` | BTC move in 10 s that triggers a 30-second velocity-pause flag |
| `NEW_WINDOW_SETTLE_S` | `15.0` | Grace period after contract discovery before monitoring data counts |
| `MIN_ADX_THRESHOLD` | `15.0` | ADX below this suppresses all RSI/BB bias signals |
| `FUTURES_TAKER_RATIO_HIGH` | `1.15` | Taker ratio above this flags long-heavy (informational) |
| `FUTURES_TAKER_RATIO_LOW` | `0.85` | Taker ratio below this flags short-heavy (informational) |
| `BINANCE_DEPTH_IMBALANCE_THRESHOLD` | `0.15` | Smoothed depth imbalance magnitude for informational signal |
| `LIQ_VETO_THRESHOLD_USD` | `500000` | 2-min rolling liquidation USD for informational signal |
| `DASHBOARD_HOST` | `127.0.0.1` | Dashboard bind host |
| `DASHBOARD_PORT` | `8000` | Dashboard port |

---

## Logged data

| File | Contents |
|------|----------|
| `logs/session_<ts>.csv` | Every analysis event this session (recommendations, skips, errors) |
| `logs/predictions.csv` | Cross-session prediction outcomes with resolution and model accuracy |
| `logs/lifetime_stats.json` | Persisted prediction accuracy counters across all sessions |
| `logs/executor_bankroll.json` | Trade P&L — persists across restarts |
| `logs/resolution_history.json` | Last 100 window resolutions with model accuracy labels |

---

## Dashboard

The live dashboard at `http://127.0.0.1:8000` displays:

- **GBM YES %** — current fair-value probability with confidence tier (Neutral / Moderate / Strong / Extreme)
- **Live Edge (GBM − Mid)** — GBM fair value minus Kalshi market mid in cents; green when YES is underpriced (> +10¢), red when NO is underpriced (< −10¢), glows at ±15¢
- **BTC Price** — 4-exchange equal-weighted average with per-source breakdown (CB / KR / BS / GE)
- **BTC Slope** — weighted $/s velocity over 90s and 300s windows
- **CVD** — cumulative volume delta from Coinbase trade stream (net buy BTC since window open)
- **Depth Imbalance** — 30-second smoothed top-10 bid/ask quantity imbalance (−1 to +1)
- **Perp Basis** — Kraken PI_XBTUSD mark − index (30 s smoothed); fires as informational signal at ±$50; green/bull-lead coloring at > +$80, red/bear-lead coloring at < −$80 (also the slope-gate bypass threshold)
- **Funding Rate** — OKX perp funding rate (informational)
- Recommendation panel with lock confidence block (GBM at lock time + gap vs market)
- BTC sparkline with window-open strike line
- Bot executor P&L and current position
- Resolution log with per-window model accuracy

### Resolution log format

```
KXBTC15M-26MAY151600-00  BTC 79096.27  (+14.65)  → YES [Kalshi]  model=YES [CORRECT]  slope=CORRECT
```

- **`→ YES / NO`** — what Kalshi settled
- **`[Kalshi]`** — result from live API; `[estimated]` = API timed out, inferred from Coinbase price
- **`model=YES [CORRECT/WRONG]`** — the locked recommendation and whether it was right
- **`slope=CORRECT/WRONG`** — GBM slope direction accuracy (tracked separately)
- **Green** = model predicted and was correct; **Red** = predicted and wrong; **Gray** = no prediction that window

---

## Notes

- **Fees.** Kalshi taker fees ≈ 7% × p × (1−p) per contract. At 40¢ entry, round-trip taker cost is ~1.7¢ per contract. Not deducted from paper sizing.
- **Settlement accuracy.** Queries Kalshi's API for the official BRTI-based result. Falls back to a Coinbase-price estimate if the API doesn't return within 2 minutes, tagged `[estimated]`.
- **GBM sigma source.** Uses Deribit DVOL (implied vol) when tau > 3 min. Switches to 5-min rolling realized vol when tau < 3 min — DVOL overestimates short-horizon variance and makes the model too uncertain near settlement.
- **Perp basis lead signal.** Kraken PI_XBTUSD basis (mark − index) is smoothed over 30 seconds. Acts as an informational supporting signal when |basis| > $50. A sustained premium > $80 additionally bypasses the slope-confirmation gate for YES locks (and vice-versa for discounts and NO locks).
