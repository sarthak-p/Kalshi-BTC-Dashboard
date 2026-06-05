"""
Live trade executor — places real orders on Kalshi via the REST API.

On lock: always takes the ask (taker order) to ensure immediate fills and avoid
adverse selection on maker limits. Holds the order for the entire
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
        self._pending_order_id:  Optional[str] = None
        self._pending_contract:  Optional[str] = None
        self._pending_side:      Optional[str] = None
        self._pending_n:         int   = 0
        self._pending_price:     float = 0.0
        self._pending_placed_at:   float = 0.0
        self._last_poll_ts:        float = 0.0
        self._ceiling_skip_logged: bool  = False
        self._floor_skip_logged:   bool  = False

    # ── Startup ───────────────────────────────────────────────────────────────

    async def startup(self) -> None:
        self.state.trading_mode = "live"
        await self._sync_balance(is_startup=True)
        await self._sync_position()
        await self.state.log_event(
            f"🟢 Live — balance ${self.state.executor_bankroll:.2f}"
        )
        asyncio.ensure_future(self._balance_sync_loop())

    # ── Entry override ────────────────────────────────────────────────────────

    async def maybe_trade(self) -> None:
        # Daily stop loss — halt trading if session down more than $50
        if self.state.executor_session_pnl < -50.0:
            return
        
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

        ob   = self.state.orderbook
        side = entry["side"]

        if side == "YES":
            ask = ob.best_ask()
            if ask is None:
                return
            limit_price = ask
        else:
            yes_bid = ob.best_bid()
            if yes_bid is None:
                return
            limit_price = 100.0 - yes_bid

        # ── 55¢ ceiling — keep retrying, don't set _attempted_contract ────────
        if limit_price > 55.0:
            if not self._ceiling_skip_logged:
                await self.state.log_event(
                    f"⏭ Skipped {side} — {limit_price:.0f}¢ above 55¢ ceiling"
                )
                self._ceiling_skip_logged = True
            return  # no _attempted_contract — retries next tick

        self._ceiling_skip_logged = False
        # ─────────────────────────────────────────────────────────────────────

        if limit_price < 20.0:
            if not self._floor_skip_logged:
                await self.state.log_event(
                    f"⏭ Skipped {side} — entry {limit_price:.0f}¢ below 20¢ floor"
                )
                self._floor_skip_logged = True
            return  # no _attempted_contract — retries next tick

        self._floor_skip_logged = False

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

        self._pending_order_id  = order_id
        self._pending_contract  = entry["contract"]
        self._pending_side      = entry["side"]
        self._pending_n         = n_contracts
        self._pending_price     = limit_price
        self._pending_placed_at = time.monotonic()
        self._last_poll_ts      = time.monotonic()
        await self.state.log_event(
            f"⏳ {entry['side']} taker {n_contracts}×{limit_price:.0f}¢"
            f"  gap {entry['gap']:+.1f}¢"
        )

    async def _manage_pending_order(self) -> None:
        # Cancel if GBM goes neutral while the order is live
        current_fv = self.state.analysis.get("fv")
        if current_fv is not None:
            side = self._pending_side
            if (side == "NO" and current_fv >= 55.0) or (side == "YES" and current_fv <= 45.0):
                contract = self._pending_contract
                await self._cancel_order(self._pending_order_id)
                if await self._check_order_filled(self._pending_order_id):
                    await self.state.log_event(
                        f"⏳ {side} cancel-raced fill — recording position"
                    )
                    # fall through to record position below
                else:
                    await self.state.log_event(
                        f"⏳ {side} limit cancelled — GBM reversed {current_fv:.0f}¢"
                    )
                    self._clear_pending()
                    self._attempted_contract = contract
                    return

        # Cancel when the entry window has closed
        phase = self.state.analysis.get("phase")
        if phase not in ("entry_open",):
            contract = self._pending_contract
            await self._cancel_order(self._pending_order_id)
            if await self._check_order_filled(self._pending_order_id):
                await self.state.log_event(
                    f"⏳ {self._pending_side} cancel-raced fill — recording position"
                )
                # fall through to record position below
            else:
                await self.state.log_event(
                    f"⏳ {self._pending_side} limit cancelled — window closing"
                )
                self._clear_pending()
                self._attempted_contract = contract
                return

        # Application-level IOC: cancel after 1.5s if not filled, then retry with fresh price
        now = time.monotonic()
        if now - self._pending_placed_at >= 1.5 and self._pending_placed_at > 0:
            filled = await self._check_order_filled(self._pending_order_id)
            if not filled:
                await self._cancel_order(self._pending_order_id)
                # Re-check: order may have filled between our check and the cancel
                filled = await self._check_order_filled(self._pending_order_id)
                if not filled:
                    await self.state.log_event(
                        f"⏳ {self._pending_side} order unfilled after 1.5s — retrying with fresh price"
                    )
                    self._clear_pending()
                    return  # no _attempted_contract — retries next tick at updated price
                # Filled during the cancel window — fall through to record position
            # Was filled during the check window — fall through to record position
        elif now - self._last_poll_ts < _ORDER_POLL_S:
            return
        else:
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
        self._pending_order_id  = None
        self._pending_contract  = None
        self._pending_side      = None
        self._pending_n         = 0
        self._pending_price     = 0.0
        self._pending_placed_at = 0.0
        self._last_poll_ts      = 0.0

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

    async def _fetch_kalshi_positions(self) -> list[dict]:
        url  = self.cfg.kalshi_rest_base + "/portfolio/positions"
        path = urlparse(url).path
        headers = _make_rest_headers(self.cfg, "GET", path)
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                return resp.json().get("market_positions", [])
        except Exception:
            return []

    async def _sync_position(self) -> None:
        """Reconcile bot position state against Kalshi's actual open positions."""
        contract = self.state.active_contract
        if not contract:
            return
        # Already tracking an open position — nothing to reconcile
        if self.state.position.get("status") == "open":
            return
        positions = await self._fetch_kalshi_positions()
        for p in positions:
            if p.get("ticker") != contract:
                continue
            yes_pos = p.get("position", 0)   # positive = long YES, negative = long NO
            if yes_pos == 0:
                continue
            side      = "YES" if yes_pos > 0 else "NO"
            contracts = abs(yes_pos)
            # Use current market mid as a best-effort fill price estimate
            ob        = self.state.orderbook
            bid, ask  = ob.best_bid(), ob.best_ask()
            if side == "YES":
                fill_price = ask or (bid or 50.0)
            else:
                fill_price = (100.0 - bid) if bid else 50.0
            await self.state.open_position(contract, side, contracts, fill_price, "live")
            await self.state.log_event(
                f"🔄 Position sync: found {side} {contracts}× on {contract} — recorded"
            )
            break

    async def _balance_sync_loop(self) -> None:
        while True:
            await asyncio.sleep(_BALANCE_SYNC_S)
            await self._sync_balance()
            await self._sync_position()


def _to_yes_price(side: str, price: float) -> int:
    """Convert internal price (¢) to the Kalshi yes_price integer."""
    if side == "YES":
        return int(round(price))
    return int(round(100.0 - price))
