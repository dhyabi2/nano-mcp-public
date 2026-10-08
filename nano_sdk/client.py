"""Thin JSON-RPC-ish client over the public Nano node at rpc.nano.to.

The node is a full live Nano node exposed over HTTP POST JSON (docs.nano.to/nano-rpc).
Reads (version, account_balance, account_info, account_history, block_info, ...) are free;
write actions (process) and PoW (work_generate) may require the NANO_RPC_KEY.
"""
from __future__ import annotations

import os

import httpx

DEFAULT_RPC_URL = "https://rpc.nano.to"


class RpcError(RuntimeError):
    """Raised for every answer that is not a usable node reply.

    That is: a non-2xx response, a body that is not JSON, a body that is not a JSON object, and
    a JSON object carrying an `error` key. A caller handling RpcError has handled all of them.
    """


class RpcClient:
    def __init__(self, url: str | None = None, api_key: str | None = None, timeout: float = 30.0):
        self.url = url or os.environ.get("NANO_RPC_URL") or DEFAULT_RPC_URL
        self.api_key = api_key if api_key is not None else os.environ.get("NANO_RPC_KEY")
        self.timeout = timeout

    def call(self, **payload) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        resp = httpx.post(self.url, json=payload, headers=headers, timeout=self.timeout)
        if resp.status_code != 200:
            raise RpcError(f"rpc.nano.to HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            data = resp.json()
        except ValueError as ex:
            # A 200 carrying something that is not JSON is not a node answer: it is whatever sat
            # between us and the node - a proxy error page, a captive portal, a CDN interstitial.
            # json.JSONDecodeError is a ValueError, so it escaped `except RpcError` entirely.
            raise RpcError(f"rpc.nano.to returned a {resp.status_code} that is not JSON: "
                           f"{resp.text[:300]!r}") from ex
        if not isinstance(data, dict):
            # Every documented action answers with a JSON object, and `call` is annotated `-> dict`.
            # Returning a list or a bare scalar pushed the failure into the caller, which then did
            # data["balance"] on it and raised TypeError far from the cause.
            raise RpcError(f"rpc.nano.to returned {type(data).__name__}, not a JSON object: "
                           f"{str(data)[:300]}")
        if "error" in data:
            raise RpcError(f"rpc error: {data['error']}")
        return data

    # ---- read actions (free) ----
    def version(self) -> dict:
        return self.call(action="version")

    def account_balance(self, account: str) -> dict:
        """account: nano_ address or @username."""
        return self.call(action="account_balance", account=account)

    def account_info(self, account: str) -> dict:
        return self.call(action="account_info", account=account)

    def account_history(self, account: str, count: int = 10, offset: int = 0, sorting: str = "desc") -> dict:
        return self.call(action="account_history", account=account, count=count, offset=offset, sorting=sorting)

    def block_info(self, block_hash: str) -> dict:
        return self.call(action="block_info", hash=block_hash)

    def pending(self, account: str, count: int = 10) -> dict:
        return self.call(action="pending", account=account, count=count)

    # ---- write / PoW actions (may require NANO_RPC_KEY) ----
    def work_generate(self, hash: str) -> dict:
        """Generate proof-of-work for a block. `hash` is the frontier (previous)."""
        return self.call(action="work_generate", hash=hash)

    def process(self, block: dict, subtype: str | None = None) -> dict:
        """Broadcast a signed block. Returns {"hash": <block hash>} on success.

        Submitting `subtype` (send/open/receive/change) is recommended by
        docs.nano.org to avoid incorrect sends and will be required and, in older
        wording, 'highly recommended'.
        """
        payload: dict = {"action": "process", "json_block": "true", "block": block}
        if subtype:
            payload["subtype"] = subtype
        return self.call(**payload)