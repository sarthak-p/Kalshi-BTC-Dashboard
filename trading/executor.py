"""
Trade executor — follows the model recommendation panel and places orders.

TRADING_MODE=paper  → simulated fills at current market price, no real orders placed
TRADING_MODE=live   → real orders via Kalshi REST API, balance synced from Kalshi

Position sizing: flat $5 per trade regardless of bankroll.
Reversals (recommendation flips mid-window): close existing position at market,
then enter the new side — works correctly in both paper and live mode.
"""
from __future__ import annotations

import asyncio

import httpx

from config import Settings
from feeds.kalshi_ws import _make_rest_headers
from logger.event_logger import EventLogger
from state.state_manager import StateManager

_UNIT_SIZE_USD = 100.0  # fixed dollars risked per trade


class Executor:
    def __init__(self, state: StateManager, cfg: Settings, logger: EventLogger):
        self.state  = state
        self.cfg    = cfg
        self.logger = logger
        self._attempted_contract: str | None = None  # prevents retry spam on failed orders

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
            if self.cfg.paper_bankroll_reset > 0:
                self.state.executor_bankroll          = self.cfg.paper_bankroll_reset
                self.state.executor_bankroll_original = self.cfg.paper_bankroll_reset
                self.state.executor_all_time_trades   = 0
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
        """Follow the model's 8-min locked decision (final_model_side)."""
        contract = self.state.active_contract
        if not contract:
            self._attempted_contract = None  # reset for next window
            return

        # Don't retry a failed order in the same window — wait for the next contract
        if contract == self._attempted_contract:
            return

        # Only act once the model has locked its decision at the 8-min mark,
        # and only if that lock was set for this specific contract (not a prior window's lock).
        if not self.state.final_model_locked:
            return
        if self.state.final_model_contract != contract:
            return

        target_side = self.state.final_model_side
        if not target_side:
            return

        ob = self.state.orderbook
        if target_side == "YES":
            price = ob.best_ask()
        else:
            yes_bid = ob.best_bid()
            price = (100.0 - yes_bid) if yes_bid is not None else None

        if not price:
            return

        pos = self.state.position
        in_contract = pos["status"] == "open" and pos["ticker"] == contract

        # Already holding the right side — nothing to do
        if in_contract and pos["side"] == target_side:
            return

        # Recommendation flipped — only close if the new side is enterable at a valid price.
        # Skipping this check caused crystallising full losses (e.g. close NO at 3¢ when YES
        # is at 97¢, then being blocked from entering YES by the max-price ceiling).
        if in_contract and pos["side"] != target_side:
            await self._close_position(contract, pos)

        # Re-validate edge at execution — gap may have compressed since the model locked.
        # Uses the GBM stored at lock time so we're measuring against the same reference
        # that justified the trade, not a drifting live value.
        locked_fv = self.state.final_model_fv
        mid = (ob.best_bid() + ob.best_ask()) / 2.0 if ob.best_bid() and ob.best_ask() else None
        if mid is not None:
            edge = (locked_fv - mid) if target_side == "YES" else (mid - locked_fv)
            if edge < self.cfg.min_gbm_market_gap_cents:
                self._attempted_contract = contract
                await self.state.log_event(
                    f"⏭ Skipped {target_side} — edge gone: GBM {locked_fv:.0f}¢ vs market {mid:.0f}¢ "
                    f"(edge {edge:+.1f}¢, need {self.cfg.min_gbm_market_gap_cents:.0f}¢)"
                )
                return

        n_contracts = max(1, int(_UNIT_SIZE_USD / (price / 100.0)))

        if self.cfg.trading_mode == "paper":
            await self._paper_fill(contract, target_side, n_contracts, price)
        elif self.cfg.trading_mode == "live":
            await self._live_order(contract, target_side, n_contracts, price)

    # ── Close position (paper or live) ───────────────────────────────────────

    async def _close_position(self, ticker: str, pos: dict) -> None:
        """Close an open position at market — routes to paper or live path."""
        if self.cfg.trading_mode == "live":
            await self._live_close(ticker, pos)
        else:
            await self._paper_close(ticker, pos)

    async def _paper_close(self, ticker: str, pos: dict) -> None:
        side = pos["side"]
        ob   = self.state.orderbook
        if side == "YES":
            sell_price = ob.best_bid()
        else:
            yes_ask    = ob.best_ask()
            sell_price = (100.0 - yes_ask) if yes_ask is not None else None

        if sell_price is None:
            sell_price = pos["fill_price"]

        await self.state.stop_position(ticker, sell_price)
        pnl = self.state.position["pnl"]
        await self.state.log_event(
            f"🔄 FLIP — closed {side}  {pos['contracts']} × {pos['fill_price']:.1f}¢ "
            f"→ {sell_price:.1f}¢  PnL ${pnl:+.2f}  balance ${self.state.executor_bankroll:.2f}"
        )
        await self.logger.log("bias_switch", {
            "ticker":     ticker,
            "side":       side,
            "contracts":  pos["contracts"],
            "fill_price": pos["fill_price"],
            "sell_price": sell_price,
            "pnl":        pnl,
            "mode":       "paper",
        })

    async def _live_close(self, ticker: str, pos: dict) -> None:
        side = pos["side"]
        ob   = self.state.orderbook
        if side == "YES":
            est_sell = ob.best_bid() or pos["fill_price"]
        else:
            yes_ask  = ob.best_ask()
            est_sell = (100.0 - yes_ask) if yes_ask is not None else pos["fill_price"]

        # 20¢ below bid — fills immediately even if market moves before order lands
        aggressive_price = max(2, int(round(est_sell)) - 20)
        price_key = "yes_price" if side == "YES" else "no_price"
        path = "/trade-api/v2/portfolio/orders"
        url  = f"{self.cfg.kalshi_rest_base}/portfolio/orders"
        headers = _make_rest_headers(self.cfg, "POST", path)
        body = {
            "ticker":   ticker,
            "action":   "sell",
            "side":     side.lower(),
            "count":    pos["contracts"],
            "type":     "market",
            price_key:  aggressive_price,
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, headers=headers, json=body)
                if not resp.is_success:
                    detail = resp.text[:300]
                    await self.state.log_event(f"❌ Close rejected {resp.status_code} ({ticker}): {detail}")
                    await self.logger.log("close_error", {"ticker": ticker, "status": resp.status_code, "body": detail})
                    return
                order_id = resp.json().get("order", {}).get("order_id", "?")

            actual_sell = await self._fetch_fill_price(order_id, est_sell)

            # Verify the order actually filled — if not, abort the flip to avoid dual Kalshi positions
            if actual_sell is None:
                await self._cancel_order(order_id)
                await self.state.log_event(f"⚠ Close order unfilled — cancelled ({ticker}), holding position")
                await self.logger.log("close_unfilled", {"ticker": ticker, "order_id": order_id})
                return

            await self.state.stop_position(ticker, actual_sell)
            pnl = self.state.position["pnl"]
            await self.state.log_event(
                f"🔄 LIVE FLIP — closed {side}  {pos['contracts']} × {pos['fill_price']:.1f}¢ "
                f"→ {actual_sell:.1f}¢  PnL ${pnl:+.2f}  order={order_id}  "
                f"balance ${self.state.executor_bankroll:.2f}"
            )
            await self.logger.log("bias_switch", {
                "ticker":     ticker,
                "side":       side,
                "contracts":  pos["contracts"],
                "fill_price": pos["fill_price"],
                "sell_price": actual_sell,
                "pnl":        pnl,
                "order_id":   order_id,
                "mode":       "live",
            })
        except Exception as exc:
            await self.state.log_event(f"❌ Live close failed ({ticker}): {exc}")
            await self.logger.log("close_error", {"ticker": ticker, "error": str(exc)})

    # ── Paper fill ────────────────────────────────────────────────────────────

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

    # ── Live order ────────────────────────────────────────────────────────────

    async def _live_order(
        self, ticker: str, side: str, contracts: int, estimated_price: float
    ) -> None:
        path = "/trade-api/v2/portfolio/orders"
        url  = f"{self.cfg.kalshi_rest_base}/portfolio/orders"
        headers = _make_rest_headers(self.cfg, "POST", path)
        # 20¢ above ask — guarantees immediate fill even if market moves several ticks before order lands.
        # Actual fill price is determined by Kalshi matching engine (we pay current ask, not our limit).
        aggressive_price = min(98, int(round(estimated_price)) + 20)
        price_key = "yes_price" if side == "YES" else "no_price"
        body = {
            "ticker":   ticker,
            "action":   "buy",
            "side":     side.lower(),
            "count":    contracts,
            "type":     "market",
            price_key:  aggressive_price,
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, headers=headers, json=body)
                if not resp.is_success:
                    self._attempted_contract = ticker
                    detail = resp.text[:300]
                    await self.state.log_event(
                        f"❌ Order rejected {resp.status_code} ({ticker}): {detail}"
                    )
                    await self.logger.log("order_error", {
                        "ticker": ticker, "side": side,
                        "status": resp.status_code, "body": detail,
                    })
                    return
                order    = resp.json().get("order", {})
                order_id = order.get("order_id", "?")

            actual_price = await self._fetch_fill_price(order_id, estimated_price)
            if actual_price is None:
                await self._cancel_order(order_id)
                self._attempted_contract = ticker
                await self.state.log_event(f"⚠ Buy order unfilled — cancelled ({ticker}), skipping window")
                await self.logger.log("buy_unfilled", {"ticker": ticker, "order_id": order_id})
                return
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
            self._attempted_contract = ticker
            await self.state.log_event(f"❌ Order failed ({ticker}): {exc}")
            await self.logger.log("order_error", {
                "ticker": ticker, "side": side, "error": str(exc),
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

    async def _fetch_fill_price(self, order_id: str, fallback: float) -> float | None:
        """
        Fetch actual fill price for a market order. Returns:
          - fill price (cents) if order confirmed filled
          - fallback if API calls all fail (network error — assume filled at estimate)
          - None if order is resting/unfilled (caller should cancel and abort)
        """
        if order_id == "?":
            return fallback
        path = f"/trade-api/v2/portfolio/orders/{order_id}"
        url  = f"{self.cfg.kalshi_rest_base}/portfolio/orders/{order_id}"
        headers = _make_rest_headers(self.cfg, "GET", path)
        last_status = None
        for _ in range(6):  # up to 3s total
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.get(url, headers=headers)
                    resp.raise_for_status()
                    order = resp.json().get("order", {})
                last_status = order.get("status")
                avg_price   = order.get("avg_price")
                remaining   = order.get("remaining_count", 0)
                if avg_price is not None and remaining == 0:
                    confirmed = round(float(avg_price) * 100.0, 1)
                    await self.logger.log("fill_confirmed", {
                        "order_id": order_id, "fill_cents": confirmed, "status": last_status,
                    })
                    return confirmed
                if last_status == "resting":
                    # Order sitting in book unfilled — signal caller to cancel
                    return None
            except Exception:
                pass
            await asyncio.sleep(0.5)
        # All retries exhausted without confirming — network issue, use fallback
        await self.logger.log("fill_fetch_failed", {
            "order_id": order_id, "last_status": last_status, "fallback_cents": fallback
        })
        return fallback

    async def _cancel_order(self, order_id: str) -> None:
        """Cancel a resting order on Kalshi."""
        if order_id == "?":
            return
        path = f"/trade-api/v2/portfolio/orders/{order_id}"
        url  = f"{self.cfg.kalshi_rest_base}/portfolio/orders/{order_id}"
        headers = _make_rest_headers(self.cfg, "DELETE", path)
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.delete(url, headers=headers)
            await self.logger.log("order_cancelled", {"order_id": order_id, "status": resp.status_code})
        except Exception as exc:
            await self.logger.log("cancel_error", {"order_id": order_id, "error": str(exc)})
