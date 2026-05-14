from __future__ import annotations

import base64
from functools import cached_property
from typing import Literal

from cryptography.hazmat.primitives.serialization import load_pem_private_key
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# ── Trading mode ─────────────────────────────────────────────────────────────
# Change TRADING_MODE in .env to switch between paper and live.
# paper = simulated fills, no real orders placed
# live  = real orders via Kalshi REST API — uses real money


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ── Kalshi ──────────────────────────────────────────────────────────────
    kalshi_env: Literal["demo", "prod"] = Field(env="KALSHI_ENV")
    kalshi_api_key_id: str = Field(default="", env="KALSHI_API_KEY_ID")
    kalshi_private_key_b64: str = Field(default="", env="KALSHI_PRIVATE_KEY_B64")
    btc_series_ticker: str = Field(default="KXBTCD", env="BTC_SERIES_TICKER")

    # ── BTC reference feed ───────────────────────────────────────────────────
    coinbase_ws_url: str = Field(
        default="wss://advanced-trade-ws.coinbase.com",
        env="COINBASE_WS_URL",
    )

    # ── GBM fair-value model ─────────────────────────────────────────────────
    btc_sigma: float = Field(default=0.80, env="BTC_SIGMA")

    # ── Entry-window phase thresholds (for dashboard phase indicator) ─────────
    # Only show "entry open" when this many seconds or fewer remain
    max_entry_window_s: float = Field(default=480.0, env="MAX_ENTRY_WINDOW_S")
    # "Too late" threshold
    min_entry_window_s: float = Field(default=120.0, env="MIN_ENTRY_WINDOW_S")

    # ── Sweet-spot price range (informational — helps pick entry price) ───────
    min_entry_price_cents: float = Field(default=60.0, env="MIN_ENTRY_PRICE_CENTS")
    max_entry_price_cents: float = Field(default=85.0, env="MAX_ENTRY_PRICE_CENTS")

    # ── BTC momentum threshold for recommendation ─────────────────────────────
    momentum_entry_usd: float = Field(default=30.0, env="MOMENTUM_ENTRY_USD")

    # ── Edge filters (only trade when there's a real edge over the market) ────
    # Minimum |btc_change| / tau_seconds — filters out small undecided moves early in window
    min_commitment_rate: float = Field(default=0.20, env="MIN_COMMITMENT_RATE")
    # Minimum gap between GBM probability and Kalshi mid price (cents)
    # Ensures we only trade when our model disagrees meaningfully with the market
    min_gbm_market_gap_cents: float = Field(default=8.0, env="MIN_GBM_MARKET_GAP_CENTS")

    # ── Thin-market filter ────────────────────────────────────────────────────
    min_open_interest: float = Field(default=500.0, env="MIN_OPEN_INTEREST")

    # ── New-window settle delay ───────────────────────────────────────────────
    new_window_settle_s: float = Field(default=15.0, env="NEW_WINDOW_SETTLE_S")

    # ── Velocity pause (flash-crash guard) ───────────────────────────────────
    momentum_threshold_usd: float = Field(default=150.0, env="MOMENTUM_THRESHOLD_USD")

    # ── "Away from the line" indicators ──────────────────────────────────────
    max_line_crossings: int = Field(default=2, env="MAX_LINE_CROSSINGS")
    min_direction_consistency: float = Field(default=0.6, env="MIN_DIRECTION_CONSISTENCY")

    # ── Pre-window technical analysis ─────────────────────────────────────────
    binance_symbol: str = Field(default="BTC-USD", env="BINANCE_SYMBOL")
    binance_klines_interval: str = Field(default="60", env="BINANCE_KLINES_INTERVAL")

    # ── Dashboard ────────────────────────────────────────────────────────────
    dashboard_host: str = Field(default="127.0.0.1", env="DASHBOARD_HOST")
    dashboard_port: int = Field(default=8000, env="DASHBOARD_PORT")

    # ── URLs ─────────────────────────────────────────────────────────────────
    kalshi_rest_base: str = Field(default="", env="KALSHI_REST_BASE")
    kalshi_ws_base: str = Field(default="", env="KALSHI_WS_BASE")

    bankroll: float = Field(default=250.0, env="BANKROLL")

    # ── Executor ─────────────────────────────────────────────────────────────
    trading_mode: Literal["paper", "live"] = Field(default="paper", env="TRADING_MODE")

    def model_post_init(self, __context) -> None:
        host = "demo-api.kalshi.com" if self.kalshi_env == "demo" else "api.elections.kalshi.com"
        if not self.kalshi_rest_base:
            object.__setattr__(self, "kalshi_rest_base", f"https://{host}/trade-api/v2")
        if not self.kalshi_ws_base:
            object.__setattr__(self, "kalshi_ws_base", f"wss://{host}/trade-api/ws/v2")

    @cached_property
    def kalshi_private_key(self):
        if not self.kalshi_private_key_b64:
            return None
        pem = base64.b64decode(self.kalshi_private_key_b64)
        return load_pem_private_key(pem, password=None)


settings = Settings()
