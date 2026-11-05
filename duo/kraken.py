"""Minimal Kraken spot REST client."""
from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

API_URL = "https://api.kraken.com"


class KrakenError(Exception):
    """Kraken answered and said no. Nothing happened on the exchange."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


class KrakenNetworkError(Exception):
    """No definite answer. For an order, it may or may not have been placed."""


def sign(path: str, postdata: str, nonce: int | str, secret_b64: str) -> str:
    """Builds the API-Sign header: HMAC-SHA512 of path + SHA256(nonce + postdata)."""
    digest = hashlib.sha256((str(nonce) + postdata).encode()).digest()
    mac = hmac.new(base64.b64decode(secret_b64), path.encode() + digest, hashlib.sha512)
    return base64.b64encode(mac.digest()).decode()


class KrakenClient:
    def __init__(self, key: str | None = None, secret: str | None = None, base_url: str = API_URL, timeout: float = 15):
        self.key = key
        self.secret = secret
        self.base_url = base_url
        self.timeout = timeout
        self._nonce_lock = threading.Lock()
        self._last_nonce = 0

    def _send(self, request: urllib.request.Request):
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.load(response)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, http.client.HTTPException) as error:
            # HTTPException: a truncated reply (IncompleteRead) or a bad status line, which aren't OSErrors.
            raise KrakenNetworkError(f"{type(error).__name__}: {error}") from error
        if not isinstance(payload, dict):
            raise KrakenNetworkError(f"Kraken's reply wasn't an object: {str(payload)[:80]}")
        if payload.get("error"):
            raise KrakenError(payload["error"])
        if "result" not in payload:
            raise KrakenNetworkError("Kraken's reply had no result")
        return payload["result"]

    def public(self, method: str, **params):
        query = f"?{urllib.parse.urlencode(params)}" if params else ""
        return self._send(urllib.request.Request(f"{self.base_url}/0/public/{method}{query}"))

    def _next_nonce(self) -> int:
        with self._nonce_lock:
            self._last_nonce = max(self._last_nonce + 1, int(time.time() * 1000))
            return self._last_nonce

    def private(self, method: str, **params):
        if not self.key or not self.secret:
            raise KrakenError(["KRAKEN_API_KEY and KRAKEN_API_SECRET aren't set"])
        path = f"/0/private/{method}"
        nonce = self._next_nonce()
        postdata = urllib.parse.urlencode({"nonce": nonce, **params})
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=postdata.encode(),
            headers={
                "API-Key": self.key,
                "API-Sign": sign(path, postdata, nonce, self.secret),
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        return self._send(request)
