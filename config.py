from __future__ import annotations

import base64
from functools import cached_property
from typing import Literal

from cryptography.hazmat.primitives.serialization import load_pem_private_key
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


LIVE_TRADING_ACK_TEXT = "I_UNDERSTAND_THIS_PLACES_REAL_ORDERS"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ── Kalshi ──────────────────────────────────────────────────────────────
    kalshi_env: Literal["demo", "prod"] = Field(env="KALSHI_ENV")
    kalshi_api_key_id: str = Field(default="", env="KALSHI_API_KEY_ID")
    # Base64-encoded PEM private key  (openssl genrsa 2048 | base64 -w0)
    kalshi_private_key_b64: str = Field(default="", env="KALSHI_PRIVATE_KEY_B64")

    # Series ticker used to auto-discover the active 15-min BTC contract
    btc_series_ticker: str = Field(default="KXBTCD", env="BTC_SERIES_TICKER")

    # ── BTC reference feed ───────────────────────────────────────────────────
    # Coinbase Advanced Trade public WebSocket (no auth)
    coinbase_ws_url: str = Field(
        default="wss://advanced-trade-ws.coinbase.com",
        env="COINBASE_WS_URL",
    )

    # ── Strategy ─────────────────────────────────────────────────────────────
    # Annualised BTC implied vol used in the GBM fair-value model (dashboard only)
    btc_sigma: float = Field(default=0.80, env="BTC_SIGMA")
    confidence_threshold: float = Field(default=0.45, env="CONFIDENCE_THRESHOLD")
    # Minimum seconds between consecutive signals (debounce)
    signal_debounce_s: float = Field(default=2.0, env="SIGNAL_DEBOUNCE_S")

    # ── Risk ─────────────────────────────────────────────────────────────────
    max_concurrent_positions: int = Field(default=5, env="MAX_CONCURRENT_POSITIONS")
    max_position_size_usd: float = Field(default=20.0, env="MAX_POSITION_SIZE_USD")
    daily_loss_limit_usd: float = Field(default=5.0, env="DAILY_LOSS_LIMIT_USD")
    # Exit when position value drops this many cents below entry (absolute)
    stop_loss_cents: float = Field(default=12.0, env="STOP_LOSS_CENTS")
    # Take profit when position gains this many cents above entry (absolute)
    take_profit_cents: float = Field(default=20.0, env="TAKE_PROFIT_CENTS")
    # Only enter when at least this many seconds remain in the window
    min_entry_window_s: float = Field(default=120.0, env="MIN_ENTRY_WINDOW_S")
    # Only enter when the confirmed-winner contract is in this price range
    min_entry_price_cents: float = Field(default=60.0, env="MIN_ENTRY_PRICE_CENTS")
    max_entry_price_cents: float = Field(default=85.0, env="MAX_ENTRY_PRICE_CENTS")

    # ── Paper trading ────────────────────────────────────────────────────────
    starting_balance: float = Field(default=1000.0, env="STARTING_BALANCE")

    # ── Execution ────────────────────────────────────────────────────────────
    trading_mode: Literal["paper", "live"] = Field(default="paper", env="TRADING_MODE")
    live_trading_ack: str = Field(default="", env="LIVE_TRADING_ACK")
    live_unit_size: int = Field(default=10, env="LIVE_UNIT_SIZE")
    live_max_order_cost_usd: float = Field(default=5.0, env="LIVE_MAX_ORDER_COST_USD")
    live_order_cooldown_s: float = Field(default=10.0, env="LIVE_ORDER_COOLDOWN_S")
    live_allow_existing_positions: bool = Field(
        default=False,
        env="LIVE_ALLOW_EXISTING_POSITIONS",
    )

    # ── Thin-market filter ────────────────────────────────────────────────────
    # Block all signals when the market's open_interest_fp is below this value.
    min_open_interest: float = Field(default=500.0, env="MIN_OPEN_INTEREST")

    # ── New-window settle delay ───────────────────────────────────────────────
    # Block all signals for this many seconds after a new market is discovered,
    # giving the BTC feed and orderbook time to stabilise for the new window.
    new_window_settle_s: float = Field(default=15.0, env="NEW_WINDOW_SETTLE_S")

    # ── Momentum strategy ─────────────────────────────────────────────────────
    # Minimum BTC move (USD) from window open to confirm direction
    momentum_entry_usd: float = Field(default=30.0, env="MOMENTUM_ENTRY_USD")
    # Monitor phase: only enter when this many seconds or fewer remain (last 8 min)
    max_entry_window_s: float = Field(default=480.0, env="MAX_ENTRY_WINDOW_S")

    # Hard close all open positions this many seconds before resolution
    force_exit_tau_s: float = Field(default=90.0, env="FORCE_EXIT_TAU_S")

    # ── Velocity pause (flash-crash guard) ───────────────────────────────────
    # BTC move (USD) over 30s that triggers a 30s signal pause
    momentum_threshold_usd: float = Field(default=150.0, env="MOMENTUM_THRESHOLD_USD")

    # ── Pre-window technical analysis (Binance, no auth required) ────────────
    # Symbol and candle interval used to fetch indicators before each window
    binance_symbol: str = Field(default="BTCUSDT", env="BINANCE_SYMBOL")
    binance_klines_interval: str = Field(default="1m", env="BINANCE_KLINES_INTERVAL")
    # Block entries when pre-window bias contradicts the trade direction
    # Set to False to use technicals as display-only without gating trades
    bias_gate_enabled: bool = Field(default=True, env="BIAS_GATE_ENABLED")

    # ── One-and-done ─────────────────────────────────────────────────────────
    # After a winning trade, sit out the rest of the window
    one_and_done: bool = Field(default=True, env="ONE_AND_DONE")

    # ── "Away from the line" edge replication ────────────────────────────────
    # Max number of times Kalshi mid may cross 50¢ during the monitoring window.
    # Few crossings = price committed to one side. Too many = still choppy, skip.
    max_line_crossings: int = Field(default=2, env="MAX_LINE_CROSSINGS")
    # Min fraction of recent 30-second steps where price moved further from 50¢.
    # 0.6 = at least 3 of the last 5 steps trended away from the line.
    min_direction_consistency: float = Field(default=0.6, env="MIN_DIRECTION_CONSISTENCY")

    # ── Slow-market filter ────────────────────────────────────────────────────
    # Block entries when the Kalshi contract price swings more than this many
    # cents over the last 60 seconds (erratic / fast-moving market)
    kalshi_mid_max_range_cents: float = Field(default=22.0, env="KALSHI_MID_MAX_RANGE_CENTS")

    # ── Fees ─────────────────────────────────────────────────────────────────
    # Kalshi taker fee as a fraction of traded dollar value (entry + exit)
    kalshi_taker_fee_pct: float = Field(default=0.07, env="KALSHI_TAKER_FEE_PCT")

    # ── Dashboard ────────────────────────────────────────────────────────────
    dashboard_host: str = Field(default="127.0.0.1", env="DASHBOARD_HOST")
    dashboard_port: int = Field(default=8000, env="DASHBOARD_PORT")

    # ── URLs (read from env; fall back to the canonical Kalshi host for kalshi_env) ──
    kalshi_rest_base: str = Field(default="", env="KALSHI_REST_BASE")
    kalshi_ws_base: str = Field(default="", env="KALSHI_WS_BASE")

    @model_validator(mode="after")
    def _fill_url_defaults(self) -> "Settings":
        host = "demo-api.kalshi.com" if self.kalshi_env == "demo" else "api.kalshi.com"
        if not self.kalshi_rest_base:
            self.kalshi_rest_base = f"https://{host}/trade-api/v2"
        if not self.kalshi_ws_base:
            self.kalshi_ws_base = f"wss://{host}/trade-api/ws/v2"
        if self.trading_mode == "live":
            self._validate_live_trading_settings()
        return self

    def _validate_live_trading_settings(self) -> None:
        if self.live_trading_ack != LIVE_TRADING_ACK_TEXT:
            raise ValueError(
                "TRADING_MODE=live requires LIVE_TRADING_ACK="
                f"{LIVE_TRADING_ACK_TEXT!r}"
            )
        if not self.kalshi_api_key_id or not self.kalshi_private_key_b64:
            raise ValueError("Live trading requires Kalshi API credentials")
        if self.live_max_order_cost_usd > 25.0:
            raise ValueError("Live trading requires LIVE_MAX_ORDER_COST_USD <= 25.0")
        if self.daily_loss_limit_usd > 10.0:
            raise ValueError("Live trading requires DAILY_LOSS_LIMIT_USD <= 10.0")
        if self.live_order_cooldown_s < 2.0:
            raise ValueError("Live trading requires LIVE_ORDER_COOLDOWN_S >= 2.0")

    @cached_property
    def kalshi_private_key(self):
        if not self.kalshi_private_key_b64:
            return None
        pem = base64.b64decode(self.kalshi_private_key_b64)
        return load_pem_private_key(pem, password=None)


settings = Settings()
