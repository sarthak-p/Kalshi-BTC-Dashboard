"""
Trade executor — follows the model recommendation and places paper orders.

Fills are simulated at the current market price (best ask for YES, 100−best_bid for NO).
"""
from __future__ import annotations

from config import Settings
from state.state_manager import StateManager

_UNIT_SIZE_USD = 10.0  # fixed dollars risked per trade


class Executor:
    def __init__(self, state: StateManager, cfg: Settings):
        self.state = state
        self.cfg   = cfg
        self._attempted_contract: str | None = None

    async def startup(self) -> None:
        if self.cfg.paper_bankroll_reset > 0:
            self.state.executor_bankroll          = self.cfg.paper_bankroll_reset
            self.state.executor_bankroll_original = self.cfg.paper_bankroll_reset
            self.state.executor_all_time_trades   = 0
            self.state._save_executor_bankroll()
        await self.state.log_event(
            f"📄 Paper — balance ${self.state.executor_bankroll:.2f}"
        )

    async def maybe_trade(self) -> None:
        contract = self.state.active_contract
        if not contract:
            self._attempted_contract = None
            return

        if contract == self._attempted_contract:
            return
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

        if in_contract and pos["side"] == target_side:
            return
        if in_contract and pos["side"] != target_side:
            await self._paper_close(contract, pos)

        # Skip if GBM has reversed significantly since the lock (intracandle wick guard).
        current_fv = self.state.analysis.get("fv")
        if current_fv is not None:
            if (target_side == "NO" and current_fv > 55.0) or \
               (target_side == "YES" and current_fv < 45.0):
                self._attempted_contract = contract
                await self.state.log_event(
                    f"⏭ Skipped {target_side} — GBM reversed to {current_fv:.0f}¢"
                )
                return

        # Skip if edge has compressed since the lock.
        locked_fv = self.state.final_model_fv
        mid = (ob.best_bid() + ob.best_ask()) / 2.0 if ob.best_bid() and ob.best_ask() else None
        if mid is not None:
            edge = (locked_fv - mid) if target_side == "YES" else (mid - locked_fv)
            if edge < self.cfg.min_gbm_market_gap_cents:
                self._attempted_contract = contract
                await self.state.log_event(
                    f"⏭ Skipped {target_side} — edge {edge:+.1f}¢ (need {self.cfg.min_gbm_market_gap_cents:.0f}¢)"
                )
                return

        n_contracts = max(1, int(_UNIT_SIZE_USD / (price / 100.0)))
        await self._paper_fill(contract, target_side, n_contracts, price)

    async def _paper_fill(self, ticker: str, side: str, contracts: int, fill_price: float) -> None:
        cost = round(contracts * fill_price / 100.0, 2)
        await self.state.open_position(ticker, side, contracts, fill_price, "paper")
        await self.state.log_event(
            f"📄 {side}  {contracts} × {fill_price:.1f}¢  cost ${cost:.2f}  "
            f"balance ${self.state.executor_bankroll:.2f}"
        )

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
            f"🔄 Closed {side}  {pos['contracts']} × {pos['fill_price']:.1f}¢ "
            f"→ {sell_price:.1f}¢  PnL ${pnl:+.2f}  balance ${self.state.executor_bankroll:.2f}"
        )
