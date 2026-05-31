"""
Live trade executor — places real orders on Kalshi via the REST API.

On lock: for small GBM gaps (<15¢) posts a maker limit at min(bid+1, ask-1);
for large gaps (≥15¢) takes the ask directly. Holds the order for the entire
entry window, polling for fill every 2 s. Cancels if GBM goes neutral or window
closes. Position sizing is flat: cfg.trade_size_usd per trade.

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

_BALANCE_SYNC_S = 30.0
_ORDER_POLL_S   = 2.0   # how often to poll Kalshi for limit order fill status


class LiveExecutor(Executor):
    """Real-money executor. Subclasses Executor; overrides entry and close."""

    def __init__(self, state: StateManager, cfg: Settings, logger=None):
        super().__init__(state, cfg, logger)
        self._pending_order_id: Optional[str] = None
        self._pending_contract: Optional[str] = None
        self._pending_side:     Optional[str] = None
        self._pending_n:        int   = 0
        self._pending_price:    float = 0.0
        self._last_poll_ts:     float = 0.0

    # ── Startup ───────────────────────────────────────────────────────────────

    async def startup(self) -> None:
        self.state.trading_mode = "live"
        await self._sync_balance(is_startup=True)
        await self.state.log_event(
            f"🟢 Live — balance ${self.state.executor_bankroll:.2f}"
        )
        asyncio.ensure_future(self._balance_sync_loop())

    # ── Entry override ────────────────────────────────────────────────────────

    async def maybe_trade(self) -> None:
        contract = self.state.active_contract

        # ── Exit checks for open positions ────────────────────────────────────
        pos    = self.state.position
        in_pos = pos["status"] == "open" and pos["ticker"] == contract



        # Cancel stale pending order when contract changes
        if self._pending_order_id and self._pending_contract != contract:
            await self._cancel_order(self._pending_order_id)
            self._clear_pending()

        # Manage existing pending order for the current contract
        if self._pending_order_id:
            await self._manage_pending_order()
            return

        # Run all entry guards
        entry = await self._prepare_trade()
        if entry is None:
            return

        # Always take the ask — matches backtest fill assumption, eliminates cancelled orders
        ob   = self.state.orderbook
        side = entry["side"]

        if side == "YES":
            ask = ob.best_ask()
            limit_price = ask if ask is not None else entry["price"]
        else:
            yes_bid = ob.best_bid()
            limit_price = (100.0 - yes_bid) if yes_bid is not None else entry["price"]

        if limit_price < 20.0:
            await self.state.log_event(
                f"⏭ Skipped {side} — entry {limit_price:.0f}¢ below 20¢ floor (GBM/slope noise)"
            )
            self._attempted_contract = entry["contract"]
            return

        n_contracts = max(1, int(self.cfg.trade_size_usd / (limit_price / 100.0)))
        yes_price   = _to_yes_price(entry["side"], limit_price)

        order_id = await self._place_order(
            "buy", entry["contract"], entry["side"], n_contracts, yes_price
        )
        if order_id is None:
            await self.state.log_event(
                f"❌ Limit order failed: {entry['side']} {n_contracts}×{limit_price:.0f}¢"
            )
            self._attempted_contract = entry["contract"]
            return

        self._pending_order_id = order_id
        self._pending_contract = entry["contract"]
        self._pending_side     = entry["side"]
        self._pending_n        = n_contracts
        self._pending_price    = limit_price
        self._last_poll_ts     = time.monotonic()
        await self.state.log_event(
            f"⏳ {entry['side']} taker {n_contracts}×{limit_price:.0f}¢"
            f"  gap {entry['gap']:+.1f}¢"
        )

    async def _manage_pending_order(self) -> None:
        # Cancel if GBM goes neutral while the order is live
        current_fv = self.state.analysis.get("fv")
        if current_fv is not None:
            side = self._pending_side
            if (side == "NO" and current_fv >= 50.0) or (side == "YES" and current_fv <= 50.0):
                contract = self._pending_contract
                await self._cancel_order(self._pending_order_id)
                await self.state.log_event(
                    f"⏳ {side} limit cancelled — GBM neutral {current_fv:.0f}¢"
                )
                self._clear_pending()
                self._attempted_contract = contract
                return

        # Cancel when the entry window has closed
        phase = self.state.analysis.get("phase")
        if phase not in ("entry_open",):
            contract = self._pending_contract
            await self._cancel_order(self._pending_order_id)
            await self.state.log_event(
                f"⏳ {self._pending_side} limit cancelled — window closing"
            )
            self._clear_pending()
            self._attempted_contract = contract
            return

        # Poll for fill every _ORDER_POLL_S seconds
        now = time.monotonic()
        if now - self._last_poll_ts < _ORDER_POLL_S:
            return
        self._last_poll_ts = now

        if not await self._check_order_filled(self._pending_order_id):
            return

        # Confirmed fill — record position
        contract = self._pending_contract
        cost = round(self._pending_n * self._pending_price / 100.0, 2)
        await self.state.open_position(
            contract, self._pending_side, self._pending_n, self._pending_price, "live"
        )
        await self.state.log_event(
            f"🟢 LIVE {self._pending_side}  {self._pending_n}×{self._pending_price:.1f}¢"
            f"  cost ${cost:.2f}  balance ${self.state.executor_bankroll:.2f}"
        )
        await self._sync_balance()
        self._attempted_contract = contract
        self._clear_pending()

    def _clear_pending(self) -> None:
        self._pending_order_id = None
        self._pending_contract = None
        self._pending_side     = None
        self._pending_n        = 0
        self._pending_price    = 0.0
        self._last_poll_ts     = 0.0

    # ── Kalshi REST helpers ───────────────────────────────────────────────────

    async def _place_order(
        self, action: str, ticker: str, side: str, count: int, yes_price: int,
    ) -> Optional[str]:
        url  = self.cfg.kalshi_rest_base + "/portfolio/orders"
        path = urlparse(url).path
        headers = _make_rest_headers(self.cfg, "POST", path)
        body = {
            "action":          action,
            "client_order_id": str(uuid.uuid4()),
            "count":           count,
            "side":            side.lower(),
            "ticker":          ticker,
            "type":            "limit",
            "yes_price":       yes_price,
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, headers=headers, json=body)
                resp.raise_for_status()
                return resp.json().get("order", {}).get("order_id")
        except Exception as exc:
            await self.state.log_event(f"❌ Order API error: {exc}")
            return None

    async def _check_order_filled(self, order_id: str) -> bool:
        url  = self.cfg.kalshi_rest_base + f"/portfolio/orders/{order_id}"
        path = urlparse(url).path
        try:
            headers = _make_rest_headers(self.cfg, "GET", path)
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                return resp.json().get("order", {}).get("status", "") == "executed"
        except Exception:
            return False

    async def _cancel_order(self, order_id: str) -> None:
        url  = self.cfg.kalshi_rest_base + f"/portfolio/orders/{order_id}"
        path = urlparse(url).path
        headers = _make_rest_headers(self.cfg, "DELETE", path)
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.delete(url, headers=headers)
        except Exception:
            pass

    async def _fetch_kalshi_balance(self) -> Optional[float]:
        url  = self.cfg.kalshi_rest_base + "/portfolio/balance"
        path = urlparse(url).path
        headers = _make_rest_headers(self.cfg, "GET", path)
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
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
    """Convert internal price (¢) to the Kalshi yes_price integer."""
    if side == "YES":
        return int(round(price))
    return int(round(100.0 - price))
