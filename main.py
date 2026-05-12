"""
Entry point.  Starts all feeds, the strategy loop, paper trader, risk monitor,
the dashboard server, and the event logger — all concurrently via asyncio.gather.
"""
from __future__ import annotations

import asyncio
import signal as _signal
import sys

import uvicorn

from config import settings
from dashboard.app import app
from execution.live_trader import LiveTrader
from feeds.btc_feed import BtcFeed
from feeds.kalshi_ws import KalshiFeed
from logger.event_logger import EventLogger
from risk.risk_manager import RiskManager
from simulation.paper_trader import PaperTrader
from state.state_manager import StateManager
from strategy.scalper import Scalper


async def main() -> None:
    # ── Shared objects ───────────────────────────────────────────────────────
    logger  = EventLogger()
    state   = StateManager(
        starting_balance=settings.starting_balance,
        trading_mode=settings.trading_mode,
        taker_fee_pct=settings.kalshi_taker_fee_pct,
        momentum_threshold_usd=settings.momentum_threshold_usd,
    )
    risk    = RiskManager(cfg=settings, logger=logger)

    # Inject state manager into FastAPI app
    app.state.state_manager = state

    # ── Component instances ──────────────────────────────────────────────────
    kalshi_feed  = KalshiFeed(state=state, cfg=settings, logger=logger)
    btc_feed     = BtcFeed(state=state,     cfg=settings, logger=logger)
    scalper      = Scalper(state=state,    cfg=settings, logger=logger)
    trader = (
        LiveTrader(state=state, cfg=settings, logger=logger, risk=risk)
        if settings.trading_mode == "live"
        else PaperTrader(state=state, cfg=settings, logger=logger, risk=risk)
    )

    await state.log_event(
        f"Bot starting — env={settings.kalshi_env}  "
        f"mode={settings.trading_mode}  "
        f"threshold={settings.signal_threshold:.0%}  "
        f"max_pos={settings.max_concurrent_positions}"
    )

    # ── Uvicorn config (non-blocking) ────────────────────────────────────────
    uv_config = uvicorn.Config(
        app=app,
        host=settings.dashboard_host,
        port=settings.dashboard_port,
        log_level="warning",
    )
    uv_server = uvicorn.Server(uv_config)

    # ── Graceful shutdown on Ctrl+C ──────────────────────────────────────────
    loop = asyncio.get_running_loop()

    def _shutdown(*_):
        print("\nShutting down…")
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for sig in (_signal.SIGINT, _signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown)

    print(
        f"\n  Kalshi BTC Bot\n"
        f"  Dashboard → http://{settings.dashboard_host}:{settings.dashboard_port}\n"
        f"  Env: {settings.kalshi_env}  |  Series: {settings.btc_series_ticker}\n"
        f"  Mode: {settings.trading_mode.upper()}\n"
        f"  BTC feed: Coinbase BTC-USD\n"
    )

    await asyncio.gather(
        logger.flush_loop(),
        state.broadcast_loop(),
        kalshi_feed.run(),
        btc_feed.run(),
        scalper.run(),
        trader.run(),
        risk.run(state),
        uv_server.serve(),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, asyncio.CancelledError):
        sys.exit(0)
