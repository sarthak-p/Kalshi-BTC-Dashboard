"""
Paper trader.

Consumes signals from state.signal_queue, checks risk, opens simulated
positions filled at live Kalshi orderbook prices, and monitors them until
either the window closes (auto-settle), a stop loss fires, or a time stop fires.

Contract economics (Kalshi binary):
  • 1 contract costs  entry_price / 100  USD
  • 1 contract pays   $1.00  if it resolves in your favour, $0.00 otherwise
  • Early close PnL = (close_price − entry_price) * qty / 100  USD
  • Settlement:
      YES resolves → YES contracts settle at 100, NO contracts settle at 0
      NO  resolves → YES contracts settle at 0,  NO contracts settle at 100

Stop loss rules:
  • Position-level (30%): exit at market when position value ≤ 30% of entry
  • Time stop (2 min): if window has ≤ 120 s left AND position is in loss, exit
    at market (profitable positions ride to settlement)

Market exits use the live best bid for the position's side:
  • Selling YES → receive best YES bid
  • Selling NO  → receive best NO bid (= 100 − best YES ask)
"""
from __future__ import annotations

import asyncio
import time
import uuid
from typing import Optional

from config import Settings
from logger.event_logger import EventLogger
from risk.risk_manager import RiskManager
from state.state_manager import Orderbook, Position, Signal, StateManager


class PaperTrader:
    def __init__(
        self,
        state: StateManager,
        cfg: Settings,
        logger: EventLogger,
        risk: RiskManager,
    ):
        self.state = state
        self.cfg = cfg
        self.logger = logger
        self.risk = risk

    async def run(self) -> None:
        await asyncio.gather(
            self._signal_consumer(),
            self._position_monitor(),
            self._window_expiry_watcher(),
        )

    # ── Signal consumer ──────────────────────────────────────────────────────

    async def _signal_consumer(self) -> None:
        while True:
            sig: Signal = await self.state.signal_queue.get()
            if self.state.kill_switch:
                continue
            if not self.risk.allow_new_position(self.state):
                await self.state.log_event(
                    f"Signal {sig.id} blocked by risk manager"
                )
                continue
            await self._open_position(sig)

    async def _open_position(self, sig: Signal) -> None:
        ob: Orderbook = self.state.orderbook
        fill_price = _simulate_fill_price(sig.side, ob)
        if fill_price is None:
            await self.state.log_event(
                f"Signal {sig.id}: no liquidity to fill ({sig.side})"
            )
            return

        qty = _calc_qty(fill_price, self.cfg.max_position_size_usd)
        cost_usd = fill_price * qty / 100.0

        if cost_usd < 0.01:
            return

        entry_fee = round(cost_usd * self.cfg.kalshi_taker_fee_pct, 6)
        pos = Position(
            id=uuid.uuid4().hex[:8],
            market_ticker=sig.market_ticker,
            side=sig.side,
            entry_price=fill_price,
            qty=qty,
            entry_time=time.time(),
            cost_usd=round(cost_usd, 4),
            current_price=fill_price,
            stop_price=round(fill_price * self.cfg.stop_loss_pct, 1),
            fees_usd=entry_fee,
        )
        await self.state.add_position(pos)
        await self.state.mark_signal_acted(sig.id)
        await self.state.log_event(
            f"Opened {pos.id}  {sig.side.upper()}  {qty}× @ {fill_price}¢  "
            f"cost=${cost_usd:.2f}  stop={pos.stop_price:.1f}¢"
        )
        await self.logger.log("open_position", {
            "pos_id": pos.id,
            "side": sig.side,
            "qty": qty,
            "fill": fill_price,
            "cost_usd": cost_usd,
            "stop_price": pos.stop_price,
            "signal_id": sig.id,
        })

    # ── Position monitor ──────────────────────────────────────────────────────

    async def _position_monitor(self) -> None:
        while True:
            await asyncio.sleep(0.5)
            ob = self.state.orderbook
            mid = ob.mid()
            if mid is None:
                continue

            now = time.time()
            seconds_left = max(0.0, self.state.window_close_ts - now)

            for pos in list(self.state.open_positions):
                if pos.status == "closed":
                    continue

                # YES: mid-based value for P&L tracking and stop/time-stop checks.
                # NO:  use NO bid (100 - yes_ask) — the actual sell price —
                #      so stop and time-stop trigger on the conservative/worst-case
                #      price rather than the optimistic mid.
                yes_ask = ob.best_ask()
                if pos.side == "yes":
                    pos_value = mid
                else:
                    pos_value = (100.0 - yes_ask) if yes_ask is not None else (100.0 - mid)
                await self.state.update_position_price(pos.id, pos_value)

                # ── Take profit when position hits the scalp target ────────────
                # YES: trigger on YES mid.
                # NO:  trigger on NO ask = (100 - yes_bid), the marketable price
                #      at which a counterparty will buy the NO contract from us.
                if pos.side == "yes":
                    tp_value = mid
                else:
                    yes_bid = ob.best_bid()
                    tp_value = (100.0 - yes_bid) if yes_bid is not None else pos_value
                if tp_value >= pos.entry_price * (1.0 + self.cfg.take_profit_pct):
                    exit_price = _simulate_exit_price(pos.side, ob) or mid
                    await self.state.log_event(
                        f"TAKE PROFIT {pos.id}  {pos.side.upper()}  "
                        f"entry={pos.entry_price:.1f}¢  exit={exit_price:.1f}¢  "
                        f"PnL=${pos.pnl:+.2f}"
                    )
                    await self._close_position_at(pos, exit_price, "take_profit")
                    continue

                # ── Position-level stop loss ──────────────────────────────────
                if pos.stop_price > 0 and pos_value <= pos.stop_price:
                    exit_price = _simulate_exit_price(pos.side, ob) or mid
                    await self.state.log_event(
                        f"STOP LOSS {pos.id}  {pos.side.upper()}  "
                        f"entry={pos.entry_price:.1f}¢  stop={pos.stop_price:.1f}¢  "
                        f"exit={exit_price:.1f}¢"
                    )
                    await self._close_position_at(pos, exit_price, "stop_loss")
                    continue

                # ── Time stop: ≤ 2 min left and position is in loss ──────────
                in_loss = pos_value < pos.entry_price
                if seconds_left <= 120.0 and in_loss:
                    exit_price = _simulate_exit_price(pos.side, ob) or mid
                    await self.state.log_event(
                        f"TIME STOP {pos.id}  {pos.side.upper()}  "
                        f"≤2 min left in loss  exit={exit_price:.1f}¢  "
                        f"PnL=${pos.pnl:+.2f}"
                    )
                    await self._close_position_at(pos, exit_price, "time_stop")

    # ── Window expiry auto-settle ─────────────────────────────────────────────

    async def _window_expiry_watcher(self) -> None:
        """Check every second; when the window closes, settle all open positions."""
        last_contract: Optional[str] = None
        while True:
            await asyncio.sleep(1.0)
            contract = self.state.active_contract
            close_ts = self.state.window_close_ts
            if not contract or close_ts <= 0:
                continue

            now = time.time()
            if now < close_ts:
                last_contract = contract
                continue

            # Window just closed
            if last_contract != contract:
                last_contract = contract
                continue

            open_positions = list(self.state.open_positions)
            if not open_positions:
                last_contract = contract
                continue

            # KXBTC15M resolves YES if BTC at close >= BTC at window open
            btc_at_close = self.state.btc_price
            btc_open = self.state.btc_open
            resolved_yes = btc_at_close >= btc_open if btc_open > 0 else None

            resolution = "YES" if resolved_yes else "NO" if resolved_yes is not None else "?"
            btc_chg = btc_at_close - btc_open if btc_open > 0 else 0.0
            chg_sign = "+" if btc_chg >= 0 else ""
            settlement_msg = (
                f"{contract}  BTC {btc_at_close:.2f}  "
                f"({chg_sign}{btc_chg:.2f})  → {resolution}"
            )
            await self.state.log_event(f"Window closed: {settlement_msg}")
            await self.state.set_last_settlement(settlement_msg)

            for pos in open_positions:
                if pos.status == "closed":
                    continue
                await self._settle_position(pos, resolved_yes)

            last_contract = contract

    async def _settle_position(
        self, pos: Position, resolved_yes: Optional[bool]
    ) -> None:
        if resolved_yes is None:
            settlement = pos.current_price
        else:
            if pos.side == "yes":
                settlement = 100.0 if resolved_yes else 0.0
            else:
                settlement = 0.0 if resolved_yes else 100.0
        await self._close_position_at(pos, settlement, "settlement")
        await self.state.log_event(
            f"Settled {pos.id}  {pos.side.upper()}  @ {settlement}¢  "
            f"PnL=${pos.pnl:+.2f}"
        )

    # ── Shared close helper ───────────────────────────────────────────────────

    async def _close_position_at(
        self, pos: Position, exit_price: float, reason: str
    ) -> None:
        # If take_profit triggered but the fill came back below entry, the trigger
        # fired on a momentary price spike that didn't hold — correct the label.
        if reason == "take_profit" and exit_price < pos.entry_price:
            reason = "exit"
        pos.close_price = exit_price
        pos.close_time = time.time()
        pos.close_reason = reason
        exit_fee = round(exit_price * pos.qty / 100.0 * self.cfg.kalshi_taker_fee_pct, 6)
        pos.fees_usd = round(pos.fees_usd + exit_fee, 6)
        await self.state.close_position(pos)
        await self.logger.log("close_position", {
            "pos_id": pos.id,
            "side": pos.side,
            "entry": pos.entry_price,
            "close": exit_price,
            "qty": pos.qty,
            "pnl": pos.pnl,
            "reason": reason,
        })


# ── Helpers ──────────────────────────────────────────────────────────────────

def _simulate_fill_price(side: str, ob: Orderbook) -> Optional[float]:
    """Market buy: pay the best ask for that side."""
    if side == "yes":
        return ob.best_ask()
    else:
        bb = ob.best_bid()
        return (100.0 - bb) if bb is not None else None


def _simulate_exit_price(side: str, ob: Orderbook) -> Optional[float]:
    """Market sell: receive the best bid for that side."""
    if side == "yes":
        return ob.best_bid()
    else:
        ba = ob.best_ask()
        return (100.0 - ba) if ba is not None else None


def _calc_qty(fill_price: float, max_usd: float) -> int:
    if fill_price <= 0:
        return 0
    cost_per_contract = fill_price / 100.0
    qty = int(max_usd / cost_per_contract)
    return max(1, min(qty, 200))
