# Kalshi BTC 15-Min Scalping Bot

Automated trading bot for Kalshi BTC 15-minute binary markets, with paper and live execution modes and a real-time FastAPI dashboard.

## Strategy

Each Kalshi `KXBTCD` contract is a binary: pays $1 if BTC closes at or above where it opened the 15-minute window, $0 otherwise. YES is the "BTC up" side, NO is the "BTC down" side.

The bot replicates a discretionary scalping approach:

**1. Monitor phase (first ~7 min — no trades)**
The bot watches the BTC price and Kalshi orderbook without acting. It uses this period to:
- Identify which direction BTC is sustaining momentum
- Check that the Kalshi contract is committing to one side of 50¢ (few line crossings)
- Confirm the price is drifting steadily away from 50¢, not oscillating

**2. Entry (last 2–8 min of the window)**
After the monitoring phase, if conditions align, the bot buys the contract on the side BTC is trending — but only when:
- The contract is priced at **60–85¢** (confirmed direction, still room to run)
- BTC has moved at least **$30** from the window open
- Pre-window BTC technicals (RSI, ADX, Bollinger Bands from Binance) agree with the direction
- The Kalshi price has crossed 50¢ no more than twice during monitoring
- At least 60% of recent price steps moved further from 50¢ (steady, not choppy)

**3. Exit**
- **Take profit:** +20¢ above entry
- **Stop loss:** −12¢ below entry
- **Force exit:** all positions closed 90 seconds before resolution — never holds to expiry
- **One-and-done:** after a winning trade, the bot sits out the rest of that window

**Why 60–85¢?**
Buying at 70¢ when the market is trending toward 100¢ gives a potential gain of ~30¢ on a 70¢ stake (43% return). The fixed take profit (+20¢) and stop loss (−12¢) give a 1.67:1 reward/risk ratio — profitable at any win rate above 38%.

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

Run in paper mode first (`TRADING_MODE=paper`) until you're satisfied with performance.

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
5. Confirm `BTC_SERIES_TICKER` matches the current Kalshi series name.

## Live Trading

To place real orders, set in `.env`:

```env
TRADING_MODE=live
LIVE_TRADING_ACK=I_UNDERSTAND_THIS_PLACES_REAL_ORDERS
LIVE_UNIT_SIZE=10
LIVE_MAX_ORDER_COST_USD=5.00
DAILY_LOSS_LIMIT_USD=5.0
```

Live mode is refused unless all credentials are present, the ack string matches exactly, `LIVE_MAX_ORDER_COST_USD ≤ 25`, and `DAILY_LOSS_LIMIT_USD ≤ 10`. Orders are fill-or-kill limits — no resting orders are left behind.

## Architecture

```text
main.py
  -> EventLogger.flush_loop()       async CSV flush every 5s
  -> StateManager.broadcast_loop()  WebSocket push to dashboard
  -> KalshiFeed.run()               REST contract discovery + WS orderbook
  -> BtcFeed.run()                  Coinbase BTC-USD reference price
  -> Scalper.run()                  signal generation + Binance technicals
  -> PaperTrader / LiveTrader       position management and exits
  -> RiskManager.run()              daily loss kill switch
  -> FastAPI/Uvicorn                dashboard server
```

All components share a single `StateManager` in-memory hub. Feeds write into it, the scalper reads from it, the trader records positions in it, and the dashboard streams snapshots from it over WebSocket.

## Technicals (Binance, free, no auth)

Every 60 seconds the bot fetches 50 one-minute candles from `api.binance.com` and computes:

| Indicator | Signal |
|---|---|
| RSI(14) < 40 | Bullish point |
| RSI(14) > 60 | Bearish point |
| BB(20) position < 0.4 (near lower band) | Bullish point |
| BB(20) position > 0.6 (near upper band) | Bearish point |

Majority of points sets the bias (`up` / `down` / `neutral`). When `BIAS_GATE_ENABLED=true`, the bot skips trades where the pre-window bias contradicts the BTC direction. ADX(14) is computed and logged but does not gate entries.

## Configuration Reference

| Variable | Default | Description |
|---|---|---|
| `KALSHI_ENV` | required | `demo` or `prod` |
| `KALSHI_API_KEY_ID` | empty | Kalshi API key ID |
| `KALSHI_PRIVATE_KEY_B64` | empty | Base64-encoded PEM private key |
| `BTC_SERIES_TICKER` | `KXBTCD` | Series ticker for contract auto-discovery |
| `TRADING_MODE` | `paper` | `paper` or `live` |
| `MAX_ENTRY_WINDOW_S` | `480` | Enter only when ≤ this many seconds remain (~last 8 min) |
| `MIN_ENTRY_WINDOW_S` | `120` | Stop new entries when < this many seconds remain |
| `MIN_ENTRY_PRICE_CENTS` | `60` | Minimum contract price to enter (¢) |
| `MAX_ENTRY_PRICE_CENTS` | `85` | Maximum contract price to enter (¢) |
| `MOMENTUM_ENTRY_USD` | `30` | Minimum BTC move from window open to confirm direction |
| `TAKE_PROFIT_CENTS` | `20` | Exit when position gains this many cents |
| `STOP_LOSS_CENTS` | `12` | Exit when position loses this many cents |
| `FORCE_EXIT_TAU_S` | `90` | Hard-close all positions this many seconds before resolution |
| `ONE_AND_DONE` | `true` | Sit out rest of window after a winning trade |
| `MAX_LINE_CROSSINGS` | `2` | Max times Kalshi mid may cross 50¢ during monitoring |
| `MIN_DIRECTION_CONSISTENCY` | `0.6` | Min fraction of recent steps trending away from 50¢ |
| `KALSHI_MID_MAX_RANGE_CENTS` | `22` | Max ¢ range in last 60s before market is too erratic |
| `BIAS_GATE_ENABLED` | `true` | Block entries when Binance RSI/BB contradicts direction |
| `BINANCE_SYMBOL` | `BTCUSDT` | Symbol for Binance klines fetch |
| `CONFIDENCE_THRESHOLD` | `0.45` | Minimum confidence score to act on a signal |
| `SIGNAL_DEBOUNCE_S` | `2.0` | Minimum seconds between signals |
| `MAX_CONCURRENT_POSITIONS` | `1` | Maximum open positions at once |
| `DAILY_LOSS_LIMIT_USD` | `5.0` | Kill switch triggers at this session loss |
| `MAX_POSITION_SIZE_USD` | `20.0` | Maximum paper position size |
| `MIN_OPEN_INTEREST` | `500` | Skip signals in thin markets |
| `NEW_WINDOW_SETTLE_S` | `15` | Wait this long after new contract discovery before trading |
| `MOMENTUM_THRESHOLD_USD` | `150` | BTC move in 10s that triggers a 30s signal pause |
| `STARTING_BALANCE` | `1000` | Paper trading starting balance |
| `LIVE_UNIT_SIZE` | `10` | Target contracts per live order |
| `LIVE_MAX_ORDER_COST_USD` | `5.00` | Max USD per live order (≤ $25) |
| `LIVE_ORDER_COOLDOWN_S` | `10.0` | Min seconds between live order attempts |
| `DASHBOARD_HOST` | `127.0.0.1` | Dashboard bind host |
| `DASHBOARD_PORT` | `8000` | Dashboard port |

## Project Layout

```text
main.py                      asyncio entry point
config.py                    settings (reads .env)
state/state_manager.py       shared in-memory hub, dataclasses, WebSocket broadcast
feeds/kalshi_ws.py           Kalshi REST/WS feed, auth, contract discovery, orderbook
feeds/btc_feed.py            Coinbase BTC-USD reference price feed
execution/kalshi_client.py   authenticated Kalshi REST order client
execution/live_trader.py     live FOK order execution and position monitor
strategy/scalper.py          signal generation, monitoring phase, entry gates
strategy/technicals.py       Binance klines fetch, RSI / ADX / BB computation
simulation/paper_trader.py   paper portfolio, take profit, stop loss, force exit
risk/risk_manager.py         position limits and daily loss kill switch
logger/event_logger.py       in-memory event buffer and async CSV flush
dashboard/app.py             FastAPI routes and WebSocket broadcast
dashboard/static/index.html  browser dashboard
logs/                        per-session CSV logs; lifetime_stats.json
```

## Notes

- **Fees:** Kalshi charges ~7% taker fee per trade leg. Each round trip costs ~14% of notional. Factor this into any profitability analysis.
- **Paper first:** the dashboard shows live P&L and win rate. Run in paper mode for at least a few days before going live.
- **Live P&L is estimated** — use Kalshi as the source of truth for actual fills and settled balances.
- **No backtest runner** is included. Historical performance must be inferred from session logs.
