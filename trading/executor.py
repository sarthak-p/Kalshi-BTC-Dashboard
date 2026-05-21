"""
Trade executor — follows the model recommendation and places paper orders.

Fills are simulated at the current market price (best ask for YES, 100−best_bid for NO).
"""
from __future__ import annotations

from config import Settings
from state.state_manager import StateManager

_BASE_SIZE_USD = 150.0
_MAX_SIZE_USD  = 200.0
_MIN_SIZE_USD  = 500.0


def _calc_size_usd(gap_cents: float, signal_count: int) -> float:
    """Scale position size by GBM-market gap and confirming signal breadth.

    gap_cents=20, signal_count=5 → $100 (base).
    Wide gap + many confirmers → up to $200; narrow gap + few → floor at $50.
    """
    gap_factor    = gap_cents / 20.0
    signal_factor = max(0.5, min(1.0, signal_count / 5.0))
    raw = _BASE_SIZE_USD * gap_factor * signal_factor
    return max(_MIN_SIZE_USD, min(_MAX_SIZE_USD, round(raw, 2)))


class Executor:
    def __init__(self, state: StateManager, cfg: Settings):
        self.state = state
        self.cfg   = cfg
        self._attempted_contract: str | None = None

    async def startup(self) -> None:
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
        size_usd     = _calc_size_usd(gap, signal_count)
        n_contracts  = max(1, int(size_usd / (price / 100.0)))
        await self._paper_fill(contract, target_side, n_contracts, price, size_usd, gap, signal_count)

    async def _paper_fill(
        self, ticker: str, side: str, contracts: int, fill_price: float,
        size_usd: float = _BASE_SIZE_USD, gap: float = 0.0, signal_count: int = 0,
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
