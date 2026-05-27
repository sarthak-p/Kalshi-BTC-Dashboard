"""
Trade executor — follows the model recommendation and places paper orders.

Fills are simulated at the current market price (best ask for YES, 100−best_bid for NO),
subject to an 80¢ ceiling (_MAX_ENTRY_PRICE). If the ask exceeds the ceiling the trade
is skipped that tick and retried on the next, simulating a resting limit order.

Position sizing uses half-Kelly: f* = 0.5 × (p_true − p_market) / (1 − p_market),
where p_true is the historically measured model accuracy (80%), not GBM fair value.
Capped at _MAX_BANKROLL_FRACTION per trade. Trades with no positive edge are skipped.
"""
from __future__ import annotations
import asyncio

from config import Settings
from state.state_manager import StateManager

_MAX_ENTRY_PRICE: float = 80.0
_KELLY_FRACTION: float = 0.5          # half-Kelly multiplier
_MAX_BANKROLL_FRACTION: float = 0.20  # hard cap: never risk more than 20% per trade
_MODEL_ACCURACY_FALLBACK: float = 0.80  # used only if no resolved locks exist yet

class Executor:
    def __init__(self, state: StateManager, cfg: Settings):
        self.state = state
        self.cfg   = cfg
        self._attempted_contract: str | None = None
        self._kelly_skip_logged:  str | None = None

    def _get_p_market(self, side: str, taker_price: float, ob) -> float:
        return taker_price / 100.0

    def _calc_kelly_size(self, p_true: float, p_market: float) -> tuple[float, float]:
        """Return (size_usd, kelly_pct) using half-Kelly.
        Returns (0.0, 0.0) when there is no positive edge.
        """
        if p_market >= 1.0 or p_true <= p_market:
            return 0.0, 0.0
        kelly = (p_true - p_market) / (1.0 - p_market)
        fraction = min(kelly * _KELLY_FRACTION, _MAX_BANKROLL_FRACTION)
        size = round(fraction * self.state.executor_bankroll, 2)
        return size, round(fraction * 100.0, 1)

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

        p_true = self.state._res_pred_accuracy(lifetime=True) or _MODEL_ACCURACY_FALLBACK
        p_market = self._get_p_market(target_side, price, ob)
        fv       = self.state.final_model_fv
        size_usd, kelly_pct = self._calc_kelly_size(p_true, p_market)

        if size_usd <= 0:
            if self._kelly_skip_logged != contract:
                self._kelly_skip_logged = contract
                await self.state.log_event(
                    f"⏭ No Kelly edge {target_side} (fv {fv:.0f}¢, market {price:.0f}¢) — retrying"
                )
            return None  # don't lock out window — retry each tick in case market pulls back

        return {
            "contract":  contract,
            "side":      target_side,
            "price":     price,
            "gap":       self.state.final_model_gap,
            "kelly_pct": kelly_pct,
            "size_usd":  size_usd,
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
            entry["size_usd"], entry["gap"], entry["kelly_pct"],
        )
        self._attempted_contract = entry["contract"]

    async def _paper_fill(
        self, ticker: str, side: str, contracts: int, fill_price: float,
        size_usd: float = 0.0, gap: float = 0.0, kelly_pct: float = 0.0,
    ) -> None:
        cost = round(contracts * fill_price / 100.0, 2)
        await self.state.open_position(ticker, side, contracts, fill_price, "paper")
        await self.state.log_event(
            f"📄 {side}  {contracts} × {fill_price:.1f}¢  cost ${cost:.2f}  "
            f"[{kelly_pct:.1f}% Kelly = ${size_usd:.0f}  gap {gap:+.1f}¢]  "
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
            f"balance ${self.state.executor_bankroll:.2f}"
        )
