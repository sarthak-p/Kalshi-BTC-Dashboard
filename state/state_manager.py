from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from fastapi import WebSocket

_STATS_FILE = Path("logs/lifetime_stats.json")


def _load_lifetime_stats() -> tuple[int, int]:
    try:
        data = json.loads(_STATS_FILE.read_text())
        return int(data.get("total_trades", 0)), int(data.get("total_wins", 0))
    except Exception:
        return 0, 0


def _save_lifetime_stats(trades: int, wins: int) -> None:
    try:
        _STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATS_FILE.write_text(json.dumps({"total_trades": trades, "total_wins": wins}))
    except Exception:
        pass


# ── Shared data-model types ──────────────────────────────────────────────────

@dataclass
class Position:
    id: str
    market_ticker: str
    side: str           # "yes" | "no"
    entry_price: float  # cents (0-100) in the position's side
    qty: int            # number of contracts
    entry_time: float   # unix timestamp
    cost_usd: float     # entry_price * qty / 100
    current_price: float = 0.0
    pnl: float = 0.0
    status: str = "open"   # "open" | "closed"
    close_price: float = 0.0
    close_time: float = 0.0
    close_reason: str = ""
    stop_price: float = 0.0  # exit trigger: 30% of entry in position's side cents
    fees_usd: float = 0.0   # accumulated taker fees (entry + exit)
    mode: str = "paper"      # "paper" | "live"
    entry_order_id: str = ""
    entry_client_order_id: str = ""
    close_order_id: str = ""


@dataclass
class Signal:
    id: str
    timestamp: float
    market_ticker: str
    side: str           # "yes" | "no"
    btc_price: float
    kalshi_mid: float
    fair_value: float   # cents
    gap_pct: float      # fraction (0.08 = 8%)
    confidence: float
    yes_ask: float = 0.0   # live YES ask at signal time (cents)
    no_ask: float = 0.0    # live NO ask at signal time (cents)
    acted: bool = False


@dataclass
class Orderbook:
    yes_bids: dict = field(default_factory=dict)  # price(cents) -> qty
    yes_asks: dict = field(default_factory=dict)
    top_yes_bid: Optional[float] = None
    top_yes_ask: Optional[float] = None
    last_seq: int = 0
    last_update: float = 0.0

    def best_bid(self) -> Optional[float]:
        if self.top_yes_bid is not None:
            return self.top_yes_bid
        return max(self.yes_bids) if self.yes_bids else None

    def best_ask(self) -> Optional[float]:
        if self.top_yes_ask is not None:
            return self.top_yes_ask
        return min(self.yes_asks) if self.yes_asks else None

    def mid(self) -> Optional[float]:
        bb, ba = self.best_bid(), self.best_ask()
        if bb is not None and ba is not None:
            return (bb + ba) / 2.0
        return bb if bb is not None else ba


# ── Central state hub ────────────────────────────────────────────────────────

class StateManager:
    def __init__(
        self,
        starting_balance: float = 1000.0,
        trading_mode: str = "paper",
        taker_fee_pct: float = 0.07,
        momentum_threshold_usd: float = 150.0,
    ):
        # Feed state
        self.btc_price: float = 0.0
        self.btc_history: deque[tuple[float, float]] = deque(maxlen=300)
        self.btc_feed_active: bool = False
        self.kalshi_feed_active: bool = False

        # Contract / window
        self.active_contract: Optional[str] = None
        self.window_close_ts: float = 0.0
        self.window_open_ts: float = 0.0
        self.window_discovered_ts: float = 0.0  # wall-clock time when market was discovered
        self.open_interest: float = 0.0         # open_interest_fp from market_discovered
        self.btc_open: float = 0.0  # BTC price at the moment the current window opened

        # Momentum / velocity
        self.momentum_direction: str = "neutral"  # "up" | "down" | "neutral"
        self.velocity_pause: bool = False
        self.velocity_pause_until: float = 0.0

        # Orderbook
        self.orderbook: Orderbook = Orderbook()

        # Trading
        self.trading_mode: str = trading_mode
        self.taker_fee_pct: float = taker_fee_pct
        self.momentum_threshold_usd: float = momentum_threshold_usd
        self.open_positions: list[Position] = []
        self.closed_positions: list[Position] = []
        self.balance: float = starting_balance
        self.session_pnl: float = 0.0
        self.session_fees_usd: float = 0.0
        self.daily_loss: float = 0.0

        # Signals (display list — last 50)
        self.signals: deque[Signal] = deque(maxlen=50)
        # Queue consumed by paper trader; separate from display list
        self.signal_queue: asyncio.Queue[Signal] = asyncio.Queue()

        # Logs
        self.event_log: deque[str] = deque(maxlen=200)

        # Control
        self.kill_switch: bool = False

        # Lifetime stats (persisted across sessions)
        self.lifetime_trades, self.lifetime_wins = _load_lifetime_stats()

        # Session metadata
        self.session_start_ts: float = time.time()
        self.last_settlement_msg: str = ""

        # Internal
        self._lock = asyncio.Lock()
        self._dirty = asyncio.Event()
        self._connections: set[WebSocket] = set()

    # ── WebSocket connection management ──────────────────────────────────────

    def register_ws(self, ws: WebSocket) -> None:
        self._connections.add(ws)

    def unregister_ws(self, ws: WebSocket) -> None:
        self._connections.discard(ws)

    # ── State-update methods (safe to call from any async task) ──────────────

    async def update_btc(self, price: float) -> None:
        async with self._lock:
            ts = time.time()
            self.btc_price = price
            self.btc_history.append((ts, price))
            self.btc_feed_active = True
            # Anchor baseline on the first tick after a new window opens
            if self.btc_open == 0.0 and self.active_contract:
                self.btc_open = price
            self._update_momentum_velocity(ts, price)
        self._dirty.set()

    def _update_momentum_velocity(self, now: float, price: float) -> None:
        """Compute momentum direction and velocity pause state. Called under lock."""
        history = list(self.btc_history)

        # ── Velocity: move > $50 in 10 seconds → pause signals for 30 s ────
        p10 = _nearest_price(history, now - 10.0)
        if p10 is not None and abs(price - p10) > 50.0:
            self.velocity_pause = True
            self.velocity_pause_until = now + 30.0
        elif self.velocity_pause and now >= self.velocity_pause_until:
            self.velocity_pause = False

        # ── Momentum: > $300 move sustained for 20+ seconds ─────────────────
        p30 = _nearest_price(history, now - 30.0)
        p20 = _nearest_price(history, now - 20.0)
        if p30 is not None and p20 is not None:
            delta_30 = price - p30
            delta_20 = price - p20
            # Both windows move the same direction and the 30s move exceeds threshold
            if abs(delta_30) >= self.momentum_threshold_usd and delta_30 * delta_20 > 0:
                self.momentum_direction = "up" if delta_30 > 0 else "down"
            else:
                self.momentum_direction = "neutral"
        else:
            self.momentum_direction = "neutral"

    async def update_orderbook(self, ob: Orderbook) -> None:
        async with self._lock:
            self.orderbook = ob
            self.kalshi_feed_active = True
        self._dirty.set()

    async def set_active_contract(
        self, ticker: str, close_ts: float, open_ts: float, open_interest: float = 0.0
    ) -> None:
        async with self._lock:
            self.active_contract = ticker
            self.window_close_ts = close_ts
            self.window_open_ts = open_ts
            self.window_discovered_ts = time.time()
            self.open_interest = open_interest
            self.btc_open = 0.0  # reset; set by set_btc_open or first update_btc tick
            # If BTC is already live, anchor immediately rather than waiting for next tick
            if self.btc_price > 0:
                self.btc_open = self.btc_price
        self._dirty.set()

    async def set_btc_open(self, price: float) -> None:
        async with self._lock:
            self.btc_open = price
        self._dirty.set()

    async def add_signal(self, sig: Signal) -> None:
        async with self._lock:
            self.signals.appendleft(sig)
        await self.signal_queue.put(sig)
        self._dirty.set()

    async def mark_signal_acted(self, sig_id: str) -> None:
        async with self._lock:
            for s in self.signals:
                if s.id == sig_id:
                    s.acted = True
                    break
        self._dirty.set()

    async def add_position(self, pos: Position) -> None:
        async with self._lock:
            self.open_positions.append(pos)
            self.balance -= pos.cost_usd
        self._dirty.set()

    async def update_open_interest(self, oi: float) -> None:
        async with self._lock:
            self.open_interest = oi
        self._dirty.set()

    async def set_balance(self, balance_usd: float) -> None:
        async with self._lock:
            self.balance = balance_usd
        self._dirty.set()

    async def update_position_price(self, pos_id: str, price: float) -> None:
        async with self._lock:
            for p in self.open_positions:
                if p.id == pos_id:
                    p.current_price = price
                    p.pnl = (price - p.entry_price) * p.qty / 100.0
                    break
        self._dirty.set()

    async def close_position(self, pos: Position) -> None:
        async with self._lock:
            self.open_positions = [p for p in self.open_positions if p.id != pos.id]
            pos.status = "closed"
            self.closed_positions.append(pos)
            proceeds = pos.close_price * pos.qty / 100.0
            pos.pnl = proceeds - pos.cost_usd - pos.fees_usd
            self.balance += proceeds
            self.session_pnl += pos.pnl
            self.session_fees_usd += pos.fees_usd
            self.daily_loss = max(0.0, -self.session_pnl)
            self.lifetime_trades += 1
            if pos.pnl > 0:
                self.lifetime_wins += 1
            _save_lifetime_stats(self.lifetime_trades, self.lifetime_wins)
        self._dirty.set()

    async def log_event(self, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        async with self._lock:
            self.event_log.appendleft(f"[{ts}] {msg}")
        self._dirty.set()

    async def activate_kill_switch(self) -> None:
        async with self._lock:
            self.kill_switch = True
        await self.log_event("KILL SWITCH ACTIVATED — no new positions")

    async def set_last_settlement(self, msg: str) -> None:
        async with self._lock:
            self.last_settlement_msg = msg
        self._dirty.set()

    # ── Serialisation ────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        now = time.time()
        ob = self.orderbook
        return {
            "ts": now,
            # BTC
            "btc_price": self.btc_price,
            "btc_history": list(self.btc_history)[-120:],
            "btc_feed_active": self.btc_feed_active,
            # Contract / window
            "active_contract": self.active_contract,
            "window_close_ts": self.window_close_ts,
            "window_open_ts": self.window_open_ts,
            "window_seconds_left": max(0.0, self.window_close_ts - now),
            "btc_open": self.btc_open,
            "btc_change": round(self.btc_price - self.btc_open, 2) if self.btc_open > 0 else 0.0,
            # Momentum / velocity
            "momentum_direction": self.momentum_direction,
            "velocity_pause": self.velocity_pause,
            # Orderbook
            "orderbook": {
                "best_bid": ob.best_bid(),
                "best_ask": ob.best_ask(),
                "mid": ob.mid(),
                "top_yes_bid": ob.top_yes_bid,
                "top_yes_ask": ob.top_yes_ask,
                "yes_bids": {
                    str(k): v
                    for k, v in sorted(ob.yes_bids.items(), reverse=True)
                },
                "yes_asks": {
                    str(k): v
                    for k, v in sorted(ob.yes_asks.items())
                },
            },
            "kalshi_feed_active": self.kalshi_feed_active,
            # Positions
            "open_positions": [asdict(p) for p in self.open_positions],
            "open_position_count": len(self.open_positions),
            # Portfolio
            "trading_mode": self.trading_mode,
            "balance": round(self.balance, 2),
            "session_pnl": round(self.session_pnl, 2),
            "session_fees_usd": round(self.session_fees_usd, 4),
            "session_net_pnl": round(self.session_pnl, 2),  # pnl already includes fees
            "taker_fee_pct": self.taker_fee_pct,
            "daily_loss": round(self.daily_loss, 2),
            # Signals
            "signals": [asdict(s) for s in list(self.signals)[:20]],
            # Stats
            "win_rate": self._win_rate(),
            "lifetime_trades": self.lifetime_trades,
            "lifetime_wins": self.lifetime_wins,
            "avg_hold_time_s": round(self._avg_hold_time(), 1),
            # Control
            "kill_switch": self.kill_switch,
            # Session
            "session_start_ts": self.session_start_ts,
            "last_settlement_msg": self.last_settlement_msg,
            # Log
            "event_log": list(self.event_log)[:50],
        }

    def _win_rate(self) -> float:
        if self.lifetime_trades == 0:
            return 0.0
        return round(self.lifetime_wins / self.lifetime_trades, 3)

    def _avg_hold_time(self) -> float:
        closed = [p for p in self.closed_positions if p.close_time > 0]
        if not closed:
            return 0.0
        return sum(p.close_time - p.entry_time for p in closed) / len(closed)

    # ── Broadcast loop (run as a dedicated asyncio task) ─────────────────────

    async def broadcast_loop(self) -> None:
        while True:
            await self._dirty.wait()
            self._dirty.clear()
            await self._broadcast_all()

    async def _broadcast_all(self) -> None:
        if not self._connections:
            return
        payload = json.dumps(self.to_dict())
        dead: set[WebSocket] = set()
        for ws in list(self._connections):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.add(ws)
        self._connections -= dead


# ── Module-level helpers ─────────────────────────────────────────────────────

def _nearest_price(history: list, target_ts: float) -> Optional[float]:
    """Return the BTC price whose timestamp is nearest to target_ts, within 5 s."""
    best_price = None
    best_diff = float("inf")
    for ts, price in history:
        diff = abs(ts - target_ts)
        if diff < best_diff:
            best_diff = diff
            best_price = price
    return best_price if best_diff <= 5.0 else None
