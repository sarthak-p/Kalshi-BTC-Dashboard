"""Kraken PI_XBTUSD perpetual futures ticker feed.

Provides perp mark price and index price via futures.kraken.com/ws/v1.
The basis (mark − index) leads spot price by a few seconds because
institutional flow hits the perp before spot exchanges reprice.
"""
from __future__ import annotations

import asyncio
import json

import websockets

from config import Settings
from state.state_manager import StateManager

_WS_URL         = "wss://futures.kraken.com/ws/v1"
_PRODUCT_ID     = "PI_XBTUSD"
_RECONNECT_BASE = 1.0
_RECONNECT_MAX  = 30.0


class KrakenPerpFeed:
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
                await self.state.log_event(f"Kraken perp feed error: {exc}")
                await asyncio.sleep(delay)
                delay = min(delay * 2.0, _RECONNECT_MAX)

    async def _connect(self) -> None:
        async with websockets.connect(
            _WS_URL,
            ping_interval=20,
            ping_timeout=10,
            open_timeout=15,
        ) as ws:
            await ws.send(json.dumps({
                "event": "subscribe",
                "feed": "ticker",
                "product_ids": [_PRODUCT_ID],
            }))
            _logged = False
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("feed") != "ticker" or msg.get("product_id") != _PRODUCT_ID:
                    continue
                bid   = msg.get("bid")
                ask   = msg.get("ask")
                index = msg.get("index")
                if bid is None or ask is None or index is None:
                    continue
                mid = (float(bid) + float(ask)) / 2.0
                if not _logged:
                    _logged = True
                    await self.state.log_event(
                        f"Kraken perp feed live  mark={mid:.1f}  index={float(index):.1f}  "
                        f"basis={mid - float(index):+.1f}"
                    )
                await self.state.update_perp_data(mid, float(index))
