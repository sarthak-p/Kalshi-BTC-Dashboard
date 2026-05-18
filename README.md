# Kalshi BTC 15-Min Trading Bot

Automated trading bot for Kalshi BTC 15-minute binary markets. Uses a drift-adjusted GBM fair-value model as the primary signal, with BTC slope and RSI/BB technical bias as secondary signals. Locks a recommendation at the 8-minute mark and places a fill when all pre-lock gates pass.

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

### Trade lifecycle

**Lock** fires when all of the following hold simultaneously inside the entry window (8:00–2:00 mark):

1. The raw signal has held the **same side for 20 continuous seconds**. The timer resets to zero if the signal goes neutral, so YES → neutral → YES does not accumulate time.
2. Slope confirms lock direction (`slope ≥ 0.05` for YES, `slope ≤ −0.05` for NO). **Bypassed** when |GBM − 50| > 25 (model already extremely confident).
3. |GBM − 50| ≥ 10 (no-conviction guard).

**Pre-lock vetoes** (checked in order after the stability conditions pass):

| Veto | Condition | Blocks |
|------|-----------|--------|
| Market-direction | Kalshi mid opposes model by > 25¢ | Both |
| OI squeeze | OI 5-min delta < −1.5% | YES |
| OI expansion | OI 5-min delta > +1.5% | NO |
| Long squeeze | liq\_long\_2m > $500K | YES |
| Short squeeze | liq\_short\_2m > $500K | NO |
| Taker short-heavy | taker ratio < 0.85 (feed live) | YES |
| Taker long-heavy | taker ratio > 1.15 (feed live) | NO |
| Depth ask-heavy | smoothed depth imbalance < −0.15 | YES |
| Depth bid-heavy | smoothed depth imbalance > +0.15 | NO |

**Flip suppression** — once a side locks in the recommendation, it holds for 60 seconds before flipping. Early unlock when GBM crosses 45% (YES locked) or 55% (NO locked), indicating a genuine reversal.

**Fill** — simulated at the current best ask (YES) or `100 − best bid` (NO) at execution time.

**Settlement** — at window close the position is marked won/lost based on the official Kalshi result.

### Position sizing

Flat **$100 per trade**. At 40¢ entry that's ~250 contracts; at 60¢ entry ~166 contracts. Sizing does not vary by confidence or prior result.

---

## The GBM model

GBM (Geometric Brownian Motion) computes the probability that BTC closes at or above the window-open strike. Inputs:

- Current BTC price vs strike (equal-weighted average of Coinbase, Kraken, and Bitstamp)
- Time remaining in the window
- Current BTC velocity (slope of recent price) — shifts the z-score so a rising BTC scores higher even if still below strike
- Volatility from Deribit DVOL (implied vol), falling back to rolling 10-minute realized vol from tick data

---

## BTC price feed

The bot averages prices from three exchanges in real time:

| Exchange | WebSocket | Notes |
|----------|-----------|-------|
| Coinbase | `wss://advanced-trade-ws.coinbase.com` | Primary — also drives CVD and slope history |
| Kraken | `wss://ws.kraken.com` | XBT/USD trade feed (v1 API) |
| Bitstamp | `wss://ws.bitstamp.net` | live\_trades\_btcusd |

`btc_price` is the equal-weighted average of whichever exchanges have a non-zero price. If only Coinbase is connected, the full Coinbase price is used — downstream GBM, slope, and strike comparison all read from the same averaged value. This reduces BRTI divergence since BRTI itself averages the same exchange set.

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
| Kalshi orderbook | Kalshi WS | Tick | Entry price, OB imbalance, market-direction veto |
| DVOL | Deribit REST | Every 15 s | GBM vol input |
| Funding rate | OKX REST | Every 15 s | Informational signal |
| Taker ratio | `fstream.binance.com/ws/btcusdt@aggTrade` | Real-time | 5-min rolling buy/sell ratio; lock veto |
| Depth imbalance | `fstream.binance.com/ws/btcusdt@depth20@100ms` | 100 ms | Top-10 bid/ask imbalance; lock veto |
| Liquidations | `fstream.binance.com/ws/btcusdt@forceOrder` | Tick | 2-min rolling long/short liq USD; lock veto |

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
  → FuturesTakerFeed.run()            Binance Futures aggTrade → rolling 5-min taker ratio
  → BinanceDepthFeed.run()            Binance Futures depth20@100ms → bid/ask imbalance
  → BinanceLiqFeed.run()              Binance Futures forceOrder → 2-min rolling liquidations
  → Analyzer.run()
      _analysis_loop()                GBM fair value + recommendation (every 50 ms)
      _bias_refresher()               RSI/BB (between windows only) + DVOL + OKX funding (every 15 s)
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
| `PAPER_BANKROLL_RESET` | `0` | Set to a positive value to reset paper balance on next startup, then set back to 0 |
| `BTC_SIGMA` | `0.80` | Fallback annualized vol for GBM when DVOL unavailable |
| `MOMENTUM_ENTRY_USD` | `20.0` | Min BTC move from strike to show as bullish/bearish in signal panel |
| `BTC_SLOPE_SIGNAL_THRESHOLD` | `0.30` | Min \|slope\| in $/s for slope signal to fire (≈ $18/min) |
| `MIN_COMMITMENT_RATE` | `0.08` | Warning threshold: `\|BTC move\| / tau` in $/s (shown as ⚠, does not block) |
| `MIN_GBM_MARKET_GAP_CENTS` | `8.0` | Min gap between GBM and Kalshi mid for a small-edge warning (display only) |
| `MAX_ENTRY_WINDOW_S` | `480.0` | Entry window opens when seconds remaining crosses this (8:00 mark) |
| `MIN_ENTRY_WINDOW_S` | `120.0` | Too-late threshold — entry window closes below this (2:00 mark) |
| `MOMENTUM_THRESHOLD_USD` | `150.0` | BTC move in 10 s that triggers a 30-second velocity-pause flag |
| `NEW_WINDOW_SETTLE_S` | `15.0` | Grace period after contract discovery before monitoring data counts |
| `MIN_OPEN_INTEREST` | `500` | Thin-market flag threshold (contracts) |
| `OI_SQUEEZE_THRESHOLD_PCT` | `-1.5` | OI 5-min delta below this blocks YES lock |
| `FUTURES_TAKER_RATIO_HIGH` | `1.15` | Taker ratio above this blocks NO lock |
| `FUTURES_TAKER_RATIO_LOW` | `0.85` | Taker ratio below this blocks YES lock |
| `BINANCE_DEPTH_IMBALANCE_THRESHOLD` | `0.15` | Smoothed depth imbalance magnitude for lock veto |
| `LIQ_VETO_THRESHOLD_USD` | `500000` | 2-min rolling liquidation USD that triggers a lock veto |
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
- **BTC Price** — 3-exchange equal-weighted average with per-source breakdown (CB / KR / BS)
- **BTC Slope** — weighted $/s velocity over 90s and 300s windows
- **CVD** — cumulative volume delta from Coinbase trade stream (net buy BTC since window open)
- **Depth Imbalance** — 30-second smoothed top-10 bid/ask quantity imbalance (−1 to +1)
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
- **`model=YES [CORRECT/WRONG]`** — the 8-min locked recommendation and whether it was right
- **`slope=CORRECT/WRONG`** — GBM slope direction accuracy (tracked separately)
- **Green** = model predicted and was correct; **Red** = predicted and wrong; **Gray** = no prediction that window

---

## Notes

- **Fees.** Kalshi taker fees ≈ 7% × p × (1−p) per contract. At 40¢ entry, round-trip taker cost is ~1.7¢ per contract. Not deducted from paper sizing.
- **Settlement accuracy.** Queries Kalshi's API for the official BRTI-based result. Falls back to a Coinbase-price estimate if the API doesn't return within 2 minutes, tagged `[estimated]`.
- **GBM sigma source.** Uses Deribit DVOL (implied vol) when available. Falls back to rolling 10-minute realized vol from tick data.
- **Market-direction veto threshold.** The 25¢ gap cutoff is hardcoded in `strategy/scalper.py`. Empirically: model accuracy drops to ~20% when the Kalshi market mid opposes GBM by more than this amount, likely because BRTI constituents have already priced in information from exchanges the bot cannot see.
