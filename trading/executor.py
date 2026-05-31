"""
Trade executor — follows the model recommendation and places paper orders.

Fills are simulated at the current market price (best ask for YES, 100−best_bid for NO).
Position sizing is flat: cfg.trade_size_usd per trade.
"""
from __future__ import annotations
import asyncio

from config import Settings
from logger.event_logger import EventLogger
from state.state_manager import StateManager


class Executor:
    def __init__(self, state: StateManager, cfg: Settings, logger: EventLogger | None = None):
        self.state  = state
        self.cfg    = cfg
        self.logger = logger
        self._attempted_contract: str | None = None

    async def startup(self) -> None:
        await self.state.log_event(
            f"📄 Paper — balance ${self.state.executor_bankroll:.2f}"
        )

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


        n_contracts = max(1, int(self.cfg.trade_size_usd / (price / 100.0)))
        return {
            "contract":    contract,
            "side":        target_side,
            "price":       price,
            "gap":         self.state.final_model_gap,
            "n_contracts": n_contracts,
        }

    async def maybe_trade(self) -> None:
        pos      = self.state.position
        contract = self.state.active_contract
        in_pos   = pos["status"] == "open" and pos["ticker"] == contract



        entry = await self._prepare_trade()
        if entry is None:
            return

        await self._paper_fill(entry["contract"], entry["side"], entry["n_contracts"], entry["price"])
        self._attempted_contract = entry["contract"]

    async def _paper_fill(self, ticker: str, side: str, contracts: int, fill_price: float) -> None:
        cost = round(contracts * fill_price / 100.0, 2)
        await self.state.open_position(ticker, side, contracts, fill_price, "paper")
        await self.state.log_event(
            f"📄 {side}  {contracts} × {fill_price:.1f}¢  cost ${cost:.2f}  "
            f"balance ${self.state.executor_bankroll:.2f}"
        )

    async def _paper_close(self, ticker: str, pos: dict) -> None:
        side = pos["side"]
        ob = self.state.orderbook
        if side == "YES":
            sell_price = ob.best_bid() or pos["fill_price"]
        else:
            yes_ask = ob.best_ask()
            sell_price = (100.0 - yes_ask) if yes_ask is not None else pos["fill_price"]

        await self.state.stop_position(ticker, sell_price)
        pnl = self.state.position["pnl"]
        await self.state.log_event(
            f"Closed {side} @ {sell_price:.1f}¢  PnL ${pnl:+.2f}  "
            f"balance ${self.state.executor_bankroll:.2f}"
        )
