"""Coinbase BTC-USD reference price feed."""
from __future__ import annotations

import asyncio
import json

import websockets

from config import Settings
from logger.event_logger import EventLogger
from state.state_manager import StateManager

RECONNECT_BASE = 1.0
RECONNECT_MAX = 60.0


class BtcFeed:
    def __init__(self, state: StateManager, cfg: Settings, logger: EventLogger):
        self.state = state
        self.cfg = cfg
        self.logger = logger

    async def run(self) -> None:
        await self._run_loop(self._connect_coinbase)

    # ── Reconnect harness ────────────────────────────────────────────────────

    async def _run_loop(self, connector) -> None:
        delay = RECONNECT_BASE
        while True:
            try:
                await connector()
                delay = RECONNECT_BASE
            except Exception as exc:
                await self.state.log_event(f"BTC price feed error: {exc}")
                await self.logger.log("btc_feed_error", {"err": str(exc)})
                await asyncio.sleep(delay)
                delay = min(delay * 2.0, RECONNECT_MAX)

    # ── Coinbase Advanced Trade ──────────────────────────────────────────────

    async def _connect_coinbase(self) -> None:
        async with websockets.connect(
            self.cfg.coinbase_ws_url,
            ping_interval=20,
            ping_timeout=10,
            open_timeout=15,
        ) as ws:
            await self.state.log_event("BTC price feed connected (Coinbase BTC-USD)")
            await ws.send(json.dumps({
                "type": "subscribe",
                "channel": "ticker",
                "product_ids": ["BTC-USD"],
            }))
            await ws.send(json.dumps({
                "type": "subscribe",
                "channel": "market_trades",
                "product_ids": ["BTC-USD"],
            }))
            async for raw in ws:
                msg = json.loads(raw)
                price = _parse_coinbase_price(msg)
                if price:
                    await self.state.update_btc(price)
                trades = _parse_coinbase_trades(msg)
                for size, is_buy in trades:
                    await self.state.update_cvd_trade(size, is_buy)


# ── Coinbase message parser ───────────────────────────────────────────────────

def _parse_coinbase_trades(msg: dict) -> list[tuple[float, bool]]:
    """
    Coinbase Advanced Trade market_trades message shape:
    {"channel": "market_trades", "events": [{"trades": [{"side": "BUY"/"SELL", "size": "0.01", ...}]}]}
    Returns list of (size, is_buy) — BUY = buyer hit ask = bullish CVD.
    """
    if msg.get("channel") != "market_trades":
        return []
    result = []
    for event in msg.get("events", []):
        for trade in event.get("trades", []):
            try:
                result.append((float(trade["size"]), trade["side"] == "BUY"))
            except (KeyError, TypeError, ValueError):
                pass
    return result


def _parse_coinbase_price(msg: dict) -> float | None:
    """
    Coinbase Advanced Trade ticker message shape:
    {
      "channel": "ticker",
      "events": [{"type": "snapshot"|"update", "tickers": [{"price": "94750.01", ...}]}]
    }
    """
    if msg.get("channel") != "ticker":
        return None
    for event in msg.get("events", []):
        for ticker in event.get("tickers", []):
            raw = ticker.get("price")
            if raw:
                try:
                    return float(raw)
                except (TypeError, ValueError):
                    pass
    return None
