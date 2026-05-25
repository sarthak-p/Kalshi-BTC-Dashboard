"""
Live trade executor — places real orders on Kalshi via the REST API.

On lock: places a limit buy at min(current_ask, 75¢) and holds it for the
entire entry window. Polls for fill every 2 seconds. Cancels automatically
when the entry window closes (phase leaves entry_open). The 75¢ ceiling
ensures positive EV at the measured ~77% accuracy.

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
from trading.executor import Executor, _MAX_ENTRY_PRICE

_BALANCE_SYNC_S = 30.0
_ORDER_POLL_S   = 2.0   # how often to poll Kalshi for limit order fill status


class LiveExecutor(Executor):
    """Real-money executor. Subclasses Executor; overrides entry and close."""

    _BASE_SIZE_USD: float = 10.0
    _MAX_SIZE_USD:  float = 15.0
    _MIN_SIZE_USD:  float = 5.0

    def __init__(self, state: StateManager, cfg: Settings):
        super().__init__(state, cfg)
        self._pending_order_id:    Optional[str] = None
        self._pending_contract:    Optional[str] = None
        self._pending_side:        Optional[str] = None
        self._pending_n:           int   = 0
        self._pending_price:       float = 0.0
        self._pending_gap:         float = 0.0
        self._pending_signal_count: int  = 0
        self._last_poll_ts:        float = 0.0

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

        # Place limit at ceiling or better
        limit_price = min(entry["price"], _MAX_ENTRY_PRICE)
        n_contracts = max(1, int(entry["size_usd"] / (limit_price / 100.0)))
        yes_price   = _to_yes_price(entry["side"], limit_price)

        order_id = await self._place_order(
            "buy", entry["contract"], entry["side"], n_contracts, yes_price
        )
        if order_id is None:
            await self.state.log_event(
                f"❌ Limit order failed: {entry['side']} {n_contracts}×{limit_price:.0f}¢"
            )
            return

        self._pending_order_id     = order_id
        self._pending_contract     = entry["contract"]
        self._pending_side         = entry["side"]
        self._pending_n            = n_contracts
        self._pending_price        = limit_price
        self._pending_gap          = entry["gap"]
        self._pending_signal_count = entry["signal_count"]
        self._last_poll_ts         = time.monotonic()
        await self.state.log_event(
            f"⏳ {entry['side']} limit {n_contracts}×{limit_price:.0f}¢ placed"
            f"  [gap {entry['gap']:+.1f}¢  sigs {entry['signal_count']}]"
        )

    async def _manage_pending_order(self) -> None:
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
            f"  cost ${cost:.2f}  [gap {self._pending_gap:+.1f}¢"
            f"  sigs {self._pending_signal_count}]"
            f"  balance ${self.state.executor_bankroll:.2f}"
        )
        await self._sync_balance()
        self._attempted_contract = contract
        self._clear_pending()

    def _clear_pending(self) -> None:
        self._pending_order_id     = None
        self._pending_contract     = None
        self._pending_side         = None
        self._pending_n            = 0
        self._pending_price        = 0.0
        self._pending_gap          = 0.0
        self._pending_signal_count = 0
        self._last_poll_ts         = 0.0

    # ── Close override ────────────────────────────────────────────────────────

    async def _paper_close(self, ticker: str, pos: dict) -> None:
        side = pos["side"]
        yes_price = 1 if side == "YES" else 99  # sell at any available bid

        attempt = 0
        while True:
            attempt += 1
            order_id = await self._place_order(
                "sell", ticker, side, pos["contracts"], yes_price,
                reduce_only=True, time_in_force="immediate_or_cancel",
            )
            if order_id is None:
                await self.state.log_event(f"❌ Live sell order error (attempt {attempt})")
            else:
                await asyncio.sleep(0.2)

            still_open = await self._has_open_position(ticker)
            if not still_open:
                break
            await self.state.log_event(f"⚠ Sell retry {attempt} — position still open")
            await asyncio.sleep(1.0)

        ob = self.state.orderbook
        if side == "YES":
            sell_price = ob.best_bid() or pos["fill_price"]
        else:
            yes_ask = ob.best_ask()
            sell_price = (100.0 - yes_ask) if yes_ask is not None else pos["fill_price"]

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
        self, action: str, ticker: str, side: str, count: int, yes_price: int,
        reduce_only: bool = False, time_in_force: str | None = None,
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
        if reduce_only:
            body["reduce_only"] = True
        if time_in_force:
            body["time_in_force"] = time_in_force
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

    async def _has_open_position(self, ticker: str) -> bool:
        url  = self.cfg.kalshi_rest_base + f"/portfolio/positions?ticker={ticker}"
        path = urlparse(url).path
        try:
            headers = _make_rest_headers(self.cfg, "GET", path)
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                positions = resp.json().get("market_positions", [])
                return any(abs(p.get("position", 0)) > 0 for p in positions)
        except Exception:
            return True  # assume still open on error — keep retrying

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
