# Kalshi BTC 15-Min Trading Bot

Automated paper-trading bot for Kalshi BTC 15-minute binary markets. Uses a drift-adjusted GBM fair-value model as the primary signal, with BTC slope and RSI/BB technical bias as secondary context. Places simulated fills whenever the model locks a recommendation at the 8-minute mark.

---

## How Kalshi BTC contracts work

Each `KXBTC15M` contract is a binary that pays **$1.00 if BTC closes at or above the window-open price**, $0.00 otherwise.

- **Buy YES at 40¢** → profit 60¢ if BTC closes up, lose 40¢ if not
- **Buy NO at 35¢** → profit 65¢ if BTC closes down, lose 35¢ if not

Kalshi settles using CF Benchmarks' BRTI (averaged over the 60 seconds before close) — not Coinbase spot. The bot queries the Kalshi settlement API for the official result.

---

## How the bot decides to trade

### Decision hierarchy

First signal that fires wins:

1. **GBM fair value** — if GBM < 35% → NO; if GBM > 65% → YES
2. **BTC slope** — if GBM neutral and |slope| > 0.30 $/s and GBM confirms (> 60% for YES, < 40% for NO) → slope drives
3. **Technical bias=down** — if GBM+slope both neutral and GBM < 40% → NO
4. **No signal** → no trade

`bias=up` is **not** a standalone trigger — 50% accuracy in live data. Shown in the dashboard as informational only.

### Trade lifecycle

- **Lock**: at the **7:30 mark** (`entry_open` phase), the model begins waiting to lock. Two conditions must both hold:
  1. The raw signal has held the **same side for 30 continuous seconds** — filters single-tick spikes. The timer resets to zero if the signal goes neutral, so a YES → neutral → YES pattern does not accumulate continuous time.
  2. GBM is past the threshold (> 65% YES or < 35% NO)

- **Pre-lock gates** (checked in order, any failure skips the lock and logs the reason):

  | Gate | Condition | Blocks |
  |------|-----------|--------|
  | Slope alignment | slope < 0.05 $/s for YES, or > −0.05 $/s for NO | Both |
  | GBM conviction | \|GBM − 50\| < 12 → model has no opinion | Both |
  | OI squeeze | OI 5-min delta < −1.5% | YES |
  | OI expansion | OI 5-min delta > +1.5% | NO |
  | Long squeeze | liq_long_2m > $500K | YES |
  | Short squeeze | liq_short_2m > $500K | NO |
  | Taker ratio (short-heavy) | taker ratio < 0.85 (feed must be live) | YES |
  | Taker ratio (long-heavy) | taker ratio > 1.15 (feed must be live) | NO |
  | Depth imbalance (ask-heavy) | smoothed depth < −0.15 | YES |
  | Depth imbalance (bid-heavy) | smoothed depth > +0.15 | NO |

- **Fill**: simulated at the current best ask (YES) or `100 − best bid` (NO) at the moment of execution.

- **Settlement**: at window close the position is marked won/lost based on the official Kalshi result.

### Position sizing

Flat **$100 per trade**, every window. At 40¢ entry that's ~250 contracts; at 60¢ entry ~166 contracts.

---

## The GBM model

The GBM (Geometric Brownian Motion) model prices the probability that BTC closes above the window-open strike. It incorporates:

- Current BTC price vs strike
- Time remaining in the window
- Current BTC velocity (slope of recent price) — a fast-rising BTC scores higher even if still below strike
- Volatility from Deribit DVOL (implied vol), or rolling realized vol as fallback

---

## Technical bias (RSI/BB)

Fetched every **15 seconds between windows** (locked during active windows) from Coinbase Exchange 1-minute candles — last 35 candles (~35 minutes of data).

| Indicator | Bullish (`up`) | Bearish (`down`) |
|-----------|---------------|-----------------|
| RSI(14) | > 60 | < 40 |
| Bollinger Band position | > 0.6 (near upper band) | < 0.4 (near lower band) |
| ADX(14) | must be ≥ 15 for any signal to count | < 15 → all signals suppressed |

- `bias=down` (RSI < 40): **73% accurate** — standalone NO trigger when GBM+slope neutral and GBM < 43%
- `bias=up` (RSI > 60): **50% accurate** — informational only, never a trade trigger

**ADX < 15** → bias forced to neutral regardless of RSI/BB.

The bias is locked at window discovery and does not update mid-window.

---

## Recommendation panel signals

| Signal | Source | Role |
|--------|--------|------|
| **GBM fair value** | Live BTC + DVOL | Primary — drives when GBM < 35% (NO) or > 65% (YES) |
| **BTC slope** | Coinbase spot price history | Secondary — drives when GBM neutral and \|slope\| > 0.30 $/s |
| **Technical bias=down** | Coinbase 1-min candles | Tertiary — standalone NO trigger when GBM+slope neutral and GBM < 43% |
| **Technical bias=up** | Coinbase 1-min candles | Informational only |
| BTC momentum | Coinbase spot | Informational |
| CVD (order flow) | Coinbase trade stream | Informational |
| Funding rate | OKX perp | Informational |
| Orderbook imbalance | Kalshi order book | Informational |
| Kalshi mid momentum | Kalshi mid price history | Informational |
| **Taker ratio** | Binance Futures aggTrade stream | Informational + lock veto when strongly opposing |
| **Depth imbalance** | Binance Futures depth20 stream | Informational + lock veto when strongly opposing |
| **Liquidation pressure** | Binance Futures forceOrder stream | Informational + hard lock veto |

---

## Live data feeds

| Feed | Source | Update rate | Purpose |
|------|--------|-------------|---------|
| BTC price + CVD | Coinbase Advanced Trade WS | Tick | GBM input, CVD signal |
| Kalshi orderbook | Kalshi WS | Tick | Entry price, OB imbalance |
| DVOL | Deribit REST | Every 15 s | GBM vol input |
| Funding rate | OKX REST | Every 15 s | Informational signal |
| **Taker ratio** | `fstream.binance.com/ws/btcusdt@aggTrade` | Real-time (1-s state update) | 5-min rolling buy/sell ratio; lock veto |
| **Depth imbalance** | `fstream.binance.com/ws/btcusdt@depth20@100ms` | 100 ms | Top-10 bid/ask quantity imbalance; lock veto |
| **Liquidations** | `fstream.binance.com/ws/btcusdt@forceOrder` | Tick | 2-min rolling long/short liq USD; hard lock veto |

All Binance feeds use `fstream.binance.com` (futures streaming endpoint), which is accessible from the US. `fapi.binance.com` (REST) and `stream.binance.com` (spot WS) are geo-blocked in the US.

The taker ratio is computed locally from the aggTrade stream as a rolling 5-minute `takerBuyVol / takerSellVol` (matching Binance's REST definition). State is updated once per second.

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
  → FuturesTakerFeed.run()            Binance Futures aggTrade → rolling 5-min taker ratio
  → BinanceDepthFeed.run()            Binance Futures depth20@100ms → bid/ask imbalance
  → BinanceLiqFeed.run()              Binance Futures forceOrder → 2-min rolling liquidations
  → Analyzer.run()
      _analysis_loop()                GBM fair value + recommendation (every 50 ms)
      _bias_refresher()               RSI/BB (between windows only) + DVOL + OKX basis/funding (every 15 s)
      _window_resolver()              settlement + accuracy tracking (every 1 s)
  → Executor
      maybe_trade()                   paper fill based on locked model recommendation
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
| `BTC_SERIES_TICKER` | `KXBTC15M` | Series ticker for contract auto-discovery |
| `BANKROLL` | `1000.00` | Starting paper bankroll |
| `PAPER_BANKROLL_RESET` | `0` | Set to a positive value to reset paper balance on next startup, then set back to 0 |
| `BTC_SIGMA` | `0.80` | Fallback annualized vol for GBM when DVOL unavailable |
| `MOMENTUM_ENTRY_USD` | `20.0` | Min BTC move from strike shown as "bullish/bearish" in signal panel |
| `BTC_SLOPE_SIGNAL_THRESHOLD` | `0.30` | Min \|slope\| in $/s for slope signal to fire (0.30 $/s ≈ $18/min) |
| `MIN_COMMITMENT_RATE` | `0.08` | Warning threshold: `\|BTC move\| / tau` in $/s (shown as ⚠, does not block) |
| `MIN_GBM_MARKET_GAP_CENTS` | `10.0` | Dashboard display only — informational gap warning, does not block execution |
| `MIN_ENTRY_PRICE_CENTS` | `8.0` | Dashboard display only — does not block execution |
| `MAX_ENTRY_PRICE_CENTS` | `65.0` | Dashboard display only — does not block execution |
| `MAX_ENTRY_WINDOW_S` | `450.0` | Entry window opens when seconds remaining crosses this (7:30 mark) |
| `MIN_ENTRY_WINDOW_S` | `120.0` | Too-late threshold — entry window closes below this (2-min mark) |
| `MOMENTUM_THRESHOLD_USD` | `150.0` | BTC move in 10 s that triggers a 30-second velocity-pause flag |
| `NEW_WINDOW_SETTLE_S` | `15.0` | Grace period after contract discovery before monitoring data counts |
| `MIN_OPEN_INTEREST` | `500` | Thin-market flag threshold (contracts) |
| `MIN_ADX_THRESHOLD` | `15.0` | ADX below this forces technical bias to neutral |
| `BINANCE_SYMBOL` | `BTC-USD` | Coinbase product ID for candle fetch |
| `BINANCE_KLINES_INTERVAL` | `60` | Candle granularity in seconds |
| `FUTURES_TAKER_RATIO_HIGH` | `1.15` | Taker ratio above this → long-heavy; blocks NO lock and leans YES in signals |
| `FUTURES_TAKER_RATIO_LOW` | `0.85` | Taker ratio below this → short-heavy; blocks YES lock and leans NO in signals |
| `OI_SQUEEZE_THRESHOLD_PCT` | `-1.5` | OI 5-min delta below this blocks YES lock (shrinking OI = no real momentum) |
| `BINANCE_DEPTH_IMBALANCE_THRESHOLD` | `0.15` | Smoothed depth imbalance magnitude for signal and lock veto |
| `LIQ_VETO_THRESHOLD_USD` | `500000` | 2-min rolling liquidation USD that triggers a hard lock veto |
| `DASHBOARD_HOST` | `127.0.0.1` | Dashboard bind host |
| `DASHBOARD_PORT` | `8000` | Dashboard port |

---

## Logged data

| File | Contents |
|------|----------|
| `logs/session_<ts>.csv` | Every analysis event this session (recommendations, skips, errors) |
| `logs/predictions.csv` | Cross-session prediction outcomes with resolution and model accuracy |
| `logs/lifetime_stats.json` | Persisted prediction accuracy counters across all sessions |
| `logs/executor_bankroll.json` | Paper trade P&L — persists across restarts |
| `logs/resolution_history.json` | Last 100 window resolutions with model accuracy labels |

---

## Dashboard

The live dashboard at `http://127.0.0.1:8000` displays:

- **GBM YES %** — current fair-value probability, colored green (>52%), red (<48%), grey (neutral)
- **BTC Slope** — weighted $/s velocity, colored green (positive) / red (negative)
- **Taker Ratio** — 5-min rolling buy/sell volume ratio; green (>1.15 long-heavy), red (<0.85 short-heavy)
- **Depth Imbalance** — 30-second smoothed top-10 bid/ask quantity imbalance (−1 to +1)
- **Liq Pressure** — red `⚠ LONG SQZ` or green `⚠ SHORT SQZ` when 2-min rolling liquidations exceed half the veto threshold ($250K)
- Recommendation panel with live basis list and lock confidence block
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
- **Binance geo-block.** All three Binance feeds use `fstream.binance.com` (futures streaming), which is US-accessible. The spot WebSocket (`stream.binance.com`) and futures REST (`fapi.binance.com`) return HTTP 451 from the US. The taker ratio is therefore computed locally from the aggTrade stream rather than polled from the REST endpoint.
- **Taker ratio warmup.** The taker ratio shows `—` until at least one sell-side trade has been observed on the aggTrade stream (typically within one second of connecting). Lock vetoes based on taker ratio only fire when the feed is live (`ratio > 0`).
- **Two bankrolls.** The executor bankroll (`logs/executor_bankroll.json`) tracks actual paper trades placed. The model predicts every window but only fires a trade when all lock conditions are met, so not every prediction results in a trade.
- **Position sizing.** Flat $100 per trade (~250 contracts at 40¢). Sizing does not vary by confidence or prior result.
