"""Binance spot BTC/USDT top-20 orderbook depth imbalance — 100 ms WebSocket."""
from __future__ import annotations

import asyncio
import json

import websockets

from config import Settings
from state.state_manager import StateManager

_WS_URL = "wss://fstream.binance.com/ws/btcusdt@depth20@100ms"
_RECONNECT_BASE = 1.0
_RECONNECT_MAX = 30.0


class BinanceDepthFeed:
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
                await self.state.log_event(f"Binance depth feed error: {exc}")
                await asyncio.sleep(delay)
                delay = min(delay * 2.0, _RECONNECT_MAX)

    async def _connect(self) -> None:
        async with websockets.connect(
            _WS_URL,
            ping_interval=20,
            ping_timeout=10,
            open_timeout=15,
        ) as ws:
            await self.state.log_event("Binance depth feed connected (BTC/USDT depth20@100ms)")
            async for raw in ws:
                msg = json.loads(raw)
                imbalance = _parse_imbalance(msg)
                if imbalance is not None:
                    await self.state.update_binance_depth(imbalance)


def _parse_imbalance(msg: dict) -> float | None:
    """
    Compute bid/ask quantity imbalance across the top 10 levels on each side.
    Returns a value in [-1, +1]: +1 = all bids, -1 = all asks.

    Futures depth20 stream uses "b"/"a"; spot uses "bids"/"asks".
    We try both so the feed is format-agnostic.
    """
    try:
        bids = (msg.get("b") or msg.get("bids") or [])[:10]
        asks = (msg.get("a") or msg.get("asks") or [])[:10]
        if not bids or not asks:
            return None
        bid_qty = sum(float(qty) for _, qty in bids)
        ask_qty = sum(float(qty) for _, qty in asks)
        total = bid_qty + ask_qty
        if total == 0.0:
            return None
        return (bid_qty - ask_qty) / total
    except Exception:
        return None
