"""
Async event logger.

Hot path: append to an in-memory deque (no I/O).
Background flush task: write buffered rows to CSV every 5 seconds.
"""
from __future__ import annotations

import asyncio
import csv
import json
import os
import time
from collections import deque
from pathlib import Path
from typing import Any

LOG_DIR = Path("logs")
FLUSH_INTERVAL = 5.0        # seconds
BUFFER_MAX = 10_000         # drop oldest if buffer overflows


class EventLogger:
    def __init__(self) -> None:
        self._buffer: deque[dict] = deque(maxlen=BUFFER_MAX)
        self._lock = asyncio.Lock()
        self._csv_path: Path | None = None
        self._fieldnames: list[str] = ["ts", "event", "data"]

    _PRED_FIELDS: list[str] = [
        "session_ts", "date_utc", "ticker", "floor_strike",
        "btc_open", "btc_close", "btc_change", "resolution",
        "predicted_direction", "prediction_yes_pct", "pre_window_bias",
        "prediction_correct",
    ]

    # ── Public API ───────────────────────────────────────────────────────────

    async def log(self, event: str, data: dict[str, Any] | None = None) -> None:
        row = {
            "ts": time.time(),
            "event": event,
            "data": json.dumps(data or {}),
        }
        async with self._lock:
            self._buffer.append(row)

    def log_prediction(self, data: dict[str, Any]) -> None:
        """Append one row to the persistent cross-session predictions log (synchronous)."""
        LOG_DIR.mkdir(exist_ok=True)
        pred_csv = LOG_DIR / "predictions.csv"
        write_header = not pred_csv.exists()
        with open(pred_csv, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=self._PRED_FIELDS, extrasaction="ignore")
            if write_header:
                writer.writeheader()
            writer.writerow(data)

    # ── Background flush loop (run as an asyncio task) ───────────────────────

    async def flush_loop(self) -> None:
        LOG_DIR.mkdir(exist_ok=True)
        self._csv_path = LOG_DIR / f"session_{int(time.time())}.csv"
        # Write header
        async with self._lock:
            rows_snapshot: list[dict] = []  # first flush is empty
        self._write_rows(rows_snapshot, write_header=True)

        while True:
            await asyncio.sleep(FLUSH_INTERVAL)
            await self._flush()

    async def _flush(self) -> None:
        async with self._lock:
            if not self._buffer:
                return
            rows = list(self._buffer)
            self._buffer.clear()
        self._write_rows(rows)

    def _write_rows(self, rows: list[dict], write_header: bool = False) -> None:
        if self._csv_path is None:
            return
        mode = "a"
        with open(self._csv_path, mode, newline="") as f:
            writer = csv.DictWriter(f, fieldnames=self._fieldnames)
            if write_header:
                writer.writeheader()
            writer.writerows(rows)
