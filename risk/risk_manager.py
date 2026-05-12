"""
Risk manager.

Stateless checks called synchronously on every potential position open.
Monitors daily loss and flips the kill switch when the limit is hit.
"""
from __future__ import annotations

import asyncio

from config import Settings
from logger.event_logger import EventLogger
from state.state_manager import StateManager


class RiskManager:
    def __init__(self, cfg: Settings, logger: EventLogger):
        self.cfg = cfg
        self.logger = logger

    # ── Called by paper trader before opening a position ─────────────────────

    def allow_new_position(self, state: StateManager) -> bool:
        if state.kill_switch:
            return False
        if len(state.open_positions) >= self.cfg.max_concurrent_positions:
            return False
        if state.daily_loss >= self.cfg.daily_loss_limit_usd:
            return False
        if state.balance < self.cfg.max_position_size_usd:
            return False
        return True

    def allow_live_position(self, state: StateManager) -> bool:
        if state.kill_switch:
            return False
        if len(state.open_positions) >= self.cfg.max_concurrent_positions:
            return False
        if state.daily_loss >= self.cfg.daily_loss_limit_usd:
            return False
        return True

    # ── Background monitor — run as an asyncio task ───────────────────────────

    async def run(self, state: StateManager) -> None:
        while True:
            await asyncio.sleep(1.0)
            if state.kill_switch:
                continue
            await self._check_daily_loss(state)

    async def _check_daily_loss(self, state: StateManager) -> None:
        if state.daily_loss >= self.cfg.daily_loss_limit_usd:
            await state.activate_kill_switch()
            await self.logger.log("risk_event", {
                "event": "daily_loss_limit_hit",
                "daily_loss": state.daily_loss,
                "limit": self.cfg.daily_loss_limit_usd,
            })
