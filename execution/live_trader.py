"""
Live Kalshi trader.

Consumes strategy signals and submits real Kalshi orders. Entries and exits use
fill-or-kill limit orders so the bot does not leave resting orders behind while
it is running with a small account.
"""
from __future__ import annotations

import asyncio
import math
import sys
import time
import uuid
from typing import Any, Optional

import httpx

from config import Settings
from execution.kalshi_client import KalshiOrderClient
from logger.event_logger import EventLogger
from risk.risk_manager import RiskManager
from simulation.paper_trader import _simulate_exit_price, _simulate_fill_price
from state.state_manager import Orderbook, Position, Signal, StateManager

BALANCE_SYNC_INTERVAL_S = 15.0


class LiveTrader:
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
        self.client = KalshiOrderClient(cfg)
        self._last_entry_attempt_ts = 0.0
        self._last_exit_attempt_by_pos: dict[str, float] = {}

    async def run(self) -> None:
        await self._startup_check()
        await asyncio.gather(
            self._signal_consumer(),
            self._position_monitor(),
            self._window_expiry_watcher(),
            self._balance_sync_loop(),
        )

    # ── Startup / balance ───────────────────────────────────────────────────

    async def _startup_check(self) -> None:
        try:
            balance_usd = await self._sync_balance_once()
            await self._check_existing_positions()
        except Exception as exc:
            await self.state.activate_kill_switch()
            await self.state.log_event(f"Live trading startup failed: {exc}")
            raise

        await self.state.log_event(
            "LIVE TRADING ARMED — real Kalshi orders enabled; "
            f"budget=${self.cfg.live_max_order_cost_usd:.2f}/signal  balance=${balance_usd:.2f}"
        )
        await self.logger.log("live_trading_armed", {
            "balance_usd": balance_usd,
            "max_order_cost_usd": self.cfg.live_max_order_cost_usd,
        })

    async def _check_existing_positions(self) -> None:
        data = await self.client.get_positions()
        positions = [
            p for p in data.get("market_positions", [])
            if abs(_float_value(p.get("position_fp"))) > 0
        ]
        if not positions:
            return

        tickers = ", ".join(str(p.get("ticker", "?")) for p in positions[:5])
        msg = (
            f"Kalshi account already has {len(positions)} open market "
            f"position(s): {tickers}"
        )
        if self.cfg.live_allow_existing_positions:
            await self.state.log_event(f"LIVE WARNING: {msg}")
            await self.logger.log("live_existing_positions_warning", {
                "count": len(positions),
                "tickers": [p.get("ticker") for p in positions],
            })
            return

        # Hard stop — log, activate kill switch, then exit the process cleanly.
        await self.state.activate_kill_switch()
        await self.state.log_event(f"LIVE STARTUP REFUSED: {msg}")
        await self.logger.log("live_existing_positions_error", {
            "count": len(positions),
            "tickers": [p.get("ticker") for p in positions],
        })
        print(
            f"\n{'=' * 60}\n"
            f"FATAL: {msg}\n"
            f"Set LIVE_ALLOW_EXISTING_POSITIONS=true to override.\n"
            f"{'=' * 60}\n",
            flush=True,
        )
        sys.exit(1)

    async def _balance_sync_loop(self) -> None:
        while True:
            await asyncio.sleep(BALANCE_SYNC_INTERVAL_S)
            try:
                await self._sync_balance_once()
            except Exception as exc:
                await self.state.log_event(f"Live balance sync failed: {exc}")
                await self.logger.log("live_balance_error", {"err": str(exc)})

    async def _sync_balance_once(self) -> float:
        data = await self.client.get_balance()
        balance_cents = int(data.get("balance") or 0)
        balance_usd = balance_cents / 100.0
        await self.state.set_balance(balance_usd)
        return balance_usd

    # ── Signal consumer ──────────────────────────────────────────────────────

    async def _signal_consumer(self) -> None:
        while True:
            sig: Signal = await self.state.signal_queue.get()
            if self.state.kill_switch:
                continue
            if not self.risk.allow_live_position(self.state):
                await self.state.log_event(
                    f"Signal {sig.id} blocked by live risk manager"
                )
                continue
            now = time.time()
            if now - self._last_entry_attempt_ts < self.cfg.live_order_cooldown_s:
                continue
            self._last_entry_attempt_ts = now
            await self._open_position(sig)

    async def _open_position(self, sig: Signal) -> None:
        ob: Orderbook = self.state.orderbook
        limit_price = _entry_limit_price(sig.side, ob)
        if limit_price is None:
            await self.state.log_event(
                f"Signal {sig.id}: no live liquidity to buy {sig.side.upper()}"
            )
            return

        max_cost_cents = int(round(self.cfg.live_max_order_cost_usd * 100))
        # Use the configured unit size, but never exceed the dollar budget.
        budget_count = max_cost_cents // limit_price
        count = max(1, min(self.cfg.live_unit_size, budget_count))
        estimated_cost_cents = limit_price * count

        balance_usd = await self._sync_balance_once()
        if int(balance_usd * 100) < estimated_cost_cents:
            await self.state.log_event(
                f"Signal {sig.id}: insufficient Kalshi cash "
                f"(${balance_usd:.2f}) for {estimated_cost_cents}¢ order"
            )
            return

        client_order_id = f"kb-entry-{sig.id}-{uuid.uuid4().hex[:12]}"
        order_data = _build_order(
            ticker=sig.market_ticker,
            side=sig.side,
            action="buy",
            count=count,
            price_cents=limit_price,
            client_order_id=client_order_id,
            reduce_only=False,
        )

        try:
            order = (await self.client.create_order(order_data)).get("order", {})
        except Exception as exc:
            await self._log_order_error("entry", sig.id, exc)
            return

        await self.state.mark_signal_acted(sig.id)
        fill_count = _filled_count(order)
        if fill_count < 1:
            await self.state.log_event(
                f"Live entry {client_order_id}: not filled "
                f"(status={order.get('status', '?')})"
            )
            await self.logger.log("live_entry_unfilled", {
                "signal_id": sig.id,
                "side": sig.side,
                "limit_price": limit_price,
                "order": _order_log_payload(order),
            })
            return

        fill_price = _filled_price_cents(order, sig.side, limit_price, fill_count)
        cost_usd = fill_price * fill_count / 100.0
        entry_fee = round(cost_usd * self.cfg.kalshi_taker_fee_pct, 6)
        pos = Position(
            id=_position_id(order),
            market_ticker=sig.market_ticker,
            side=sig.side,
            entry_price=fill_price,
            qty=fill_count,
            entry_time=time.time(),
            cost_usd=round(cost_usd, 4),
            current_price=fill_price,
            stop_price=round(fill_price * self.cfg.stop_loss_pct, 1),
            fees_usd=entry_fee,
            mode="live",
            entry_order_id=str(order.get("order_id") or ""),
            entry_client_order_id=client_order_id,
        )
        await self.state.add_position(pos)
        await self.state.log_event(
            f"LIVE OPEN {pos.id}  {sig.side.upper()}  {fill_count}x @ "
            f"{fill_price:.1f}¢  cost=${cost_usd:.2f}  stop={pos.stop_price:.1f}¢"
        )
        await self.logger.log("live_open_position", {
            "pos_id": pos.id,
            "order_id": pos.entry_order_id,
            "client_order_id": client_order_id,
            "side": sig.side,
            "qty": fill_count,
            "fill": fill_price,
            "cost_usd": cost_usd,
            "signal_id": sig.id,
        })

    # ── Position monitor ─────────────────────────────────────────────────────

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
                if pos.status == "closed" or pos.mode != "live":
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

                # Take profit when position has reached the scalp target.
                # YES: trigger on YES mid.
                # NO:  trigger on NO ask = (100 - yes_bid), the marketable price
                #      at which a counterparty will buy the NO contract from us.
                if pos.side == "yes":
                    tp_value = mid
                else:
                    yes_bid = ob.best_bid()
                    tp_value = (100.0 - yes_bid) if yes_bid is not None else pos_value
                if tp_value >= pos.entry_price * (1.0 + self.cfg.take_profit_pct):
                    await self._try_close_position(pos, "take_profit")
                    continue

                if pos.stop_price > 0 and pos_value <= pos.stop_price:
                    await self._try_close_position(pos, "stop_loss")
                    continue

                in_loss = pos_value < pos.entry_price
                if seconds_left <= 120.0 and in_loss:
                    await self._try_close_position(pos, "time_stop")

    async def _try_close_position(self, pos: Position, reason: str) -> None:
        now = time.time()
        last_attempt = self._last_exit_attempt_by_pos.get(pos.id, 0.0)
        if now - last_attempt < self.cfg.live_order_cooldown_s:
            return
        self._last_exit_attempt_by_pos[pos.id] = now

        exit_price = _exit_limit_price(pos.side, self.state.orderbook)
        if exit_price is None:
            await self.state.log_event(
                f"LIVE EXIT {pos.id}: no liquidity to sell {pos.side.upper()}"
            )
            return

        client_order_id = f"kb-exit-{pos.id}-{uuid.uuid4().hex[:12]}"
        order_data = _build_order(
            ticker=pos.market_ticker,
            side=pos.side,
            action="sell",
            count=pos.qty,
            price_cents=exit_price,
            client_order_id=client_order_id,
            reduce_only=True,
        )

        try:
            order = (await self.client.create_order(order_data)).get("order", {})
        except Exception as exc:
            await self._log_order_error("exit", pos.id, exc)
            return

        fill_count = _filled_count(order)
        if fill_count < pos.qty:
            await self.state.log_event(
                f"LIVE EXIT {pos.id}: not filled "
                f"(status={order.get('status', '?')} limit={exit_price}¢)"
            )
            await self.logger.log("live_exit_unfilled", {
                "pos_id": pos.id,
                "limit_price": exit_price,
                "reason": reason,
                "order": _order_log_payload(order),
            })
            return

        close_price = _filled_price_cents(order, pos.side, exit_price, fill_count)
        pos.close_order_id = str(order.get("order_id") or "")
        await self._close_position_at(pos, close_price, reason)
        await self.state.log_event(
            f"LIVE CLOSE {pos.id}  {pos.side.upper()}  @ {close_price:.1f}¢  "
            f"{reason}  PnL=${pos.pnl:+.2f}"
        )

    # ── Window expiry local settlement ───────────────────────────────────────

    async def _window_expiry_watcher(self) -> None:
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

            if last_contract != contract:
                last_contract = contract
                continue

            open_positions = [
                p for p in list(self.state.open_positions)
                if p.mode == "live" and p.status != "closed"
            ]
            if not open_positions:
                last_contract = contract
                continue

            btc_at_close = self.state.btc_price
            btc_open = self.state.btc_open
            resolved_yes = btc_at_close >= btc_open if btc_open > 0 else None
            resolution = "YES" if resolved_yes else "NO" if resolved_yes is not None else "?"
            btc_chg = btc_at_close - btc_open if btc_open > 0 else 0.0
            chg_sign = "+" if btc_chg >= 0 else ""
            settlement_msg = (
                f"{contract}  BTC {btc_at_close:.2f}  "
                f"({chg_sign}{btc_chg:.2f})  -> {resolution}"
            )
            await self.state.log_event(f"Window closed: {settlement_msg}")
            await self.state.set_last_settlement(settlement_msg)

            for pos in open_positions:
                settlement = _settlement_price(pos, resolved_yes)
                await self._close_position_at(pos, settlement, "settlement")
                await self.state.log_event(
                    f"LIVE SETTLED {pos.id}  {pos.side.upper()}  @ "
                    f"{settlement:.0f}¢  est PnL=${pos.pnl:+.2f}"
                )

            last_contract = contract

    async def _close_position_at(
        self,
        pos: Position,
        exit_price: float,
        reason: str,
    ) -> None:
        # If take_profit triggered but the actual fill came back below entry,
        # the market moved against us while the order was in flight — correct the label.
        if reason == "take_profit" and exit_price < pos.entry_price:
            reason = "exit"
        pos.close_price = exit_price
        pos.close_time = time.time()
        pos.close_reason = reason
        exit_fee = round(exit_price * pos.qty / 100.0 * self.cfg.kalshi_taker_fee_pct, 6)
        pos.fees_usd = round(pos.fees_usd + exit_fee, 6)
        await self.state.close_position(pos)
        await self.logger.log("live_close_position", {
            "pos_id": pos.id,
            "side": pos.side,
            "entry": pos.entry_price,
            "close": exit_price,
            "qty": pos.qty,
            "pnl": pos.pnl,
            "reason": reason,
            "close_order_id": pos.close_order_id,
        })

    async def _log_order_error(self, phase: str, ident: str, exc: Exception) -> None:
        detail = str(exc)
        if isinstance(exc, httpx.HTTPStatusError):
            detail = f"{exc.response.status_code}: {exc.response.text[:500]}"
        await self.state.log_event(f"Live {phase} order failed ({ident}): {detail}")
        await self.logger.log("live_order_error", {
            "phase": phase,
            "id": ident,
            "err": detail,
        })


# ── Helpers ──────────────────────────────────────────────────────────────────

def _entry_limit_price(side: str, ob: Orderbook) -> Optional[int]:
    price = _simulate_fill_price(side, ob)
    return _normalize_price(price, for_buy=True)


def _exit_limit_price(side: str, ob: Orderbook) -> Optional[int]:
    price = _simulate_exit_price(side, ob)
    return _normalize_price(price, for_buy=False)


def _normalize_price(price: float | None, for_buy: bool) -> Optional[int]:
    if price is None:
        return None
    rounded = math.ceil(price) if for_buy else math.floor(price)
    if rounded < 1 or rounded > 99:
        return None
    return int(rounded)


def _build_order(
    ticker: str,
    side: str,
    action: str,
    count: int,
    price_cents: int,
    client_order_id: str,
    reduce_only: bool,
    time_in_force: str = "immediate_or_cancel",
) -> dict[str, Any]:
    order: dict[str, Any] = {
        "ticker": ticker,
        "action": action,
        "side": side,
        "count": count,
        "type": "limit",
        "time_in_force": time_in_force,
        "client_order_id": client_order_id,
        "cancel_order_on_pause": True,
        "self_trade_prevention_type": "taker_at_cross",
    }
    if reduce_only:
        order["reduce_only"] = True
    if side == "yes":
        order["yes_price"] = price_cents
    else:
        order["no_price"] = price_cents
    return order


def _filled_count(order: dict[str, Any]) -> int:
    for key in ("fill_count", "filled_count", "fill_count_fp"):
        raw = order.get(key)
        if raw is not None:
            return int(float(raw))
    initial = order.get("initial_count") or order.get("initial_count_fp")
    remaining = order.get("remaining_count") or order.get("remaining_count_fp")
    if initial is not None and remaining is not None:
        return max(0, int(float(initial) - float(remaining)))
    return 0


def _filled_price_cents(
    order: dict[str, Any],
    side: str,
    fallback_price: int,
    fill_count: int,
) -> float:
    if fill_count > 0:
        fill_cost = _float_field(
            order,
            "taker_fill_cost_dollars",
            "maker_fill_cost_dollars",
        )
        if fill_cost is not None and fill_cost > 0:
            return round(fill_cost * 100.0 / fill_count, 2)

    dollars_key = "yes_price_dollars" if side == "yes" else "no_price_dollars"
    dollars_price = _float_field(order, dollars_key)
    if dollars_price is not None and dollars_price > 0:
        return round(dollars_price * 100.0, 2)

    int_key = "yes_price" if side == "yes" else "no_price"
    raw_price = order.get(int_key)
    if raw_price is not None:
        return float(raw_price)

    return float(fallback_price)


def _float_field(order: dict[str, Any], *keys: str) -> Optional[float]:
    for key in keys:
        raw = order.get(key)
        if raw in (None, ""):
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return None


def _float_value(raw: Any) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _position_id(order: dict[str, Any]) -> str:
    raw = str(order.get("order_id") or uuid.uuid4().hex)
    return raw.replace("-", "")[:8]


def _settlement_price(pos: Position, resolved_yes: Optional[bool]) -> float:
    if resolved_yes is None:
        return pos.current_price
    if pos.side == "yes":
        return 100.0 if resolved_yes else 0.0
    return 0.0 if resolved_yes else 100.0


def _order_log_payload(order: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "order_id",
        "client_order_id",
        "ticker",
        "side",
        "action",
        "status",
        "fill_count",
        "fill_count_fp",
        "remaining_count",
        "remaining_count_fp",
    )
    return {k: order.get(k) for k in keys if k in order}
