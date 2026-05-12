from __future__ import annotations

import base64
import time
from typing import Any
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

from config import Settings


class KalshiOrderClient:
    """Tiny authenticated REST client for live order execution."""

    def __init__(self, cfg: Settings):
        self.cfg = cfg

    async def get_balance(self) -> dict[str, Any]:
        return await self._request("GET", "/portfolio/balance")

    async def get_positions(self) -> dict[str, Any]:
        return await self._request(
            "GET",
            "/portfolio/positions",
            params={"count_filter": "position"},
        )

    async def create_order(self, order: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/portfolio/orders", json=order)

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = self.cfg.kalshi_rest_base + path
        headers = self._headers(method, url)

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json,
            )
            resp.raise_for_status()
            return resp.json()

    def _headers(self, method: str, url: str) -> dict[str, str]:
        ts = str(int(time.time() * 1000))
        sign_path = urlparse(url).path
        sig = self._sign(ts + method.upper() + sign_path)
        return {
            "KALSHI-ACCESS-KEY": self.cfg.kalshi_api_key_id,
            "KALSHI-ACCESS-SIGNATURE": sig,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "Content-Type": "application/json",
        }

    def _sign(self, message: str) -> str:
        key = self.cfg.kalshi_private_key
        if key is None:
            raise RuntimeError("Kalshi private key is not configured")
        sig = key.sign(
            message.encode(),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(sig).decode()
