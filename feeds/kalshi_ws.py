"""
Kalshi WebSocket feed.

Responsibilities:
  1. Auto-discover the active BTC 15-min contract via the REST API at startup
     and whenever the current window expires.
  2. Maintain an in-memory limit-order book (yes bids/asks) from orderbook_delta
     and orderbook_snapshot messages.
  3. Reconnect automatically with exponential backoff on any error.

Auth: RSA-PSS SHA-256.  Private key is base64-encoded PEM in KALSHI_PRIVATE_KEY_B64.
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from copy import deepcopy
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import httpx
import websockets
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

from config import Settings
from logger.event_logger import EventLogger
from state.state_manager import Orderbook, StateManager

RECONNECT_BASE = 1.0
RECONNECT_MAX = 5.0
WINDOW_SECONDS = 900  # 15 min
ROLLOVER_RETRY_S = 5.0   # poll interval while waiting for the next window
ROLLOVER_LOG_S = 30.0    # how often to log the "waiting" message


class _WindowGap(Exception):
    """Raised when no active contract exists yet — normal during rollover."""


class KalshiFeed:
    def __init__(self, state: StateManager, cfg: Settings, logger: EventLogger):
        self.state = state
        self.cfg = cfg
        self.logger = logger
        self._ob: Orderbook = Orderbook()
        self._cmd_id: int = 0
        self._last_gap_log_ts: float = 0.0

    # ── Public entry point ───────────────────────────────────────────────────

    async def run(self) -> None:
        delay = RECONNECT_BASE
        while True:
            try:
                await self._connect_and_run()
                delay = RECONNECT_BASE
            except _WindowGap:
                now = time.time()
                if now - self._last_gap_log_ts >= ROLLOVER_LOG_S:
                    await self.state.log_event("Waiting for next window to open...")
                    self._last_gap_log_ts = now
                await asyncio.sleep(ROLLOVER_RETRY_S)
            except Exception as exc:
                await self.state.log_event(f"Kalshi WS error: {exc}")
                await self.logger.log("kalshi_error", {"err": str(exc)})
                await asyncio.sleep(delay)
                delay = min(delay * 2.0, RECONNECT_MAX)

    # ── Connection lifecycle ─────────────────────────────────────────────────

    async def _connect_and_run(self) -> None:
        await self._discover_contract()

        # Kalshi rejects the WebSocket upgrade with HTTP 401 without these headers.
        # The same RSA-PSS signature used for REST must accompany the HTTP upgrade.
        upgrade_headers = self._ws_upgrade_headers()

        async with websockets.connect(
            self.cfg.kalshi_ws_base,
            additional_headers=upgrade_headers,
            ping_interval=20,
            ping_timeout=10,
            open_timeout=15,
        ) as ws:
            await self.state.log_event(f"Kalshi WS connected ({self.cfg.kalshi_env})")
            await self._login(ws)
            await self._subscribe(ws)

            # Schedule a re-connect just after the window closes
            window_close = self.state.window_close_ts
            loop_task = asyncio.ensure_future(self._message_loop(ws))
            reopen_task = asyncio.ensure_future(
                self._reopen_at_window_close(window_close)
            )
            done, pending = await asyncio.wait(
                {loop_task, reopen_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
            # Propagate any exception from the loop
            for t in done:
                if t.exception():
                    raise t.exception()  # triggers outer reconnect

    async def _message_loop(self, ws) -> None:
        async for raw in ws:
            await self._handle(json.loads(raw))

    async def _reopen_at_window_close(self, close_ts: float) -> None:
        wait = max(0.0, close_ts - time.time()) + 2.0  # 2 s buffer after close
        await asyncio.sleep(wait)
        raise RuntimeError("Window expired — reconnecting to discover new contract")

    # ── Authentication ───────────────────────────────────────────────────────

    def _ws_upgrade_headers(self) -> dict:
        """
        Signed HTTP headers sent with the WebSocket upgrade request.
        Kalshi requires these on the upgrade itself — the server returns
        HTTP 401 before the connection is established if they are absent.
        """
        if not self.cfg.kalshi_api_key_id:
            return {}
        ts = str(int(time.time() * 1000))
        sign_path = "/trade-api/ws/v2"
        sig = self._sign(ts + "GET" + sign_path)
        return {
            "KALSHI-ACCESS-KEY": self.cfg.kalshi_api_key_id,
            "KALSHI-ACCESS-SIGNATURE": sig,
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }

    async def _login(self, ws) -> None:
        if not self.cfg.kalshi_api_key_id:
            await self.state.log_event("Kalshi: no API key — skipping WS login (read-only)")
            return

        ts = str(int(time.time() * 1000))
        # Kalshi spec: sign exactly this string — path is fixed, not derived from URL
        sig = self._sign(ts + "GET" + "/trade-api/ws/v2")

        self._cmd_id += 1
        login_msg = {
            "id": str(self._cmd_id),   # spec requires string id
            "cmd": "login",
            "params": {
                "api_key": self.cfg.kalshi_api_key_id,
                "signature": sig,
                "timestamp": ts,
            },
        }
        await ws.send(json.dumps(login_msg))

    # ── Subscription ─────────────────────────────────────────────────────────

    async def _subscribe(self, ws) -> None:
        ticker = self.state.active_contract
        if not ticker:
            raise RuntimeError("No active contract to subscribe to")
        self._cmd_id += 1
        sub_msg = {
            "id": int(self._cmd_id),          # must be integer, not string
            "cmd": "subscribe",
            "params": {
                "channels": ["orderbook_delta", "ticker"],
                "market_tickers": [ticker],   # list with exactly one ticker string
            },
        }
        await ws.send(json.dumps(sub_msg))
        await self.state.log_event(f"Subscribed to {ticker}")

    # ── Message handling ─────────────────────────────────────────────────────

    async def _handle(self, msg: dict) -> None:
        mtype = msg.get("type")
        data = msg.get("msg", {})

        if mtype == "orderbook_snapshot":
            await self._apply_snapshot(data)
        elif mtype == "orderbook_delta":
            await self._apply_delta(data)
        elif mtype == "ticker":
            await self._apply_ticker(data)
        elif mtype == "error":
            if data.get("code") == 1:
                return  # harmless ack from login command
            await self.state.log_event(f"Kalshi WS server error: {data}")

    async def _apply_snapshot(self, data: dict) -> None:
        # yes_dollars_fp: list of [price_dollars_string, size_string]
        # price is in dollars ("0.5000") → convert to cents (* 100)
        ob = Orderbook(last_update=time.time())
        for price_str, size_str in data.get("yes_dollars_fp", []):
            price_cents = round(float(price_str) * 100)
            size = float(size_str)
            if size > 0:
                ob.yes_bids[price_cents] = size
        for price_str, size_str in data.get("no_dollars_fp", []):
            price_cents = round(float(price_str) * 100)
            size = float(size_str)
            if size > 0:
                # NO bid at X cents → YES ask at (100 - X) cents
                ob.yes_asks[100 - price_cents] = size
        self._ob = ob
        await self.state.update_orderbook(deepcopy(self._ob))

    async def _apply_delta(self, data: dict) -> None:
        # price_dollars: string dollars; delta_fp: string (can be negative)
        price_cents = round(float(data.get("price_dollars", 0)) * 100)
        delta = float(data.get("delta_fp", 0))
        side: str = data.get("side", "yes")

        ob = deepcopy(self._ob)
        ob.last_update = time.time()

        if side == "yes":
            target = ob.yes_bids
            key = price_cents
        else:
            # NO bid at price_cents → YES ask at (100 - price_cents)
            target = ob.yes_asks
            key = 100 - price_cents

        new_qty = target.get(key, 0) + delta
        if new_qty <= 0:
            target.pop(key, None)
        else:
            target[key] = new_qty

        self._ob = ob
        await self.state.update_orderbook(deepcopy(self._ob))

    async def _apply_ticker(self, data: dict) -> None:
        # yes_bid_dollars / yes_ask_dollars are dollar-denominated strings
        bid_str = data.get("yes_bid_dollars")
        ask_str = data.get("yes_ask_dollars")
        oi_str = data.get("open_interest_fp")

        if oi_str is not None:
            await self.state.update_open_interest(float(oi_str))

        if not bid_str and not ask_str:   # only skip if BOTH are missing
            return
        ob = deepcopy(self._ob)
        ob.last_update = time.time()
        # Keep ticker top-of-book separately from the depth book. If we insert
        # ticker prices as fake book levels, old best prices can stick around
        # after the real top-of-book moves.
        if bid_str is not None:
            ob.top_yes_bid = _parse_price_cents(bid_str)
        if ask_str is not None:
            ob.top_yes_ask = _parse_price_cents(ask_str)
        # If bid > ask the ask is stale (market makers withdrew offers).
        if ob.top_yes_bid is not None and ob.top_yes_ask is not None:
            if ob.top_yes_bid >= ob.top_yes_ask:
                ob.top_yes_ask = None
        self._ob = ob
        await self.state.update_orderbook(deepcopy(self._ob))

    # ── Contract discovery ───────────────────────────────────────────────────

    async def _discover_contract(self) -> None:
        now = datetime.now(timezone.utc)
        markets_url = self.cfg.kalshi_rest_base + "/markets"
        # Derive the signing path from the configured URL so it always matches
        markets_path = urlparse(markets_url).path
        params = {
            "series_ticker": self.cfg.btc_series_ticker,
            "status": "open",
            "limit": "50",
        }
        headers = self._rest_headers("GET", markets_path)

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                markets_url,
                params=params,
                headers=headers,
            )
            resp.raise_for_status()
            markets = resp.json().get("markets", [])

        if not markets:
            raise RuntimeError(
                f"No open markets found for series {self.cfg.btc_series_ticker}. "
                "Check BTC_SERIES_TICKER in your .env."
            )

        # Pick the currently active market: smallest positive seconds-to-close
        best: Optional[dict] = None
        best_delta = float("inf")
        for m in markets:
            raw_close = m.get("close_time") or m.get("expiration_time", "")
            if not raw_close:
                continue
            close_dt = datetime.fromisoformat(raw_close.replace("Z", "+00:00"))
            delta = (close_dt - now).total_seconds()
            if 0 < delta < best_delta:
                best_delta = delta
                best = m

        if best is None:
            raise _WindowGap()

        close_dt = datetime.fromisoformat(
            best["close_time"].replace("Z", "+00:00")
        )
        close_ts = close_dt.timestamp()
        open_ts = close_ts - WINDOW_SECONDS

        open_interest = float(best.get("open_interest_fp") or 0)
        await self.state.set_active_contract(
            ticker=best["ticker"],
            close_ts=close_ts,
            open_ts=open_ts,
            open_interest=open_interest,
        )

        # Log raw market fields so we can diagnose what Kalshi returns
        await self.logger.log("market_discovered", {
            k: v for k, v in best.items() if v not in (None, "", [], {})
        })

        # Determine strike (BTC window-open price).
        # Priority: (1) parsed from Kalshi API text fields  →
        #           (2) Coinbase historical candle for open_ts →
        #           (3) in-memory BTC history  →  (4) current BTC price.
        kalshi_strike = _parse_strike_from_market(best)
        if kalshi_strike > 0:
            await self.state.set_btc_open(kalshi_strike)
            strike_src = f"${kalshi_strike:,.2f} (Kalshi API)"
        else:
            hist_price = await self._fetch_btc_at_open(open_ts)
            if hist_price > 0:
                await self.state.set_btc_open(hist_price)
                strike_src = f"${hist_price:,.2f} (Coinbase history)"
            else:
                local = self._btc_at_open(open_ts)
                ref = local if local > 0 else self.state.btc_price
                if ref > 0:
                    await self.state.set_btc_open(ref)
                strike_src = "no history — using live BTC feed"

        await self.state.log_event(
            f"Contract: {best['ticker']}  closes in {best_delta/60:.1f} min  "
            f"strike={strike_src}"
        )

    async def _fetch_btc_at_open(self, open_ts: float) -> float:
        """
        Query Coinbase Exchange candles API (public, no auth) to get BTC price
        at the window open time. Used when the bot starts mid-window and our
        in-memory history doesn't reach back far enough.
        """
        if open_ts > time.time() - 5:
            return 0.0  # window just opened — live feed is accurate enough
        start_iso = datetime.fromtimestamp(open_ts, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        end_iso = datetime.fromtimestamp(open_ts + 120, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        try:
            async with httpx.AsyncClient(timeout=6.0) as client:
                resp = await client.get(
                    "https://api.exchange.coinbase.com/products/BTC-USD/candles",
                    params={"granularity": 60, "start": start_iso, "end": end_iso},
                )
                resp.raise_for_status()
                candles = resp.json()  # [[time, low, high, open, close, volume], ...]
                if not candles:
                    return 0.0
                # Coinbase returns newest-first; find candle whose start is closest
                best = min(candles, key=lambda c: abs(c[0] - open_ts))
                if abs(best[0] - open_ts) <= 90:
                    return float(best[3])  # open price of that candle
        except Exception as exc:
            await self.state.log_event(f"BTC history fetch failed: {exc}")
        return 0.0

    def _btc_at_open(self, open_ts: float) -> float:
        history = list(self.state.btc_history)
        if not history:
            return 0.0
        nearest = min(history, key=lambda t: abs(t[0] - open_ts))
        if abs(nearest[0] - open_ts) > 60.0:
            # Fall back to oldest history entry as best approximation
            return history[0][1] if history else 0.0
        return nearest[1]

    # ── Auth helpers ─────────────────────────────────────────────────────────

    def _sign(self, message: str) -> str:
        return _sign_msg(self.cfg.kalshi_private_key, message)

    def _rest_headers(self, method: str, path: str) -> dict:
        return _make_rest_headers(self.cfg, method, path)


def _sign_msg(private_key, message: str) -> str:
    if private_key is None:
        return ""
    sig = private_key.sign(
        message.encode(),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode()


def _make_rest_headers(cfg: Settings, method: str, path: str) -> dict:
    ts = str(int(time.time() * 1000))
    sig = _sign_msg(cfg.kalshi_private_key, ts + method.upper() + path)
    return {
        "KALSHI-ACCESS-KEY": cfg.kalshi_api_key_id,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "Content-Type": "application/json",
    }


async def fetch_kalshi_settlement(ticker: str, cfg: Settings) -> Optional[str]:
    """
    Query GET /markets/{ticker} and return 'yes', 'no', or None if not yet settled.

    Kalshi uses CF Benchmarks' BRTI (not Coinbase spot) for settlement, so this
    is the only reliable source of truth for binary outcome accuracy tracking.
    settlement_timer_seconds=1 means the result is usually available within a
    few seconds of window close.
    """
    url = f"{cfg.kalshi_rest_base}/markets/{ticker}"
    path = urlparse(url).path
    headers = _make_rest_headers(cfg, "GET", path)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            market = resp.json().get("market", {})
        result = str(market.get("result", "")).lower()
        if result in ("yes", "no"):
            return result
    except Exception:
        pass
    return None


def _parse_price_cents(raw: str) -> Optional[float]:
    price = round(float(raw) * 100)
    if price <= 0 or price >= 100:
        return None
    return float(price)


def _parse_strike_from_market(market: dict) -> float:
    """
    Extract Kalshi's BTC reference price from market metadata.
    Priority:
      1. Numeric fields: floor_strike, cap_strike, strike (returned directly by API)
      2. Text fields: 'Above $81,775.15' in subtitles/title
      3. Ticker format: KXBTC15M-26MAY2016-T81775.15
    Returns 0.0 if not found.
    """
    # 1. Direct numeric fields — most reliable
    for field in ("floor_strike", "cap_strike", "strike", "yes_floor_strike", "no_floor_strike"):
        raw = market.get(field)
        if raw is None:
            continue
        try:
            price = float(raw)
            if 10_000 < price < 1_000_000:
                return price
        except (TypeError, ValueError):
            continue

    # 2. Text fields — regex handles both 81775.15 and $81,775.15 (US comma format)
    _PRICE_RE = re.compile(r"\$?([\d]{1,6}(?:,\d{3})*(?:\.\d{1,2})?)")
    for field in ("yes_sub_title", "no_sub_title", "subtitle", "title", "result_source"):
        text = str(market.get(field) or "")
        if not text:
            continue
        for m in _PRICE_RE.finditer(text):
            try:
                price = float(m.group(1).replace(",", ""))
                if 10_000 < price < 1_000_000:
                    return price
            except ValueError:
                continue

    # 3. Ticker like KXBTC15M-26MAY2016-T81775.15
    ticker = str(market.get("ticker") or "")
    tm = re.search(r"-T([\d]+(?:\.[\d]{1,2})?)$", ticker)
    if tm:
        try:
            price = float(tm.group(1))
            if 10_000 < price < 1_000_000:
                return price
        except ValueError:
            pass

    return 0.0
