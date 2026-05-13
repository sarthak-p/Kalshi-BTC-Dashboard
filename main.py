"""
Entry point. Starts BTC feed, Kalshi feed, analyzer, dashboard server,
and event logger — all concurrently via asyncio.gather.
"""
from __future__ import annotations

import asyncio
import signal as _signal
import sys

import uvicorn

from config import settings
from dashboard.app import app
from feeds.btc_feed import BtcFeed
from feeds.kalshi_ws import KalshiFeed
from logger.event_logger import EventLogger
from state.state_manager import StateManager
from strategy.scalper import Analyzer


async def main() -> None:
    logger  = EventLogger()
    state   = StateManager(momentum_threshold_usd=settings.momentum_threshold_usd, 
                           starting_bankroll=settings.bankroll,)
    app.state.state_manager = state

    kalshi_feed = KalshiFeed(state=state, cfg=settings, logger=logger)
    btc_feed    = BtcFeed(state=state, cfg=settings, logger=logger)
    analyzer    = Analyzer(state=state, cfg=settings, logger=logger)

    await state.log_event(
        f"Dashboard started — env={settings.kalshi_env}  "
        f"entry={settings.min_entry_price_cents:.0f}–{settings.max_entry_price_cents:.0f}¢  "
        f"momentum=${settings.momentum_entry_usd:.0f}  "
        f"bankroll=${state.bankroll:.2f}"
    )

    uv_config = uvicorn.Config(
        app=app,
        host=settings.dashboard_host,
        port=settings.dashboard_port,
        log_level="warning",
    )
    uv_server = uvicorn.Server(uv_config)

    loop = asyncio.get_running_loop()

    def _shutdown(*_):
        print("\nShutting down…")
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for sig in (_signal.SIGINT, _signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown)

    print(
        f"\n  Kalshi BTC Dashboard\n"
        f"  Open → http://{settings.dashboard_host}:{settings.dashboard_port}\n"
        f"  Env: {settings.kalshi_env}  |  Series: {settings.btc_series_ticker}\n"
        f"  BTC feed: Coinbase BTC-USD\n"
    )

    await asyncio.gather(
        logger.flush_loop(),
        state.broadcast_loop(),
        kalshi_feed.run(),
        btc_feed.run(),
        analyzer.run(),
        uv_server.serve(),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, asyncio.CancelledError):
        sys.exit(0)
