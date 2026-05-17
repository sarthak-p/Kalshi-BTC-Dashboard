"""
Binance Futures taker buy/sell ratio — derived from btcusdt@aggTrade on
fstream.binance.com (US-accessible, same domain as the working liq feed).

aggTrade field semantics:
  m = true  → buyer was the maker (passive limit order) → taker SOLD
  m = false → seller was the maker                      → taker BOUGHT

Maintains O(1) running totals over a 5-minute sliding window.
State is updated at most once per second to avoid spamming the broadcast.
buySellRatio = takerBuyVol / takerSellVol  (matches Binance REST definition)
"""
from __future__ import annotations

import asyncio
import json
from collections import deque

import websockets

from config import Settings
from state.state_manager import StateManager

_WS_URL         = "wss://fstream.binance.com/ws/btcusdt@aggTrade"
_WINDOW_S       = 300.0   # 5-minute rolling window
_UPDATE_EVERY_S = 1.0     # throttle state/broadcast updates
_RECONNECT_BASE = 1.0
_RECONNECT_MAX  = 30.0


class FuturesTakerFeed:
    def __init__(self, state: StateManager, cfg: Settings):
        self.state = state
        self.cfg   = cfg
        # Running totals — updated incrementally, no full-scan needed
        self._buy_trades:  deque[tuple[float, float]] = deque()  # (ts, qty)
        self._sell_trades: deque[tuple[float, float]] = deque()
        self._buy_vol:  float = 0.0
        self._sell_vol: float = 0.0
        self._last_push: float = 0.0  # last time we pushed to state

    async def run(self) -> None:
        delay = _RECONNECT_BASE
        while True:
            try:
                await self._connect()
                delay = _RECONNECT_BASE
            except Exception as exc:
                await self.state.log_event(f"Taker ratio feed error: {exc}")
                await asyncio.sleep(delay)
                delay = min(delay * 2.0, _RECONNECT_MAX)

    async def _connect(self) -> None:
        # Reset accumulators on each fresh connection so stale data doesn't
        # carry over into the new session's window.
        self._buy_trades.clear()
        self._sell_trades.clear()
        self._buy_vol  = 0.0
        self._sell_vol = 0.0
        self._last_push = 0.0

        async with websockets.connect(
            _WS_URL,
            ping_interval=20,
            ping_timeout=10,
            open_timeout=15,
        ) as ws:
            await self.state.log_event(
                "Taker ratio feed connected (fstream btcusdt@aggTrade, 5m rolling)"
            )
            _first_ratio_logged = False
            async for raw in ws:
                msg = json.loads(raw)
                # Skip frames that aren't aggTrade (heartbeats, sub confirmations, etc.)
                if msg.get("e") != "aggTrade":
                    continue
                ts_raw = msg.get("T")
                qty_raw = msg.get("q")
                m_val = msg.get("m")
                if ts_raw is None or qty_raw is None or m_val is None:
                    continue
                ts  = ts_raw / 1000.0
                qty = float(qty_raw)

                # Route by taker side
                if m_val:                 # buyer = maker → taker sold
                    self._sell_trades.append((ts, qty))
                    self._sell_vol += qty
                else:                     # seller = maker → taker bought
                    self._buy_trades.append((ts, qty))
                    self._buy_vol += qty

                # Evict entries that have aged out of the window (O(k) prune, k≈0)
                cutoff = ts - _WINDOW_S
                while self._buy_trades and self._buy_trades[0][0] < cutoff:
                    self._buy_vol -= self._buy_trades.popleft()[1]
                while self._sell_trades and self._sell_trades[0][0] < cutoff:
                    self._sell_vol -= self._sell_trades.popleft()[1]

                # Push to state at most once per second
                if self._sell_vol > 0 and (ts - self._last_push) >= _UPDATE_EVERY_S:
                    self._last_push = ts
                    ratio = self._buy_vol / self._sell_vol
                    await self.state.update_futures_taker_ratio(ratio)
                    if not _first_ratio_logged:
                        _first_ratio_logged = True
                        await self.state.log_event(
                            f"Taker ratio live: {ratio:.3f} "
                            f"(buy {self._buy_vol:.1f} / sell {self._sell_vol:.1f} BTC)"
                        )
