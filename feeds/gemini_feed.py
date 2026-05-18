"""Gemini BTC/USD spot price feed via WebSocket v1."""
from __future__ import annotations

import asyncio
import json

import websockets

from config import Settings
from state.state_manager import StateManager

_WS_URL         = "wss://api.gemini.com/v1/marketdata/BTCUSD"
_RECONNECT_BASE = 1.0
_RECONNECT_MAX  = 30.0


class GeminiFeed:
    def __init__(self, state: StateManager, cfg: Settings):
        self.state = state
        self.cfg   = cfg

    async def run(self) -> None:
        delay = _RECONNECT_BASE
        while True:
            try:
                await self._connect()
                delay = _RECONNECT_BASE
            except Exception as exc:
                await self.state.log_event(f"Gemini feed error: {exc}")
                await asyncio.sleep(delay)
                delay = min(delay * 2.0, _RECONNECT_MAX)

    async def _connect(self) -> None:
        async with websockets.connect(
            _WS_URL,
            ping_interval=20,
            ping_timeout=10,
            open_timeout=15,
        ) as ws:
            _logged = False
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("type") != "update":
                    continue
                for event in msg.get("events", []):
                    if event.get("type") != "trade":
                        continue
                    price = float(event["price"])
                    if price <= 0:
                        continue
                    if not _logged:
                        _logged = True
                        await self.state.log_event(
                            f"Gemini feed connected (BTC/USD)  price={price:.2f}"
                        )
                    await self.state.update_exchange_price("gemini", price)
