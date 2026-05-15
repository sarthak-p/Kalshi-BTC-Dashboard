"""
Trade executor — places one market order per contract window when the analyzer
signals a clear edge.

TRADING_MODE=paper  → simulated fills at current ask, no real orders placed
TRADING_MODE=live   → real orders via Kalshi REST API, balance synced from Kalshi

Paper mode uses the executor_bankroll (persisted in logs/executor_bankroll.json)
for Kelly sizing and P&L tracking.

Live mode fetches the real Kalshi available balance on startup and after each
settlement so Kelly sizing always reflects your actual account.
"""
from __future__ import annotations

import asyncio
import time

import httpx

from config import Settings
from feeds.kalshi_ws import _make_rest_headers
from logger.event_logger import EventLogger
from state.state_manager import StateManager


class Executor:
    def __init__(self, state: StateManager, cfg: Settings, logger: EventLogger):
        self.state  = state
        self.cfg    = cfg
        self.logger = logger
        self._traded: set[str] = set()  # kept for live_order error recovery

    async def startup(self) -> None:
        """Call once at boot. In live mode, seeds executor_bankroll from Kalshi."""
        if self.cfg.trading_mode == "live":
            balance = await self._fetch_kalshi_balance()
            if balance is not None:
                self.state.executor_bankroll = balance
                self.state.executor_bankroll_initial = balance
                self.state._save_executor_bankroll()
                await self.state.log_event(
                    f"💰 Live mode — Kalshi balance: ${balance:.2f}"
                )
            else:
                await self.state.log_event(
                    "⚠ Live mode — could not fetch Kalshi balance, using persisted value"
                )
        else:
            if self.cfg.paper_bankroll_reset > 0:
                self.state.executor_bankroll = self.cfg.paper_bankroll_reset
                self.state.executor_bankroll_initial = self.cfg.paper_bankroll_reset
                self.state._save_executor_bankroll()
                await self.state.log_event(
                    f"📄 Paper mode — balance reset to ${self.cfg.paper_bankroll_reset:.2f}"
                )
            else:
                await self.state.log_event(
                    f"📄 Paper mode — starting balance: ${self.state.executor_bankroll:.2f}"
                )

    async def sync_balance(self) -> None:
        """Re-fetch Kalshi balance after settlement (live mode only)."""
        if self.cfg.trading_mode != "live":
            return
        balance = await self._fetch_kalshi_balance()
        if balance is not None:
            self.state.executor_bankroll = balance
            self.state._save_executor_bankroll()
            self.state._dirty.set()

    async def maybe_trade(self) -> None:
        """Bias follower: enter or reverse based on technical bias. No gates, no signal voting."""
        contract = self.state.active_contract
        if not contract:
            return

        bias = self.state.pre_window_bias
        if bias == "neutral":
            return

        target_side = "YES" if bias == "up" else "NO"

        # Only trade when GBM agrees with the technical bias
        fv = self.state.prediction_yes_pct
        if target_side == "YES" and fv <= 55:
            return  # GBM not leaning YES — skip
        if target_side == "NO" and fv >= 45:
            return  # GBM not leaning NO — skip

        pos = self.state.position
        in_contract = pos["status"] == "open" and pos["ticker"] == contract

        # Already holding the right side — nothing to do
        if in_contract and pos["side"] == target_side:
            return

        # Bias switched — close current position at market before reversing
        if in_contract and pos["side"] != target_side:
            await self._paper_close_bias_switch(contract, pos)

        # Enter at current market price, no filtering
        ob = self.state.orderbook
        if target_side == "YES":
            price = ob.best_ask()
        else:
            yes_bid = ob.best_bid()
            price = (100.0 - yes_bid) if yes_bid is not None else None

        if not price:
            return

        # Don't trade outside the risk/reward range — same gate as recommendation panel
        if price < self.cfg.min_entry_price_cents or price > self.cfg.max_entry_price_cents:
            return

        n_contracts = max(1, int(self.state.executor_bankroll * 0.10 / (price / 100.0)))

        if self.cfg.trading_mode == "paper":
            await self._paper_fill(contract, target_side, n_contracts, price)
        elif self.cfg.trading_mode == "live":
            await self._live_order(contract, target_side, n_contracts, price)

    # ── Bias switch close ─────────────────────────────────────────────────────

    async def _paper_close_bias_switch(self, ticker: str, pos: dict) -> None:
        """Close an open paper position at market when bias switches direction."""
        side = pos["side"]
        ob   = self.state.orderbook
        if side == "YES":
            sell_price = ob.best_bid()
        else:
            yes_ask    = ob.best_ask()
            sell_price = (100.0 - yes_ask) if yes_ask is not None else None

        if sell_price is None:
            sell_price = pos["fill_price"]  # fallback to fill price if book is empty

        await self.state.stop_position(ticker, sell_price)
        pnl = self.state.position["pnl"]
        await self.state.log_event(
            f"🔄 BIAS SWITCH — closed {side}  {pos['contracts']} × {pos['fill_price']:.1f}¢ "
            f"→ {sell_price:.1f}¢  PnL ${pnl:+.2f}  balance ${self.state.executor_bankroll:.2f}"
        )
        await self.logger.log("bias_switch", {
            "ticker":     ticker,
            "side":       side,
            "contracts":  pos["contracts"],
            "fill_price": pos["fill_price"],
            "sell_price": sell_price,
            "pnl":        pnl,
        })

    # ── Paper trading ─────────────────────────────────────────────────────────

    async def _paper_fill(
        self, ticker: str, side: str, contracts: int, fill_price: float
    ) -> None:
        cost = round(contracts * fill_price / 100.0, 2)
        await self.state.open_position(ticker, side, contracts, fill_price, "paper")
        await self.state.log_event(
            f"📄 PAPER {side}  {contracts} contracts @ {fill_price:.1f}¢  cost ${cost:.2f}  "
            f"balance ${self.state.executor_bankroll:.2f}"
        )
        await self.logger.log("paper_trade", {
            "ticker":     ticker,
            "side":       side,
            "contracts":  contracts,
            "fill_price": fill_price,
            "cost":       cost,
            "balance":    self.state.executor_bankroll,
        })

    # ── Live trading ──────────────────────────────────────────────────────────

    async def _live_order(
        self, ticker: str, side: str, contracts: int, estimated_price: float
    ) -> None:
        path = "/trade-api/v2/portfolio/orders"
        url  = f"{self.cfg.kalshi_rest_base}/portfolio/orders"
        headers = _make_rest_headers(self.cfg, "POST", path)
        body = {
            "ticker": ticker,
            "action": "buy",
            "side":   side.lower(),
            "count":  contracts,
            "type":   "market",
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, headers=headers, json=body)
                resp.raise_for_status()
                order    = resp.json().get("order", {})
                order_id = order.get("order_id", "?")

            # Market orders fill immediately — fetch the order to get actual fill price
            actual_price = await self._fetch_fill_price(order_id, estimated_price)
            cost = round(contracts * actual_price / 100.0, 2)

            await self.state.open_position(ticker, side, contracts, actual_price, "live")
            await self.state.log_event(
                f"🟢 LIVE {side}  {contracts} contracts @ {actual_price:.1f}¢  "
                f"cost ${cost:.2f}  order={order_id}  balance ${self.state.executor_bankroll:.2f}"
            )
            await self.logger.log("live_order", {
                "ticker":          ticker,
                "side":            side,
                "contracts":       contracts,
                "estimated_price": estimated_price,
                "fill_price":      actual_price,
                "cost":            cost,
                "order_id":        order_id,
                "balance":         self.state.executor_bankroll,
            })

        except Exception as exc:
            self._traded.discard(ticker)
            await self.state.log_event(f"❌ Order failed ({ticker}): {exc}")
            await self.logger.log("order_error", {
                "ticker": ticker,
                "side":   side,
                "error":  str(exc),
            })

    # ── Kalshi API helpers ────────────────────────────────────────────────────

    async def _fetch_kalshi_balance(self) -> float | None:
        """Fetch available balance from Kalshi portfolio. Returns dollars or None."""
        path = "/trade-api/v2/portfolio/balance"
        url  = f"{self.cfg.kalshi_rest_base}/portfolio/balance"
        headers = _make_rest_headers(self.cfg, "GET", path)
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                data = resp.json()

            # Try every known Kalshi response shape:
            #   {"balance": 21199}               ← cents (integer)
            #   {"balance": 211.99}              ← dollars (float)
            #   {"available_balance": 21199}     ← alternative key
            #   {"balance": {"available_balance": 21199}}  ← nested
            raw = (
                data.get("balance")
                if not isinstance(data.get("balance"), dict)
                else data["balance"].get("available_balance")
            ) or data.get("available_balance")

            if raw is not None:
                val = float(raw)
                # Heuristic: Kalshi stores cents as integers > 500;
                # dollar values for reasonable accounts are < 500
                dollars = round(val / 100.0, 2) if val > 500 else round(val, 2)
                return dollars

            await self.logger.log("balance_fetch_error", {
                "error": "no balance field found", "response": str(data)[:200]
            })
        except Exception as exc:
            await self.logger.log("balance_fetch_error", {
                "error": str(exc), "url": url
            })
        return None

    async def _fetch_fill_price(self, order_id: str, fallback: float) -> float:
        """
        Fetch the actual average fill price for a market order.
        Falls back to our estimated price if the API call fails.
        """
        if order_id == "?":
            return fallback
        path = f"/trade-api/v2/portfolio/orders/{order_id}"
        url  = f"{self.cfg.kalshi_rest_base}/portfolio/orders/{order_id}"
        headers = _make_rest_headers(self.cfg, "GET", path)
        for _ in range(3):   # market orders fill fast — 3 quick retries
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.get(url, headers=headers)
                    resp.raise_for_status()
                    order = resp.json().get("order", {})
                avg_price = order.get("avg_price")
                if avg_price is not None:
                    # avg_price is in dollars (e.g. 0.68) → convert to cents
                    return round(float(avg_price) * 100.0, 1)
            except Exception:
                pass
            await asyncio.sleep(0.5)
        return fallback
