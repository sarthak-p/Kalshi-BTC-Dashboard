"""Bitstamp BTC-USD reference price feed."""
from __future__ import annotations

import asyncio
import json

import websockets

from config import Settings
from logger.event_logger import EventLogger
from state.state_manager import StateManager

RECONNECT_BASE = 1.0
RECONNECT_MAX  = 5.0
_WS_URL        = "wss://ws.bitstamp.net"


class BitstampFeed:
    def __init__(self, state: StateManager, cfg: Settings, logger: EventLogger):
        self.state  = state
        self.cfg    = cfg
        self.logger = logger

    async def run(self) -> None:
        await self._run_loop(self._connect_bitstamp)

    async def _run_loop(self, connector) -> None:
        delay = RECONNECT_BASE
        while True:
            try:
                await connector()
                delay = RECONNECT_BASE
            except Exception as exc:
                await self.state.log_event(f"Bitstamp feed error: {exc}")
                await self.logger.log("bitstamp_feed_error", {"err": str(exc)})
                await asyncio.sleep(delay)
                delay = min(delay * 2.0, RECONNECT_MAX)

    async def _connect_bitstamp(self) -> None:
        async with websockets.connect(
            _WS_URL,
            ping_interval=20,
            ping_timeout=10,
            open_timeout=15,
        ) as ws:
            await self.state.log_event("Bitstamp price feed connected (BTC/USD)")
            await ws.send(json.dumps({
                "event": "bts:subscribe",
                "data": {"channel": "live_trades_btcusd"},
            }))
            async for raw in ws:
                msg = json.loads(raw)
                price = _parse_bitstamp_price(msg)
                if price:
                    await self.state.update_exchange_price("bitstamp", price)


def _parse_bitstamp_price(msg: dict) -> float | None:
    """
    Bitstamp live_trades message shape:
    {"event": "trade", "channel": "live_trades_btcusd", "data": {"price": 94750.01, ...}}
    """
    if not isinstance(msg, dict):
        return None
    if msg.get("event") != "trade" or msg.get("channel") != "live_trades_btcusd":
        return None
    try:
        return float(msg["data"]["price"])
    except (KeyError, TypeError, ValueError):
        return None
