# Kalshi BTC 15-Min Trading Bot

Real-time **mispricing arbitrage** bot for Kalshi BTC 15-minute binary markets,
with paper and live execution modes plus a FastAPI dashboard.

## How it works

Kalshi's `KXBTC15M` contract is a binary: it pays $1 if BTC closes at or above
the price it opened the 15-minute window at, $0 otherwise. The bot's edge is
that Kalshi's orderbook is sometimes slow to reprice after BTC makes a big move
early in the window. The bot watches for that lag and bets on the side that
Kalshi's market is undervaluing.

**Every 50 ms it asks:** given where BTC is right now relative to the window
open price, what *should* the YES contract be worth?

```
z = (btc_change% since window open) / (realized_vol * sqrt(time_remaining / year))
fair_value_yes = N(z) * 100 cents   [clamped to 5–95]
```

Volatility is estimated live from the last 10 minutes of BTC price history
using a sum-of-squared log returns estimator, clamped to `[0.20, 2.50]`. The
`BTC_SIGMA` config value is only used as a fallback until enough history
accumulates.

If `|fair_value − kalshi_mid| > 7¢` (the default threshold), and the contract
isn't mispriced in a direction that contradicts BTC momentum, it fires a signal.

**Entry window:** the bot enters any time in the first ~11 minutes of a
15-minute window. It blocks entries when fewer than 4 minutes remain
(`MIN_ENTRY_WINDOW_S=240`) because by then the market has already converged and
the edge is gone.

**Exit:** take profit when position value rises 15% above entry (`TAKE_PROFIT_PCT`),
stop loss when position value drops to 45% of entry (`STOP_LOSS_PCT`), hard
time-stop at 2 minutes remaining if the position is in loss.

The bot listens to live Kalshi market data and a Coinbase BTC/USD reference
feed, runs the model continuously, and either paper-trades or submits real
orders depending on `TRADING_MODE`.

## Security

**Never commit `.env`** — it contains your Kalshi API key and RSA private key.
The `.gitignore` excludes it. Use `.env.example` as a template. If real
credentials were ever committed, rotate them immediately.

## Quick Start

```bash
cd kalshi-btc-bot

# Python 3.11 or 3.12
python3.11 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt

cp .env.example .env
# Edit .env — at minimum set KALSHI_ENV, KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_B64

python main.py
```

Dashboard: `http://127.0.0.1:8000`

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

5. Confirm `BTC_SERIES_TICKER` matches the current Kalshi market series.

The Kalshi feed uses REST to discover the currently open market for the
configured series, then connects to the Kalshi WebSocket and subscribes to that
market's orderbook and ticker updates.

## Runtime Architecture

`main.py` starts the whole system as concurrent asyncio tasks:

```text
main.py
  -> EventLogger.flush_loop()
  -> StateManager.broadcast_loop()
  -> KalshiFeed.run()
  -> BtcFeed.run()
  -> Scalper.run()
  -> PaperTrader.run() or LiveTrader.run()
  -> RiskManager.run()
  -> FastAPI/Uvicorn dashboard
```

The shared `StateManager` is the in-memory hub. Feeds write live prices and
orderbook updates into it, the strategy reads from it, the active trader records
positions and P&L in it, and the dashboard broadcasts snapshots from it over a
WebSocket. Win rate and trade count persist across sessions in
`logs/lifetime_stats.json`.

## Data Flow

1. `KalshiFeed` discovers the active BTC 15-minute market from Kalshi REST.
2. `KalshiFeed` connects to Kalshi WebSocket and maintains a YES-side orderbook.
   NO bids are converted to YES asks with `100 - no_bid`.
3. `BtcFeed` streams Coinbase `BTC-USD` ticker updates into state as the BTC
   reference price.
4. `Scalper` evaluates the current fair value roughly every 50 ms.
5. When the fair-value gap clears the configured thresholds, `Scalper` emits a
   signal.
6. The configured trader consumes the signal and checks risk.
7. In paper mode, `PaperTrader` simulates a fill at the live best ask.
8. In live mode, `LiveTrader` sends a real fill-or-kill limit order to Kalshi.
9. Open positions are monitored for take profit, stop loss, time stop, or
   settlement when the 15-minute window closes.
10. The dashboard renders current state, feed status, signals, open positions,
    P&L, and event log.

## Fair-Value Model

The model is an up/down window binary — no strike price, just whether BTC
closes above or below where it opened.

```text
btc_change_pct   = (btc_price - btc_open) / btc_open
realized_vol_pct = rolling_realized_vol * sqrt(seconds_to_close / seconds_per_year)
z                = btc_change_pct / realized_vol_pct
fair_value_yes   = N(z) * 100  [clamped 5–95 cents]
```

Interpretation:

- `fair_value_yes > kalshi_mid` → YES looks underpriced, bot considers buying YES.
- `fair_value_yes < kalshi_mid` → NO looks underpriced, bot considers buying NO.
- A signal fires only when the gap exceeds `SIGNAL_THRESHOLD`.

Additional filters before a signal is emitted:

- **Velocity pause:** BTC moves > $50 in 10 s → all signals paused for 30 s.
- **Momentum filter:** if BTC has a sustained directional trend, the bot skips
  entries that would trade against it (unless the Kalshi price has already
  reached an extreme, triggering spike-fade mode).
- **Directional drift guard:** blocks entries after BTC has already moved
  significantly against the position's direction.
- **Entry price range:** only enters when the contract side costs between
  `MIN_ENTRY_PRICE_CENTS` and `MAX_ENTRY_PRICE_CENTS`.
- **Thin-market filter:** skips signals when open interest is below
  `MIN_OPEN_INTEREST`.
- **New-window settle:** blocks all signals for `NEW_WINDOW_SETTLE_S` after a
  new market is discovered, giving feeds time to stabilise.
- **Signal debounce + post-entry cooldown:** prevents rapid-fire entries.
- **Confidence threshold:** requires `gap_score * 0.7 + depth_score * 0.3` to
  exceed `CONFIDENCE_THRESHOLD`.

## Paper Trading Behavior

Positions are simulated using Kalshi binary contract economics:

- Buying YES costs the best YES ask.
- Buying NO costs `100 - best_yes_bid`.
- Selling YES receives the best YES bid.
- Selling NO receives `100 - best_yes_ask`.
- One contract settles at $1.00 if correct, $0.00 if incorrect.

Exit logic:

- **Take profit:** close when position value rises `TAKE_PROFIT_PCT` above entry.
- **Stop loss:** close when position value drops to `STOP_LOSS_PCT` of entry.
- **Time stop:** close losing positions when ≤ 2 minutes remain.
- **Settlement:** remaining positions close at 0 or 100 when the window expires.

## Live Trading Behavior

Live mode is intentionally conservative:

- Entries are real Kalshi `buy` orders sent as fill-or-kill limits at the
  current best ask, so no resting orders are left behind.
- Contract count per order is `min(LIVE_UNIT_SIZE, floor(LIVE_MAX_ORDER_COST_USD / entry_price))`,
  meaning the budget cap is a hard ceiling and the unit size is a target.
- Stop-loss and time-stop exits send real `sell` orders with `reduce_only=true`
  and `time_in_force=fill_or_kill`. Unfilled exits are retried after
  `LIVE_ORDER_COOLDOWN_S`.
- Window settlement is tracked locally for the dashboard; actual settlement is
  handled by Kalshi.
- On startup, live mode refuses to arm if the Kalshi account already has open
  positions, unless `LIVE_ALLOW_EXISTING_POSITIONS=true`.

To enable live mode, set in `.env`:

```env
TRADING_MODE=live
LIVE_TRADING_ACK=I_UNDERSTAND_THIS_PLACES_REAL_ORDERS
LIVE_UNIT_SIZE=5
LIVE_MAX_ORDER_COST_USD=5.00
LIVE_ALLOW_EXISTING_POSITIONS=false
DAILY_LOSS_LIMIT_USD=5.0
```

The config validator refuses live mode unless:

- `LIVE_TRADING_ACK` exactly matches the required text
- API credentials (`KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY_B64`) are present
- `LIVE_MAX_ORDER_COST_USD` is at most $25.00
- `DAILY_LOSS_LIMIT_USD` is at most $10.00
- `LIVE_ORDER_COOLDOWN_S` is at least 2 seconds

## Risk Controls

The risk manager blocks new positions when:

- the kill switch is active
- open positions are at `MAX_CONCURRENT_POSITIONS`
- daily/session loss reaches `DAILY_LOSS_LIMIT_USD`
- paper cash balance is below the position cost

The dashboard exposes a kill-switch button that activates the same state flag.

## Dashboard

FastAPI routes:

```text
GET  /           dashboard UI
GET  /api/state  current state snapshot
POST /api/kill   activate kill switch
WS   /ws         real-time state stream
```

The frontend is a single-file vanilla JS app in `dashboard/static/index.html`.

## Project Layout

```text
main.py                    asyncio entry point
config.py                  settings loader, reads .env
state/state_manager.py     shared state, dataclasses, WebSocket broadcast hub
feeds/kalshi_ws.py         Kalshi REST/WS feed, auth, contract discovery, orderbook
feeds/btc_feed.py          Coinbase BTC-USD reference price feed
execution/kalshi_client.py authenticated Kalshi REST order client
execution/live_trader.py   live FOK order execution
strategy/scalper.py        rolling vol, fair-value model, signal generation
simulation/paper_trader.py paper portfolio, exits, settlement
risk/risk_manager.py       position limits and daily loss kill switch
logger/event_logger.py     in-memory event buffer and async CSV flush
dashboard/app.py           FastAPI routes and dashboard WebSocket
dashboard/static/          browser dashboard
logs/                      runtime CSV logs; lifetime_stats.json (win rate)
```

## Configuration Reference

| Variable | Default | Description |
| --- | --- | --- |
| `KALSHI_ENV` | required | `demo` or `prod` |
| `KALSHI_API_KEY_ID` | empty | Kalshi API key ID |
| `KALSHI_PRIVATE_KEY_B64` | empty | Base64-encoded PEM private key |
| `BTC_SERIES_TICKER` | `KXBTCD` | Series ticker for contract discovery |
| `COINBASE_WS_URL` | Coinbase WS | BTC-USD reference feed URL |
| `BTC_SIGMA` | `0.80` | Fallback annualized BTC vol (used until rolling vol is ready) |
| `SIGNAL_THRESHOLD` | `0.07` | Minimum fair-value gap fraction to fire a signal |
| `CONFIDENCE_THRESHOLD` | `0.55` | Minimum confidence score to act |
| `SIGNAL_DEBOUNCE_S` | `10.0` | Minimum seconds between generated signals |
| `MIN_ENTRY_WINDOW_S` | `240.0` | Block entries when < this many seconds remain in window |
| `MIN_ENTRY_PRICE_CENTS` | `30.0` | Minimum contract ask price to enter |
| `MAX_ENTRY_PRICE_CENTS` | `65.0` | Maximum contract ask price to enter |
| `TAKE_PROFIT_PCT` | `0.15` | Exit when position value rises this fraction above entry |
| `STOP_LOSS_PCT` | `0.45` | Exit when position value drops to this fraction of entry |
| `MOMENTUM_THRESHOLD_USD` | `150.0` | BTC USD move over 30s to declare a momentum trend |
| `MAX_ADVERSE_DRIFT_PCT` | `0.002` | Max adverse BTC drift before blocking entries |
| `FADE_EXTREME_CENTS` | `72.0` | YES ask threshold above which spike-fade mode activates |
| `NEW_WINDOW_SETTLE_S` | `15.0` | Block signals after new contract discovery |
| `MIN_OPEN_INTEREST` | `500` | Minimum open interest to allow signals |
| `MAX_CONCURRENT_POSITIONS` | `3` | Maximum open positions at once |
| `DAILY_LOSS_LIMIT_USD` | `5.0` | Session loss limit before kill switch |
| `STARTING_BALANCE` | `1000.0` | Starting paper balance |
| `TRADING_MODE` | `paper` | `paper` or `live` |
| `LIVE_TRADING_ACK` | empty | Required ack string for live mode |
| `LIVE_UNIT_SIZE` | `5` | Target contracts per live order |
| `LIVE_MAX_ORDER_COST_USD` | `5.00` | Max USD cost per live order (≤ $25) |
| `LIVE_ORDER_COOLDOWN_S` | `10.0` | Min seconds between live order attempts |
| `LIVE_ALLOW_EXISTING_POSITIONS` | `false` | Allow startup with existing Kalshi positions |
| `KALSHI_REST_BASE` | env-based | Override Kalshi REST base URL |
| `KALSHI_WS_BASE` | env-based | Override Kalshi WebSocket URL |
| `DASHBOARD_HOST` | `127.0.0.1` | Dashboard bind host |
| `DASHBOARD_PORT` | `8000` | Dashboard port |

## Logs

Event rows are buffered in memory and flushed to CSV files under `logs/` every
5 seconds. Each session produces a file named `logs/session_<unix_timestamp>.csv`.

Win rate and total trade count are persisted in `logs/lifetime_stats.json` and
loaded on startup, so the win rate shown in the dashboard accumulates across
sessions.

## Limitations

- Live P&L shown in the dashboard is local and estimated. Use Kalshi as the
  source of truth for actual fills, fees, and settled balances.
- No historical backtest runner.
- Contract discovery depends on `BTC_SERIES_TICKER` matching the current Kalshi
  market naming convention.
