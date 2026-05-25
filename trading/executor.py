"""
Trade executor — follows the model recommendation and places paper orders.

Fills are simulated at the current market price (best ask for YES, 100−best_bid for NO),
subject to a 75¢ ceiling (_MAX_ENTRY_PRICE). If the ask exceeds the ceiling the trade
is skipped that tick and retried on the next, simulating a resting limit order.
"""
from __future__ import annotations
import asyncio

from config import Settings
from state.state_manager import StateManager

_MAX_ENTRY_PRICE: float = 75.0

class Executor:
    # Position sizing — override these in subclasses for different modes.
    _BASE_SIZE_USD: float = 150.0
    _MAX_SIZE_USD:  float = 200.0
    _MIN_SIZE_USD:  float = 100.0

    def __init__(self, state: StateManager, cfg: Settings):
        self.state = state
        self.cfg   = cfg
        self._attempted_contract: str | None = None

    def _calc_size_usd(self, gap_cents: float, signal_count: int) -> float:
        gap_factor    = gap_cents / 20.0
        signal_factor = max(0.5, min(1.0, signal_count / 5.0))
        raw = self._BASE_SIZE_USD * gap_factor * signal_factor
        return max(self._MIN_SIZE_USD, min(self._MAX_SIZE_USD, round(raw, 2)))

    async def startup(self) -> None:
        await self.state.log_event(
            f"📄 Paper — balance ${self.state.executor_bankroll:.2f}"
        )

    async def maybe_stop_loss(self) -> None:
        pos = self.state.position
        if pos["status"] != "open":
            return

        contract = pos["ticker"]
        if contract != self.state.active_contract:
            return

        side = pos["side"]
        ob   = self.state.orderbook

        if side == "YES":
            current_value = ob.best_bid()
        else:
            yes_ask = ob.best_ask()
            current_value = (100.0 - yes_ask) if yes_ask is not None else None

        if current_value is None or current_value > 20.0:
            return

        await self.state.log_event(
            f"🛑 Stop-loss: {side} dropped to {current_value:.1f}¢  "
            f"— closing to limit loss"
        )
        await self._paper_close(contract, pos)
        self._attempted_contract = contract  # prevent re-entry this window

    async def _prepare_trade(self) -> dict | None:
        """Run all entry guards. Returns entry params if ready to trade, None to skip."""
        contract = self.state.active_contract
        if not contract:
            self._attempted_contract = None
            return None

        if contract == self._attempted_contract:
            return None
        if not self.state.final_model_locked:
            return None
        if self.state.final_model_contract != contract:
            return None

        target_side = self.state.final_model_side
        if not target_side:
            return None

        ob = self.state.orderbook
        if target_side == "YES":
            price = ob.best_ask()
            bid = ob.best_bid()
            if price is not None and bid is not None and price <= bid:
                price = None
        else:
            yes_bid = ob.best_bid()
            price = (100.0 - yes_bid) if yes_bid is not None else None

        if not price:
            return None

        pos = self.state.position
        in_contract = pos["status"] == "open" and pos["ticker"] == contract

        if in_contract and pos["side"] == target_side:
            return None
        if in_contract and pos["side"] != target_side:
            await self._paper_close(contract, pos)

        current_fv = self.state.analysis.get("fv")
        if current_fv is not None:
            if (target_side == "NO" and current_fv > 55.0) or \
               (target_side == "YES" and current_fv < 45.0):
                self._attempted_contract = contract
                await self.state.log_event(
                    f"⏭ Skipped {target_side} — GBM reversed to {current_fv:.0f}¢"
                )
                return None

        current_slope = self.state.analysis.get("slope")
        if current_slope is not None:
            slope_opposes = (
                (target_side == "YES" and current_slope < -0.10) or
                (target_side == "NO"  and current_slope >  0.10)
            )
            if slope_opposes:
                self._attempted_contract = contract
                await self.state.log_event(
                    f"⏭ Skipped {target_side} — slope opposing at execution: {current_slope:+.3f}/s"
                )
                return None

        gap          = self.state.final_model_gap
        signal_count = self.state.recommendation.get("signal_count", 0)
        size_usd     = self._calc_size_usd(gap, signal_count)

        return {
            "contract":     contract,
            "side":         target_side,
            "price":        price,
            "gap":          gap,
            "signal_count": signal_count,
            "size_usd":     size_usd,
        }

    async def maybe_trade(self) -> None:
        entry = await self._prepare_trade()
        if entry is None:
            return

        price = entry["price"]
        if price > _MAX_ENTRY_PRICE:
            return  # retry next tick — simulates resting limit order

        n_contracts = max(1, int(entry["size_usd"] / (price / 100.0)))
        await self._paper_fill(
            entry["contract"], entry["side"], n_contracts, price,
            entry["size_usd"], entry["gap"], entry["signal_count"],
        )
        self._attempted_contract = entry["contract"]

    async def _paper_fill(
        self, ticker: str, side: str, contracts: int, fill_price: float,
        size_usd: float = 0.0, gap: float = 0.0, signal_count: int = 0,
    ) -> None:
        cost = round(contracts * fill_price / 100.0, 2)
        await self.state.open_position(ticker, side, contracts, fill_price, "paper")
        await self.state.log_event(
            f"📄 {side}  {contracts} × {fill_price:.1f}¢  cost ${cost:.2f}  "
            f"[size ${size_usd:.0f}  gap {gap:+.1f}¢  sigs {signal_count}]  "
            f"balance ${self.state.executor_bankroll:.2f}"
        )

    async def _paper_close(self, ticker: str, pos: dict) -> None:
        side = pos["side"]
        yes_price = 1 if side == "YES" else 99  # sell at any available bid

        sell_confirmed = False
        for attempt in range(1, 5):
            order_id = await self._place_order(
                "sell", ticker, side, pos["contracts"], yes_price,
                reduce_only=True, time_in_force="immediate_or_cancel",
            )
            if order_id is None:
                await self.state.log_event(f"❌ Live sell failed (attempt {attempt}/4)")
                if attempt < 4:
                    await asyncio.sleep(1.0)
                continue

            await asyncio.sleep(0.2)  # let IoC settle
            filled = await self._check_order_filled(order_id)
            if filled:
                sell_confirmed = True
                break
            if attempt < 4:
                await self.state.log_event(f"⚠ Sell retry {attempt}/4")
                await asyncio.sleep(1.0)

        if not sell_confirmed:
            await self.state.log_event("❌ Live sell failed after 4 attempts — position may still be open")
            return

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
