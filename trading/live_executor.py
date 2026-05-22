"""
Live trade executor — places real orders on Kalshi via the REST API.

Mirrors the paper Executor exactly: same entry logic, same stop-loss threshold,
same position sizing, same wick/slope guards.  The only difference is that
_paper_fill and _paper_close place and await real Kalshi limit orders instead
of simulating fills at the current market price.

Balance is fetched from Kalshi on startup and re-synced every 30 s (and after
every fill/close) so the dashboard always reflects the real account balance.

Switch between paper and live with TRADING_MODE=paper|live in .env.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from typing import Optional
from urllib.parse import urlparse

import httpx

from config import Settings
from feeds.kalshi_ws import _make_rest_headers
from state.state_manager import StateManager
from trading.executor import Executor

_FILL_TIMEOUT_S   = 10.0   # seconds to wait for a limit order to execute
_FILL_POLL_S      = 0.5    # poll interval while waiting
_BALANCE_SYNC_S   = 30.0   # periodic balance sync interval


class LiveExecutor(Executor):
    """Real-money executor.  Subclasses Executor; overrides fill/close only."""

    # Live position sizing — tune these independently of paper mode.
    _BASE_SIZE_USD: float = 15.0
    _MAX_SIZE_USD:  float = 20.0
    _MIN_SIZE_USD:  float = 10.0

    # ── Startup ───────────────────────────────────────────────────────────────

    async def startup(self) -> None:
        self.state.trading_mode = "live"
        await self._sync_balance(is_startup=True)
        await self.state.log_event(
            f"🟢 Live — balance ${self.state.executor_bankroll:.2f}"
        )
        asyncio.ensure_future(self._balance_sync_loop())

    # ── Fill / close overrides ────────────────────────────────────────────────

    async def _paper_fill(
        self,
        ticker: str,
        side: str,
        contracts: int,
        fill_price: float,
        size_usd: float = 150.0,
        gap: float = 0.0,
        signal_count: int = 0,
    ) -> None:
        yes_price = _to_yes_price(side, fill_price)
        order_id = await self._place_order("buy", ticker, side, contracts, yes_price)
        if order_id is None:
            await self.state.log_event(
                f"❌ Live order failed: {side} {contracts}×{fill_price:.1f}¢"
            )
            return

        filled = await self._await_fill(order_id)
        if not filled:
            await self._cancel_order(order_id)
            await self.state.log_event(
                f"❌ Live order timed out ({side} {contracts}×{fill_price:.1f}¢) — cancelled"
            )
            return

        cost = round(contracts * fill_price / 100.0, 2)
        await self.state.open_position(ticker, side, contracts, fill_price, "live")
        await self.state.log_event(
            f"🟢 LIVE {side}  {contracts}×{fill_price:.1f}¢  cost ${cost:.2f}  "
            f"[gap {gap:+.1f}¢  sigs {signal_count}]  "
            f"balance ${self.state.executor_bankroll:.2f}"
        )
        await self._sync_balance()

    async def _paper_close(self, ticker: str, pos: dict) -> None:
        side = pos["side"]
        ob = self.state.orderbook

        if side == "YES":
            sell_price = ob.best_bid()
        else:
            yes_ask = ob.best_ask()
            sell_price = (100.0 - yes_ask) if yes_ask is not None else None

        if sell_price is None:
            sell_price = pos["fill_price"]

        yes_price = _to_yes_price(side, sell_price)
        order_id = await self._place_order("sell", ticker, side, pos["contracts"], yes_price)
        if order_id is None:
            await self.state.log_event(f"❌ Live sell order failed: {side}")
            return

        filled = await self._await_fill(order_id)
        if not filled:
            await self._cancel_order(order_id)
            await self.state.log_event(f"❌ Live sell timed out — cancelled")
            return

        await self.state.stop_position(ticker, sell_price)
        pnl = self.state.position["pnl"]
        await self.state.log_event(
            f"🔴 LIVE Closed {side}  {pos['contracts']}×{pos['fill_price']:.1f}¢"
            f" → {sell_price:.1f}¢  PnL ${pnl:+.2f}  "
            f"balance ${self.state.executor_bankroll:.2f}"
        )
        await self._sync_balance()

    # ── Kalshi REST helpers ───────────────────────────────────────────────────

    async def _place_order(
        self, action: str, ticker: str, side: str, count: int, yes_price: int
    ) -> Optional[str]:
        url = self.cfg.kalshi_rest_base + "/portfolio/orders"
        path = urlparse(url).path
        headers = _make_rest_headers(self.cfg, "POST", path)
        body = {
            "action": action,
            "client_order_id": str(uuid.uuid4()),
            "count": count,
            "side": side.lower(),
            "ticker": ticker,
            "type": "limit",
            "yes_price": yes_price,
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, headers=headers, json=body)
                resp.raise_for_status()
                return resp.json().get("order", {}).get("order_id")
        except Exception as exc:
            await self.state.log_event(f"❌ Order API error: {exc}")
            return None

    async def _await_fill(self, order_id: str) -> bool:
        url = self.cfg.kalshi_rest_base + f"/portfolio/orders/{order_id}"
        path = urlparse(url).path
        deadline = time.monotonic() + _FILL_TIMEOUT_S
        while time.monotonic() < deadline:
            try:
                headers = _make_rest_headers(self.cfg, "GET", path)
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.get(url, headers=headers)
                    resp.raise_for_status()
                    status = resp.json().get("order", {}).get("status", "")
                if status == "executed":
                    return True
                if status in ("canceled", "cancelled"):
                    return False
            except Exception:
                pass
            await asyncio.sleep(_FILL_POLL_S)
        return False

    async def _cancel_order(self, order_id: str) -> None:
        url = self.cfg.kalshi_rest_base + f"/portfolio/orders/{order_id}"
        path = urlparse(url).path
        headers = _make_rest_headers(self.cfg, "DELETE", path)
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.delete(url, headers=headers)
        except Exception:
            pass

    async def _fetch_kalshi_balance(self) -> Optional[float]:
        url = self.cfg.kalshi_rest_base + "/portfolio/balance"
        path = urlparse(url).path
        headers = _make_rest_headers(self.cfg, "GET", path)
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                # Kalshi returns balance in cents
                cents = resp.json().get("balance", 0)
                return round(cents / 100.0, 2)
        except Exception as exc:
            await self.state.log_event(f"⚠ Balance fetch failed: {exc}")
            return None

    async def _sync_balance(self, is_startup: bool = False) -> None:
        balance = await self._fetch_kalshi_balance()
        if balance is None:
            return
        async with self.state._lock:
            self.state.executor_bankroll = balance
            if is_startup:
                self.state.executor_bankroll_original = balance
            self.state._save_executor_bankroll()
        self.state._dirty.set()

    async def _balance_sync_loop(self) -> None:
        while True:
            await asyncio.sleep(_BALANCE_SYNC_S)
            await self._sync_balance()


def _to_yes_price(side: str, price: float) -> int:
    """Convert internal price (¢) to the Kalshi yes_price integer.

    Kalshi always uses yes_price to represent the YES leg regardless of which
    side you're trading.  For YES orders, yes_price == the price we pay/receive.
    For NO orders, yes_price == 100 - (price we pay/receive for NO).
    """
    if side == "YES":
        return int(round(price))
    return int(round(100.0 - price))
