"""Binance Futures BTC real-time liquidation feed (forceOrder stream)."""
from __future__ import annotations

import asyncio
import json

import websockets

from config import Settings
from state.state_manager import StateManager

_WS_URL = "wss://fstream.binance.com/ws/btcusdt@forceOrder"
_RECONNECT_BASE = 1.0
_RECONNECT_MAX = 30.0


class BinanceLiqFeed:
    def __init__(self, state: StateManager, cfg: Settings):
        self.state = state
        self.cfg = cfg

    async def run(self) -> None:
        delay = _RECONNECT_BASE
        while True:
            try:
                await self._connect()
                delay = _RECONNECT_BASE
            except Exception as exc:
                await self.state.log_event(f"Binance liq feed error: {exc}")
                await asyncio.sleep(delay)
                delay = min(delay * 2.0, _RECONNECT_MAX)

    async def _connect(self) -> None:
        async with websockets.connect(
            _WS_URL,
            ping_interval=20,
            ping_timeout=10,
            open_timeout=15,
        ) as ws:
            await self.state.log_event("Binance liq feed connected (btcusdt@forceOrder)")
            async for raw in ws:
                parsed = _parse_liq(json.loads(raw))
                if parsed is not None:
                    side, usd_value = parsed
                    await self.state.update_liquidation(side, usd_value)


def _parse_liq(msg: dict) -> tuple[str, float] | None:
    """
    forceOrder message shape:
    {"e": "forceOrder", "o": {"S": "SELL"/"BUY", "q": "0.100", "p": "94000.00", ...}}

    SELL side order = long position was liquidated → label LONG.
    BUY  side order = short position was liquidated → label SHORT.
    """
    try:
        order = msg.get("o", {})
        ws_side = order.get("S", "")
        qty = float(order.get("q", 0))
        price = float(order.get("ap") or order.get("p", 0))
        if qty <= 0 or price <= 0:
            return None
        usd_value = qty * price
        side = "LONG" if ws_side == "SELL" else "SHORT"
        return side, usd_value
    except Exception:
        return None
