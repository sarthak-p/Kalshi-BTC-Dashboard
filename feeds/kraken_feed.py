"""Kraken BTC/USD reference price feed (WebSocket v1)."""
from __future__ import annotations

import asyncio
import json

import websockets

from config import Settings
from logger.event_logger import EventLogger
from state.state_manager import StateManager

RECONNECT_BASE = 1.0
RECONNECT_MAX  = 5.0
_WS_URL        = "wss://ws.kraken.com"


class KrakenFeed:
    def __init__(self, state: StateManager, cfg: Settings, logger: EventLogger):
        self.state  = state
        self.cfg    = cfg
        self.logger = logger

    async def run(self) -> None:
        await self._run_loop(self._connect_kraken)

    async def _run_loop(self, connector) -> None:
        delay = RECONNECT_BASE
        while True:
            try:
                await connector()
                delay = RECONNECT_BASE
            except Exception as exc:
                await self.state.log_event(f"Kraken feed error: {exc}")
                await self.logger.log("kraken_feed_error", {"err": str(exc)})
                await asyncio.sleep(delay)
                delay = min(delay * 2.0, RECONNECT_MAX)

    async def _connect_kraken(self) -> None:
        async with websockets.connect(
            _WS_URL,
            ping_interval=20,
            ping_timeout=10,
            open_timeout=15,
        ) as ws:
            await self.state.log_event("Kraken price feed connected (XBT/USD)")
            await ws.send(json.dumps({
                "event": "subscribe",
                "pair": ["XBT/USD"],
                "subscription": {"name": "trade"},
            }))
            async for raw in ws:
                msg = json.loads(raw)
                price = _parse_kraken_price(msg)
                if price:
                    await self.state.update_exchange_price("kraken", price)


def _parse_kraken_price(msg) -> float | None:
    """
    Kraken v1 trade message shape:
    [channelID, [["price", "volume", "time", "side", "type", "misc"], ...], "trade", "XBT/USD"]
    Price is a string at index 0 of each trade entry.
    """
    if not isinstance(msg, list) or len(msg) < 4:
        return None
    if msg[-2] != "trade" or msg[-1] != "XBT/USD":
        return None
    trades = msg[1]
    if not trades:
        return None
    try:
        return float(trades[-1][0])
    except (IndexError, TypeError, ValueError):
        return None
