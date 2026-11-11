"""Thin Delta Exchange India REST client (signing, retries, the few endpoints the bot needs).

Verified against Delta's docs (docs.delta.exchange): base URLs, headers (api-key, timestamp,
signature, User-Agent), HMAC-SHA256 of method+timestamp+path+query+body, 5 s timestamp window,
POST /v2/orders (+bracket_* fields), POST /v2/products/{id}/orders/leverage, GET /v2/positions,
GET /v2/wallet/balances, GET /v2/tickers/{symbol}, GET /v2/history/candles (<=2000 candles).
NOT verified live (no network from my sandbox): see README "Unverified" list.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pandas as pd
import requests

PROD = "https://api.india.delta.exchange"
TESTNET = "https://cdn-ind.testnet.deltaex.org"
USER_AGENT = "four-stage-bot/1.0"
RES_SECONDS = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
               "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "1d": 86400}


class DeltaError(Exception):
    """The exchange answered and said no (or we got a non-retryable HTTP error)."""


class UncertainError(Exception):
    """A non-idempotent request may or may not have been executed (timeout / network error)."""


class DeltaClient:
    def __init__(self, base: str = PROD, api_key: str | None = None, api_secret: str | None = None,
                 session: requests.Session | None = None, timeout: float = 10.0, time_fn=time.time,
                 sleep_fn=time.sleep):
        self.base = base.rstrip("/")
        self.key, self.secret = api_key, api_secret
        self.s = session or requests.Session()
        self.timeout, self.time_fn, self.sleep = timeout, time_fn, sleep_fn

    # ------------------------------------------------------------ signing
    @staticmethod
    def sign(secret: str, method: str, ts: str, path: str, query: str, body: str) -> str:
        msg = method + ts + path + query + body
        return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()

    def request(self, method: str, path: str, params: dict | None = None, body: dict | None = None,
                auth: bool = False, retries: int = 3):
        full_path = "/v2" + path
        query = ("?" + urlencode(params)) if params else ""
        payload = json.dumps(body, separators=(",", ":")) if body is not None else ""
        idempotent = method == "GET"
        last = None
        for attempt in range(retries + 1):
            headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
            if payload:
                headers["Content-Type"] = "application/json"
            if auth:
                if not (self.key and self.secret):
                    raise DeltaError("API key/secret not set")
                ts = str(int(self.time_fn()))               # re-signed on every attempt (5 s window)
                headers.update({"api-key": self.key, "timestamp": ts,
                                "signature": self.sign(self.secret, method, ts, full_path, query, payload)})
            try:
                r = self.s.request(method, self.base + full_path + query, data=payload or None,
                                   headers=headers, timeout=self.timeout)
            except (requests.Timeout, requests.ConnectionError) as e:
                if not idempotent:
                    raise UncertainError(f"{method} {path}: {e}") from e
                last = e
                self.sleep(min(2 ** attempt, 8))
                continue
            if r.status_code == 429:                         # rejected before processing -> safe to retry
                wait = float(r.headers.get("X-RATE-LIMIT-RESET", 1000)) / 1000.0
                self.sleep(min(max(wait, 0.5), 30))
                last = DeltaError("rate limited")
                continue
            if r.status_code >= 500:
                if not idempotent:
                    raise UncertainError(f"{method} {path}: HTTP {r.status_code}")
                last = DeltaError(f"HTTP {r.status_code}")
                self.sleep(min(2 ** attempt, 8))
                continue
            try:
                j = r.json()
            except ValueError:
                raise DeltaError(f"{method} {path}: non-JSON response HTTP {r.status_code}")
            if r.status_code >= 400 or not j.get("success", True):
                raise DeltaError(f"{method} {path}: HTTP {r.status_code} {j.get('error', j)}")
            return j
        raise DeltaError(f"{method} {path}: failed after retries ({last})")

    # ------------------------------------------------------------- public
    def get_product(self, symbol: str) -> dict:
        return self.request("GET", f"/products/{symbol}")["result"]

    def get_ticker(self, symbol: str) -> dict:
        return self.request("GET", f"/tickers/{symbol}")["result"]

    def get_candles(self, symbol: str, resolution: str, start: int, end: int) -> pd.DataFrame:
        """Closed candles with open time in [start, end). Pages in chunks of <=1500 candles."""
        secs = RES_SECONDS[resolution]
        rows, cur = [], int(start)
        while cur < end:
            nxt = min(end, cur + 1500 * secs)
            j = self.request("GET", "/history/candles",
                             params={"resolution": resolution, "symbol": symbol, "start": cur, "end": nxt})
            rows += j.get("result", [])
            cur = nxt
        if not rows:
            return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])
        df = pd.DataFrame(rows).drop_duplicates("time").sort_values("time")
        now = int(self.time_fn())
        df = df[(df["time"] + secs <= now) & (df["time"] >= start)].reset_index(drop=True)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        return df[["time", "open", "high", "low", "close", "volume"]].astype(
            {"open": float, "high": float, "low": float, "close": float, "volume": float})

    # ------------------------------------------------------------ private
    def get_balances(self) -> dict:
        return self.request("GET", "/wallet/balances", auth=True)

    def get_position(self, product_id: int) -> dict | None:
        return self.request("GET", "/positions", params={"product_id": product_id}, auth=True).get("result")

    def set_leverage(self, product_id: int, leverage: int) -> dict:
        return self.request("POST", f"/products/{product_id}/orders/leverage",
                            body={"leverage": leverage}, auth=True)["result"]

    def place_order(self, body: dict) -> dict:
        return self.request("POST", "/orders", body=body, auth=True, retries=0)["result"]

    def cancel_all(self, product_id: int) -> dict:
        return self.request("DELETE", "/orders/all", body={"product_id": product_id}, auth=True, retries=0)
