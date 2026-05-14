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
        self._traded: set[str] = set()
        self._stop_lossed: set[str] = set()  # one stop-loss allowed per contract

    async def startup(self) -> None:
        """Call once at boot. In live mode, seeds executor_bankroll from Kalshi."""
        if self.cfg.trading_mode == "live":
            balance = await self._fetch_kalshi_balance()
            if balance is not None:
                self.state.executor_bankroll = balance
                self.state._save_executor_bankroll()
                await self.state.log_event(
                    f"💰 Live mode — Kalshi balance: ${balance:.2f}"
                )
            else:
                await self.state.log_event(
                    "⚠ Live mode — could not fetch Kalshi balance, using persisted value"
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
        """Called every analysis tick. Fires at most once per contract window."""
        rec      = self.state.recommendation
        analysis = self.state.analysis
        contract = self.state.active_contract

        if not contract:
            return
        if analysis.get("phase") != "entry_open":
            return
        if rec["side"] is None:
            return
        if contract in self._traded:
            return

        side       = rec["side"]
        sizing     = rec.get("sizing", {})
        contracts  = sizing.get("contracts", 0)
        fill_price = rec.get("entry_price")

        # Edge gate cleared entry_price but lock restored the side for display.
        # Grab a fresh price from the current orderbook instead.
        if fill_price is None:
            ob = self.state.orderbook
            if side == "YES":
                fill_price = ob.best_ask()
            else:
                bb = ob.best_bid()
                fill_price = (100.0 - bb) if bb is not None else None

        if fill_price is None:
            await self.state.log_event(
                f"⚠ No trade ({side}): orderbook empty — no price available"
            )
            return

        # Kelly was computed with a stale/zero price if edge gates blocked; recompute.
        if contracts <= 0:
            from strategy.scalper import _kelly_position_size
            sizing = _kelly_position_size(
                bankroll=self.state.executor_bankroll,
                entry_price_cents=fill_price,
                win_probability=self.state._pred_accuracy(lifetime=False) or 0.91,
            )
            contracts = sizing.get("contracts", 0)

        if contracts <= 0:
            reason = sizing.get("reason", "insufficient edge or bankroll")
            await self.state.log_event(
                f"⚠ No trade ({side} @ {fill_price:.1f}¢): Kelly says skip — {reason}"
            )
            return

        self._traded.add(contract)

        if self.cfg.trading_mode == "paper":
            await self._paper_fill(contract, rec["side"], contracts, fill_price)
        else:
            await self._live_order(contract, rec["side"], contracts, fill_price)

    # ── Stop-loss ─────────────────────────────────────────────────────────────

    async def maybe_stop_loss(self) -> None:
        """
        Called every tick before maybe_trade. Sells an open position early when
        GBM strongly flips against it, then clears the trade lock so maybe_trade
        can immediately re-enter in the opposite direction if edge conditions allow.

        Guards:
          - GBM must strongly oppose the position (≤15% for YES, ≥85% for NO)
          - ≥180 s must remain — too late to act in the last 3 min
          - Sell price must be ≥8¢ — don't sell for scraps into a one-sided book
          - One stop-loss per contract per session
        """
        pos = self.state.position
        if pos["status"] != "open":
            return

        contract = pos["ticker"]
        if not contract or contract in self._stop_lossed:
            return

        tau = max(0.0, self.state.window_close_ts - time.time())
        if tau < 180.0:
            return

        fv   = self.state.prediction_yes_pct
        side = pos["side"]
        gbm_strongly_opposes = (
            (side == "YES" and fv <= 15.0) or
            (side == "NO"  and fv >= 85.0)
        )
        if not gbm_strongly_opposes:
            return

        ob = self.state.orderbook
        if side == "YES":
            sell_price = ob.best_bid()
        else:
            yes_ask = ob.best_ask()
            sell_price = (100.0 - yes_ask) if yes_ask is not None else None

        if sell_price is None or sell_price < 8.0:
            return  # book too thin to exit safely

        contracts  = pos["contracts"]
        fill_price = pos["fill_price"]

        self._stop_lossed.add(contract)
        self._traded.discard(contract)  # allow re-entry in opposite direction

        if self.cfg.trading_mode == "paper":
            await self._paper_stop_loss(contract, side, contracts, fill_price, sell_price)
        else:
            await self._live_stop_loss(contract, side, contracts, fill_price, sell_price)

    async def _paper_stop_loss(
        self, ticker: str, side: str, contracts: int, fill_price: float, sell_price: float
    ) -> None:
        await self.state.stop_position(ticker, sell_price)
        pnl = self.state.position["pnl"]
        await self.state.log_event(
            f"🛑 STOP-LOSS {side}  {contracts} × {fill_price:.1f}¢ → {sell_price:.1f}¢  "
            f"PnL ${pnl:+.2f}  balance ${self.state.executor_bankroll:.2f}"
        )
        await self.logger.log("stop_loss", {
            "ticker":     ticker,
            "side":       side,
            "contracts":  contracts,
            "fill_price": fill_price,
            "sell_price": sell_price,
            "pnl":        pnl,
            "mode":       "paper",
        })

    async def _live_stop_loss(
        self, ticker: str, side: str, contracts: int, fill_price: float, sell_price_est: float
    ) -> None:
        path = "/trade-api/v2/portfolio/orders"
        url  = f"{self.cfg.kalshi_rest_base}/portfolio/orders"
        headers = _make_rest_headers(self.cfg, "POST", path)
        body = {
            "ticker": ticker,
            "action": "sell",
            "side":   side.lower(),
            "count":  contracts,
            "type":   "market",
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, headers=headers, json=body)
                resp.raise_for_status()
                order_id = resp.json().get("order", {}).get("order_id", "?")

            actual_sell = await self._fetch_fill_price(order_id, sell_price_est)
            await self.state.stop_position(ticker, actual_sell)
            pnl = self.state.position["pnl"]
            await self.state.log_event(
                f"🛑 LIVE STOP-LOSS {side}  {contracts} × {fill_price:.1f}¢ → {actual_sell:.1f}¢  "
                f"PnL ${pnl:+.2f}  order={order_id}  balance ${self.state.executor_bankroll:.2f}"
            )
            await self.logger.log("stop_loss", {
                "ticker":     ticker,
                "side":       side,
                "contracts":  contracts,
                "fill_price": fill_price,
                "sell_price": actual_sell,
                "pnl":        pnl,
                "order_id":   order_id,
                "mode":       "live",
            })
        except Exception as exc:
            # Roll back so the position stays open and stop-loss can retry
            self._stop_lossed.discard(contract := ticker)
            self._traded.add(contract)
            await self.state.log_event(f"❌ Stop-loss failed ({ticker}): {exc}")
            await self.logger.log("stop_loss_error", {"ticker": ticker, "error": str(exc)})

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
