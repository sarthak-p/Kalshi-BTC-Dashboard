"""
Trade executor — follows the model recommendation and places paper orders.

Fills are simulated at the current market price (best ask for YES, 100−best_bid for NO).
"""
from __future__ import annotations

import time

from config import Settings
from state.state_manager import StateManager

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

        # Only exit when there's meaningful time left — inside 2 min the market
        # has largely priced the outcome and last-minute BTC spikes are common.
        tau = max(0.0, self.state.window_close_ts - time.time())
        if tau <= 120.0:
            return

        side = pos["side"]
        ob   = self.state.orderbook

        if side == "YES":
            current_value = ob.best_bid()
        else:
            yes_ask = ob.best_ask()
            current_value = (100.0 - yes_ask) if yes_ask is not None else None

        if current_value is None or current_value > 35.0:
            return

        await self.state.log_event(
            f"🛑 Stop-loss: {side} dropped to {current_value:.0f}¢  "
            f"({tau/60:.1f} min left) — closing to limit loss"
        )
        await self._paper_close(contract, pos)
        self._attempted_contract = contract  # prevent re-entry this window

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
            # Stale ask: market makers withdrew — ask below bid means no real liquidity
            bid = ob.best_bid()
            if price is not None and bid is not None and price <= bid:
                price = None
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

        # Skip if slope is now strongly opposing the locked direction.
        # Threshold 0.10 $/s — clear directional signal, not noise.
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
                return

        gap          = self.state.final_model_gap
        signal_count = self.state.recommendation.get("signal_count", 0)
        size_usd     = self._calc_size_usd(gap, signal_count)
        n_contracts  = max(1, int(size_usd / (price / 100.0)))
        await self._paper_fill(contract, target_side, n_contracts, price, size_usd, gap, signal_count)

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
