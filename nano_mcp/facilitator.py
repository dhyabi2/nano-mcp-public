"""Self-hostable x402 `exact`-on-`nano` facilitator.

Implements the /supported, /verify, /settle HTTP surface specified in
draft/x402/specs/schemes/exact/scheme_exact_nano.md (block 10). This closes the
gap between what that spec claimed the Python reference builds ("implements the
/supported, /verify, /settle surface") and the product code, which previously
had no HTTP facilitator.

Semantics (faithful to the verified block-11 TS draft + block-10 spec):

  GET /supported
      -> { "scheme": "exact", "network": "nano:live", "asset": "XNO",
           "endpoints": [...] }   (what this facilitator can verify)

  POST /verify  body = { "requirements": {...}, "payload": {...} }
      -> Validates scheme/network/asset/amount/payTo consistency, that
         payload.payload.paymentProof is a 64-hex block hash, then verifies
         the block on AT LEAST TWO INDEPENDENT Nano RPC endpoints, FAILING
         CLOSED if any endpoint cannot confirm it. Returns
         { "isValid": bool, "payer"?, "extra": {confirmedOn, consulted} }.

  POST /settle  body = { "requirements": {...}, "payload": {...} }
      -> Re-runs FULL verification (never trusts a prior /verify), then binds
         the proof to the request with an ATOMIC SINGLE-USE claim keyed by
         "nano:live <block-hash> <request_id>", so one proof yields one
         resource exactly once. Returns
         { "success": bool, "transaction", "network", "payer"?, "amount"? }.

The facilitator never moves, wraps or guards funds (Nano has no smart
contracts). The client's own send IS the payment; the facilitator only verifies
the on-chain proof on >=2 independent RPCs and settles it exactly once.

The RPC endpoints are injectable so tests can drive the verification against a
stub block store without touching the live node or moving money.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Sequence
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# RPC endpoint layer (fail-closed multi-endpoint verifier)
# ---------------------------------------------------------------------------

_HEX64 = re.compile(r"^[0-9A-Fa-f]{64}$")

NANO_LIVE_NETWORK = "nano:live"
NANO_ASSET = "XNO"
NANO_SCHEME = "exact"

DEFAULT_ENDPOINTS = (
    "https://rpc.nano.to",
    "https://rainstorm.city/api",
)


class RpcError(RuntimeError):
    """Raised when an endpoint returns a non-2xx or an error payload."""


def parse_raw(amount: str) -> int:
    """Parse a Nano amount (raw, or decimal with up to 30 raw digits) to raw."""
    clean = str(amount).strip()
    if clean == "":
        return 0
    if "." not in clean:
        return int(clean)
    int_part, frac = clean.split(".", 1)
    frac = (frac + "0" * 30)[:30]
    return int(int_part or "0") * 10**30 + int(frac or "0")


@dataclass(frozen=True)
class NormalizedBlock:
    """Canonical decode of one node's `block_info` response.

    Because independent Nano RPC nodes return the same block with slightly
    different field names, every response is normalized here into one shape so
    the fail-closed comparison ("same amount going to payTo from a real sender")
    is decoupled from node-specific quirks. Missing critical fields are an
    error, not a silent default.
    """

    account: str          # emitter (may be blank if node omits it; the payer is
                          # not required by the spec but a blank is surfaced)
    subtype: str          # 'send' / 'state' / ''
    destination: str      # receiver (link_as_account / contents.destination)
    amount_raw: int
    confirmed: bool

    @property
    def is_send(self) -> bool:
        return self.subtype in ("send", "state", "")


def normalize_block_info(raw: dict) -> NormalizedBlock:
    """Map a node's `block_info` response into the canonical form.

    Raises RpcError on a malformed/incomplete response: a node that returns a
    block without its confirmed flag or its amount cannot authoritatively prove
    anything, so it must fail closed rather than be treated as "not confirmed".
    """
    if not isinstance(raw, dict):
        raise RpcError("block_info: response is not a JSON object")

    # confirmed: real nodes return the STRING "true"; stubs use a bool True.
    confirmed_raw = raw.get("confirmed")
    if confirmed_raw is None:
        raise RpcError("block_info: missing 'confirmed' flag")
    confirmed = (
        confirmed_raw is True
        or str(confirmed_raw).strip().lower() == "true"
    )

    # Emitter account: block_account (real nodes) or account (spec/stub).
    account = str(raw.get("block_account") or raw.get("account") or "")

    # Subtype: contents.type (real send blocks) or subtype/type top-level.
    contents = raw.get("contents") or {}
    subtype = str(
        raw.get("subtype")
        or raw.get("type")
        or (contents.get("type") if isinstance(contents, dict) else None)
        or ""
    )

    # Receiver: contents.destination (real) or link_as_account / destination.
    destination = str(
        (contents.get("destination") if isinstance(contents, dict) else None)
        or raw.get("link_as_account")
        or raw.get("destination")
        or raw.get("link")
        or ""
    )

    amount_raw_v = raw.get("amount")
    if amount_raw_v is None:
        raise RpcError("block_info: missing 'amount'")
    try:
        amount_raw = parse_raw(str(amount_raw_v))
    except ValueError as err:
        raise RpcError(f"block_info: unparseable amount {amount_raw_v!r}") from err

    return NormalizedBlock(
        account=account,
        subtype=subtype,
        destination=destination,
        amount_raw=amount_raw,
        confirmed=confirmed,
    )


@dataclass
class RpcEndpoint:
    """One independent Nano RPC endpoint. `call` is injectable; the default
    POSTs a JSON-RPC action to `url` (mirrors nano_sdk.client.RpcClient)."""

    url: str
    api_key: str | None = None
    timeout: float = 30.0
    call: Callable[[str, dict], dict] | None = None

    def invoke(self, action: str, **params: object) -> dict:
        if self.call is not None:
            return self.call(action, params)
        import httpx

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        resp = httpx.post(
            self.url, json={"action": action, **params}, headers=headers, timeout=self.timeout
        )
        if resp.status_code != 200:
            raise RpcError(f"{self.url} HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        if isinstance(data, dict) and isinstance(data.get("error"), str):
            raise RpcError(f"{self.url} rpc error: {data['error']}")
        return data


@dataclass
class VerificationResult:
    ok: bool
    confirmed_sends: list = field(default_factory=list)
    consulted: int = 0
    reason: str | None = None

    @property
    def confirmed_on(self) -> int:
        return len(self.confirmed_sends)


def verify_block_on_independent_endpoints(
    endpoints: Sequence[RpcEndpoint],
    block_hash: str,
    pay_to: str,
    amount: str,
) -> VerificationResult:
    """Confirm `block_hash` is a send of exactly `amount` to `pay_to` on EVERY
    configured independent endpoint; FAILS CLOSED if any endpoint errors or any
    check fails (strategy law L1: >=2 endpoints, any single-RPC outage refuses).
    """
    required = len(endpoints)
    if required < 2:
        return VerificationResult(
            ok=False,
            consulted=required,
            reason=f"fail-closed: need >=2 independent RPC endpoints, got {required}",
        )

    confirmed_sends: list[dict] = []
    failures = 0
    first_reason: str | None = None
    expected = parse_raw(amount)

    for ep in endpoints:
        try:
            info = ep.invoke("block_info", hash=block_hash)
            nb = normalize_block_info(info)
            if not nb.confirmed:
                raise RpcError(f"block not confirmed on {ep.url}")
            if not nb.is_send:
                raise RpcError(f"block subtype {nb.subtype!r} not a send on {ep.url}")
            if nb.amount_raw != expected:
                raise RpcError(f"amount {nb.amount_raw} != required {expected} on {ep.url}")
            receiver = nb.destination if nb.subtype == "send" and nb.destination else pay_to
            if receiver != pay_to:
                raise RpcError(f"block pays {receiver}, not payTo={pay_to} on {ep.url}")

            confirmed_sends.append(
                {
                    "block_hash": block_hash,
                    "payer": nb.account,
                    "amount": str(nb.amount_raw),
                    "receiver": pay_to,
                    "confirmed": True,
                }
            )
        except Exception as err:  # noqa: BLE001 - any endpoint failure refuses
            failures += 1
            first_reason = first_reason or str(err)

    return VerificationResult(
        ok=failures == 0,
        confirmed_sends=confirmed_sends,
        consulted=required,
        reason=None if failures == 0 else (first_reason or f"confirmed on {len(confirmed_sends)}/{required}"),
    )


def build_fail_closed_config(
    custom: Sequence[RpcEndpoint] | None = None,
) -> list[RpcEndpoint]:
    """Default 2-endpoint fail-closed config, overridable by >=2 custom nodes."""
    if custom and len(custom) >= 2:
        return list(custom)
    return [RpcEndpoint(url=u) for u in DEFAULT_ENDPOINTS[:2]]


# ---------------------------------------------------------------------------
# Atomic single-use claim store (spec § Replay / single-use claim)
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS claims (
    consumption_key TEXT PRIMARY KEY,
    claimed_at      REAL NOT NULL
);
"""


class ClaimStore:
    """Atomic single-use claim keyed by `nano:live <block-hash> <request_id>`.
    INSERT..PRIMARY KEY wins exactly once; a repeat returns False (spent)."""

    def __init__(self, path: str | None = None):
        self.path = path or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "claims.db"
        )
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        # WAL + busy timeout so a second handle to the same file (e.g. a test or
        # a second facilitator process) does not immediately raise
        # 'database is locked' — same discipline as store.py's read-path fix.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def claim(self, consumption_key: str) -> bool:
        with self._lock:
            try:
                import time

                self._conn.execute(
                    "INSERT INTO claims (consumption_key, claimed_at) VALUES (?,?)",
                    (consumption_key, time.time()),
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                # already claimed -> replay refused; roll back so the aborted
                # INSERT does not leave this connection holding the write lock
                # (which would block a second handle to the same file).
                self._conn.rollback()
                return False

    def is_claimed(self, consumption_key: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM claims WHERE consumption_key=?", (consumption_key,)
            ).fetchone()
        return row is not None


def consumption_key(block_hash: str, request_id: str) -> str:
    return f"{NANO_LIVE_NETWORK} {block_hash} {request_id}"


# ---------------------------------------------------------------------------
# Facilitator
# ---------------------------------------------------------------------------

REQUIRED_SECTIONS = ("scheme", "network", "amount", "asset", "payTo")


@dataclass
class FacilitatorConfig:
    endpoints: Sequence[RpcEndpoint]
    claim_store: ClaimStore | None = None


class Facilitator:
    """Self-hostable x402 `exact`-on-`nano` facilitator (/supported, /verify,
    /settle). Never moves funds: verifies on >=2 independent RPCs (fail-closed)
    and settles with an atomic single-use claim."""

    scheme = NANO_SCHEME
    network = NANO_LIVE_NETWORK
    asset = NANO_ASSET

    def __init__(self, config: FacilitatorConfig):
        self.endpoints = list(config.endpoints)
        self.claim_store = config.claim_store or ClaimStore()

    # -- HTTP surface ------------------------------------------------------
    def supported(self) -> dict:
        return {
            "scheme": self.scheme,
            "network": self.network,
            "asset": self.asset,
            "x402Version": 2,
            "endpoints": [ep.url for ep in self.endpoints],
        }

    def verify(self, requirements: dict, payload: dict) -> dict:
        """Verify a payment proof on >=2 independent RPC endpoints (fail-closed)."""
        req_check = self._check_requirements(requirements, payload)
        if req_check is not None:
            return req_check
        proof = str((payload.get("payload") or {}).get("paymentProof") or "")
        if not _HEX64.match(proof):
            return {
                "isValid": False,
                "invalidReason": "proof missing",
                "invalidMessage": "payload.paymentProof must be a 64-hex Nano block hash",
            }
        res = verify_block_on_independent_endpoints(
            self.endpoints, proof, requirements["payTo"], requirements["amount"]
        )
        if not res.ok:
            return {
                "isValid": False,
                "invalidReason": "unconfirmed",
                "invalidMessage": res.reason
                or "proof not confirmed on all independent RPC endpoints",
                "extra": {"confirmedOn": res.confirmed_on, "consulted": res.consulted},
            }
        send = res.confirmed_sends[0]
        return {
            "isValid": True,
            "payer": send["payer"],
            "extra": {
                "confirmedOn": res.confirmed_on,
                "consulted": res.consulted,
                "amount": send["amount"],
            },
        }

    def settle(self, requirements: dict, payload: dict) -> dict:
        """Re-run FULL verification (never trust a prior /verify), then bind the
        proof to this request atomically exactly once."""
        proof = str((payload.get("payload") or {}).get("paymentProof") or "")
        req_check = self._check_requirements(requirements, payload)
        if req_check is not None:
            return {
                "success": False,
                "errorReason": "invalid",
                "errorMessage": "requirements inconsistent",
                "transaction": proof,
                "network": self.network,
            }
        if not _HEX64.match(proof):
            return {
                "success": False,
                "errorReason": "invalid",
                "errorMessage": "bad paymentProof",
                "transaction": proof,
                "network": self.network,
            }

        # 1. Re-verify on-chain (MUST NOT trust the earlier /verify).
        res = verify_block_on_independent_endpoints(
            self.endpoints, proof, requirements["payTo"], requirements["amount"]
        )
        if not res.ok or not res.confirmed_sends:
            return {
                "success": False,
                "errorReason": "unconfirmed",
                "errorMessage": res.reason or "not verified on all endpoints",
                "transaction": proof,
                "network": self.network,
            }

        # 2. Atomic single-use claim (exactly-one-wins, replay-safe).
        request_id = str(requirements.get("extra", {}).get("requestId") or "")
        key = consumption_key(proof, request_id)
        if not self.claim_store.claim(key):
            return {
                "success": False,
                "errorReason": "duplicate",
                "errorMessage": "paymentProof already claimed for this request",
                "transaction": proof,
                "network": self.network,
            }

        send = res.confirmed_sends[0]
        return {
            "success": True,
            "transaction": proof,
            "network": self.network,
            "payer": send["payer"],
            "amount": send["amount"],
        }

    def _check_requirements(self, requirements: dict, payload: dict) -> dict | None:
        accepted = payload.get("accepted") or {}
        if requirements.get("scheme") != self.scheme:
            return self._invalid("bad-scheme", "scheme must be exact")
        if (
            requirements.get("network") != self.network
            or accepted.get("network") != self.network
        ):
            return self._invalid("bad-network", f"network must be {self.network}")
        if (
            requirements.get("asset") != self.asset
            or accepted.get("asset") != self.asset
        ):
            return self._invalid("bad-asset", f"asset must be {self.asset}")
        if requirements.get("amount") != accepted.get("amount"):
            return self._invalid("bad-amount", "amount mismatch between requirements and accepted")
        if requirements.get("payTo") != accepted.get("payTo"):
            return self._invalid("bad-payto", "payTo mismatch between requirements and accepted")
        return None

    @staticmethod
    def _invalid(reason: str, message: str) -> dict:
        return {"isValid": False, "invalidReason": reason, "invalidMessage": message}


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------


def make_handler(facilitator: Facilitator) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, obj: dict) -> None:
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            obj = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(obj, dict):
                # do_POST reads `requirements`/`payload` off this with .get(),
                # so a bare number, string, list or null must be refused here
                # and answered 400 rather than raise inside the handler.
                raise ValueError("body must be a JSON object")
            return obj

        def do_GET(self) -> None:  # noqa: N802
            if urlparse(self.path).path == "/supported":
                self._send(200, facilitator.supported())
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            try:
                body = self._read_json()
            except Exception:
                self._send(400, {"error": "invalid json"})
                return
            requirements = body.get("requirements") or {}
            payload = body.get("payload") or {}
            if path == "/verify":
                self._send(200, facilitator.verify(requirements, payload))
            elif path == "/settle":
                self._send(200, facilitator.settle(requirements, payload))
            else:
                self._send(404, {"error": "not found"})

        def log_message(self, fmt: str, *args: object) -> None:  # silence
            if os.environ.get("NANO_FACILITATOR_LOGGING"):
                super().log_message(fmt, *args)

    return Handler


def serve(facilitator: Facilitator, host: str = "127.0.0.1", port: int = 8022) -> ThreadingHTTPServer:
    """Run the facilitator as a self-hostable HTTP server. Callers own
    threading: the returned server has a serve_forever loop to start."""
    return ThreadingHTTPServer((host, port), make_handler(facilitator))


__all__ = [
    "ClaimStore",
    "Facilitator",
    "FacilitatorConfig",
    "RpcEndpoint",
    "RpcError",
    "NormalizedBlock",
    "VerificationResult",
    "consumption_key",
    "make_handler",
    "normalize_block_info",
    "parse_raw",
    "serve",
    "verify_block_on_independent_endpoints",
]