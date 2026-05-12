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
    # Annualised BTC implied vol used in the binary-option fair-value model
    btc_sigma: float = Field(default=0.80, env="BTC_SIGMA")
    # Fire a signal when |fair_value − kalshi_mid| / 100 exceeds this
    signal_threshold: float = Field(default=0.08, env="SIGNAL_THRESHOLD")
    confidence_threshold: float = Field(default=0.60, env="CONFIDENCE_THRESHOLD")
    # Minimum seconds between consecutive signals (debounce)
    signal_debounce_s: float = Field(default=2.0, env="SIGNAL_DEBOUNCE_S")

    # ── Risk ─────────────────────────────────────────────────────────────────
    max_concurrent_positions: int = Field(default=5, env="MAX_CONCURRENT_POSITIONS")
    max_position_size_usd: float = Field(default=20.0, env="MAX_POSITION_SIZE_USD")
    daily_loss_limit_usd: float = Field(default=5.0, env="DAILY_LOSS_LIMIT_USD")
    # Exit when position value drops to this fraction of entry (0.5 = 50%)
    stop_loss_pct: float = Field(default=0.50, env="STOP_LOSS_PCT")
    # Take profit when position has gained this fraction from entry (0.15 = 15%)
    take_profit_pct: float = Field(default=0.15, env="TAKE_PROFIT_PCT")
    # Only enter when at least this many seconds remain in the window
    min_entry_window_s: float = Field(default=240.0, env="MIN_ENTRY_WINDOW_S")
    # Only enter when the contract side costs between these prices (scalp zone)
    min_entry_price_cents: float = Field(default=15.0, env="MIN_ENTRY_PRICE_CENTS")
    max_entry_price_cents: float = Field(default=42.0, env="MAX_ENTRY_PRICE_CENTS")

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

    # ── Momentum / drift guards ───────────────────────────────────────────────
    # BTC move (USD) over 30s to declare a momentum trend (lower = more sensitive)
    momentum_threshold_usd: float = Field(default=150.0, env="MOMENTUM_THRESHOLD_USD")
    # Max fraction BTC can be below (YES) or above (NO) the window-open price
    # before we stop entering that direction entirely (0.003 = 0.3%)
    max_adverse_drift_pct: float = Field(default=0.003, env="MAX_ADVERSE_DRIFT_PCT")
    # When YES ask >= this, momentum filter is bypassed for NO entries (spike fade)
    # When NO ask >= this (YES ask <= 100 - this), momentum filter bypassed for YES entries
    fade_extreme_cents: float = Field(default=72.0, env="FADE_EXTREME_CENTS")

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
