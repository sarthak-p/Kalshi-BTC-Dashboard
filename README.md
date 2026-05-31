# Kalshi BTC 15-Min Trading Bot

Automated trading bot for Kalshi BTC 15-minute binary markets. Uses a drift-adjusted GBM fair-value model as the primary signal, with BTC slope and RSI/BB technical bias as secondary signals. Locks a recommendation at the 5-minute mark, places a limit order, and holds the position to settlement.

---

## How Kalshi BTC contracts work

Each `KXBTC15M` contract is a binary that pays **$1.00 if BTC closes at or above the window-open price**, $0.00 otherwise.

- **Buy YES at 40¢** → profit 60¢ if BTC closes up, lose 40¢ if not
- **Buy NO at 35¢** → profit 65¢ if BTC closes down, lose 35¢ if not

Kalshi settles using CF Benchmarks' BRTI — a volume-weighted average of Coinbase, Kraken, Bitstamp, itBit, Gemini, LMAX, and Crypto.com prices over the 60 seconds before close.

---

## How the bot decides to trade

### Signal hierarchy

1. **GBM fair value** — if GBM > 60% → YES; if GBM < 40% → NO
2. **BTC slope** — if GBM neutral and |slope| ≥ 0.30 $/s and GBM confirms (> 55% for YES, < 45% for NO) → slope drives
3. **Technical bias=down** — if GBM+slope both neutral and GBM < 40% → NO
4. **No signal** → no trade

`bias=up` is never a standalone trade trigger — informational only.

### Lock gate filters (applied before locking)

Three checks must all pass before a lock fires:

1. **Strong tier** — `|fv − 50| ≥ 20` (GBM must be ≥ 70% YES or ≤ 30% YES). Blocks weak 65–70% signals that historically lose disproportionately.
2. **Slope veto** — if BTC slope actively opposes the signal direction by more than **0.10 $/s**, the entire window is skipped. Slope must be confirming or neutral.
3. *(Reversal guard removed)* — contrarian momentum trades (BTC opposite-direction but slope pushing back) are now allowed; they are profitable over time despite lower individual win rate.

**Lock** fires when the raw signal holds the same side for **15 continuous seconds** inside the entry window (5:00–2:00 mark). The timer resets to zero if the signal goes neutral or flips.

### Trade lifecycle

**Entry** — on lock, a limit order is placed at the **current ask price** (always taker). Fills within milliseconds. The order is cancelled if GBM crosses 50% neutral or the entry window closes.

**Hold to settlement** — once filled, the position is held until the contract settles. There are no take-profit or stop-loss exits. Kalshi settles the contract at window close and credits/debits the account automatically.

**Position sizing** — flat dollar amount per trade (`TRADE_SIZE_USD`), converted to fractional contracts at the limit price. Uses `fractional_trading_enabled` on Kalshi markets for exact dollar-based sizing.

---

## The GBM model

GBM (Geometric Brownian Motion) computes the probability that BTC closes at or above the window-open strike. Inputs:

- Current BTC price vs strike (equal-weighted average of Coinbase, Kraken, Bitstamp, and Gemini)
- Time remaining in the window (`tau`)
- Current BTC velocity (slope) — shifts the z-score; capped at 90s projection
- Volatility: Deribit DVOL (implied vol) when tau > 3 min; 5-min rolling realized vol when tau < 3 min

`BTC_SIGMA` (default `0.35`) is the annualized vol fallback used when DVOL is unavailable. Matches Deribit DVOL which typically runs 30–40% for BTC. Using a higher value inflates GBM uncertainty and suppresses valid signals.

---

## BTC price feed

The bot averages prices from four exchanges in real time — all BRTI constituents with accessible public feeds:

| Exchange | Feed | Notes |
|----------|------|-------|
| Coinbase | `wss://advanced-trade-ws.coinbase.com` | Primary — also drives CVD and slope history |
| Kraken | `wss://ws.kraken.com` | XBT/USD trade feed (v1 API) |
| Bitstamp | `wss://ws.bitstamp.net` | live\_trades\_btcusd |
| Gemini | `wss://api.gemini.com/v1/marketdata/BTCUSD` | trade events, WebSocket |

`btc_price` is the equal-weighted average of whichever exchanges have a non-zero price.

---

## Technical bias (RSI/BB)

Fetched every **15 seconds between windows** from Coinbase Exchange 1-minute candles — last 35 candles.

| Indicator | Bullish (`up`) | Bearish (`down`) |
|-----------|---------------|-----------------|
| RSI(14) | > 60 | < 40 |
| Bollinger Band position | > 0.6 (near upper band) | < 0.4 (near lower band) |
| ADX(14) | must be ≥ 15 for any signal to count | < 15 → all signals suppressed |

- `bias=down` may drive a NO trade when GBM+slope are neutral and GBM < 40%
- `bias=up` is informational only — never triggers a trade independently

---

## Live data feeds

| Feed | Source | Update rate | Purpose |
|------|--------|-------------|---------|
| BTC price + CVD | Coinbase Advanced Trade WS | Tick | GBM input, CVD signal, slope history |
| BTC price | Kraken WS v1 | Tick | Multi-exchange average |
| BTC price | Bitstamp WS | Tick | Multi-exchange average |
| BTC price | Gemini WS v1 | Tick | Multi-exchange average |
| Kalshi orderbook | Kalshi WS | Tick | Entry price, OB imbalance |
| Perp mark/index | Kraken `futures.kraken.com/ws/v1` (PI_XBTUSD) | Tick | Perp basis lead signal |
| DVOL | Deribit REST | Every 15 s | GBM vol input |
| Funding rate | OKX REST | Every 15 s | Informational signal |
| Taker ratio | `fstream.binance.com/ws/btcusdt@aggTrade` | Real-time | 5-min rolling buy/sell ratio |
| Depth imbalance | `fstream.binance.com/ws/btcusdt@depth20@100ms` | 100 ms | Top-10 bid/ask imbalance |
| Liquidations | `fstream.binance.com/ws/btcusdt@forceOrder` | Tick | 2-min rolling long/short liq USD |

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
  → GeminiFeed.run()                  Gemini BTC/USD price
  → KrakenPerpFeed.run()              Kraken PI_XBTUSD mark/index → 30 s smoothed perp basis
  → FuturesTakerFeed.run()            Binance Futures aggTrade → rolling 5-min taker ratio
  → BinanceDepthFeed.run()            Binance Futures depth20@100ms → bid/ask imbalance
  → BinanceLiqFeed.run()              Binance Futures forceOrder → 2-min rolling liquidations
  → Analyzer.run()
      _analysis_loop()                GBM fair value + recommendation (every 50 ms)
      _bias_refresher()               RSI/BB/ADX + DVOL + OKX funding (every 15 s)
      _window_resolver()              settlement + accuracy tracking (every 1 s)
  → LiveExecutor / Executor
      maybe_trade()                   place limit order on lock, hold to settlement
  → FastAPI/Uvicorn                   dashboard HTTP + WebSocket server
```

---

## Configuration reference

| Variable | Default | Description |
|----------|---------|-------------|
| `TRADING_MODE` | `paper` | `paper` or `live` |
| `KALSHI_ENV` | required | `demo` or `prod` |
| `KALSHI_API_KEY_ID` | — | Kalshi API key ID |
| `KALSHI_PRIVATE_KEY_B64` | — | Base64-encoded PEM private key |
| `BTC_SERIES_TICKER` | `KXBTC15M` | Series ticker for contract auto-discovery |
| `TRADE_SIZE_USD` | `15.00` | Dollar amount per trade (converted to fractional contracts) |
| `BANKROLL` | `250.00` | Starting paper bankroll |
| `BTC_SIGMA` | `0.35` | Annualized vol fallback for GBM when DVOL unavailable |
| `MAX_ENTRY_WINDOW_S` | `300.0` | Entry window opens when seconds remaining crosses this (5:00 mark) |
| `MIN_ENTRY_WINDOW_S` | `120.0` | Entry window closes below this (2:00 mark) |
| `MOMENTUM_ENTRY_USD` | `20.0` | Min BTC move from strike to show as bullish/bearish |
| `BTC_SLOPE_SIGNAL_THRESHOLD` | `0.30` | Min \|slope\| in $/s for slope signal to fire |
| `MOMENTUM_THRESHOLD_USD` | `150.0` | BTC move in 10 s that triggers a 30-second velocity-pause |
| `NEW_WINDOW_SETTLE_S` | `15.0` | Grace period after contract discovery before data counts |
| `MIN_ADX_THRESHOLD` | `15.0` | ADX below this suppresses all RSI/BB bias signals |
| `FUTURES_TAKER_RATIO_HIGH` | `1.15` | Taker ratio above this flags long-heavy |
| `FUTURES_TAKER_RATIO_LOW` | `0.85` | Taker ratio below this flags short-heavy |
| `BINANCE_DEPTH_IMBALANCE_THRESHOLD` | `0.15` | Smoothed depth imbalance magnitude threshold |
| `LIQ_VETO_THRESHOLD_USD` | `500000` | 2-min rolling liquidation USD threshold |
| `DASHBOARD_HOST` | `127.0.0.1` | Dashboard bind host |
| `DASHBOARD_PORT` | `8000` | Dashboard port |

---

## Logged data

| File | Contents |
|------|----------|
| `logs/session_<ts>.csv` | Every analysis event this session |
| `logs/predictions.csv` | Cross-session prediction outcomes |
| `logs/lifetime_stats.json` | Prediction accuracy counters across all sessions |
| `logs/executor_bankroll.json` | Trade P&L — persists across restarts |
| `logs/resolution_history.json` | Last 100 window resolutions |

---

## Dashboard

The live dashboard at `http://127.0.0.1:8000` displays:

- **GBM YES %** — current fair-value probability
- **Live Edge (GBM − Mid)** — GBM fair value minus Kalshi market mid in cents
- **BTC Price** — 4-exchange equal-weighted average
- **BTC Slope** — weighted $/s velocity over 90s and 300s windows
- **CVD** — cumulative volume delta from Coinbase trade stream
- **Depth Imbalance** — 30-second smoothed top-10 bid/ask imbalance
- **Perp Basis** — Kraken PI_XBTUSD mark − index (30 s smoothed)
- **Funding Rate** — OKX perp funding rate
- Recommendation panel with lock status
- BTC sparkline with window-open strike line
- Bot executor P&L and current position
- Resolution log with per-window model accuracy

---

## Notes

- **No price ceiling.** The bot trades at any price above 20¢. High-price (90-99¢) trades have tiny wins but >99% win rate and are net positive over time.
- **Always taker.** All orders are placed at the current ask price for immediate fill. No maker/resting orders — eliminates cancelled-order losses.
- **Settlement hold.** No exits mid-window. The bot enters once per window on model lock and holds to settlement. P&L is realised when Kalshi settles the contract.
- **Fees.** Kalshi taker fees ≈ 7% × p × (1−p) per contract. At 60¢ entry, round-trip taker cost is ~1.7¢ per contract. Not deducted from paper sizing.
- **Fractional contracts.** Kalshi markets have `fractional_trading_enabled`. The bot sends fractional contract counts for exact dollar-based sizing.
- **GBM sigma.** `BTC_SIGMA=0.35` matches Deribit DVOL. The fallback is only used when the DVOL feed is unavailable; an incorrect value here silently breaks the GBM signal.
- **Settlement accuracy.** Queries Kalshi's API for the official BRTI-based result. Falls back to a Coinbase-price estimate if the API doesn't return within 2 minutes, tagged `[estimated]`.
