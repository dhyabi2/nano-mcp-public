"""x402 `exact`-on-`nano` over HTTP: the 402 Resource Server piece (strategy L0).

The facilitator (nano_mcp/facilitator.py) implements the *coordinator* surface
(`/supported`, `/verify`, `/settle`). The missing half of the x402 handshake is
the **Resource Server**: the HTTP endpoint that answers a protected-resource
request with `402 Payment Required` + a `payment-required` header carrying the
nano `PaymentRequired`, then – once the client retries with a
`payment-signature` header carrying a verified, settled payment proof – serves
the requested result with a `payment-response` header:

    01 GET /premium-data                       -> no payment-signature yet
    02 402  payment-required: <b64 PaymentRequired>          (accepts[0] = nano exact req)
    03 client signs a Nano *send* of exactly amount to payTo (its own seed),
    04 retry GET /premium-data  payment-signature: <b64 PaymentPayload>
    05 resource server POSTs {x402Version, paymentPayload, paymentRequirements}
       to the facilitator's /verify, then /settle (re-verify + atomic claim)
    12 200  <protected result>  payment-response: <b64 SettlementResponse>

Wire fidelity (matches the x402 README's 12-step flow and @x402/core v2 types):

    PaymentRequirements = {scheme, network, asset, amount, payTo,
                           maxTimeoutSeconds, extra{requestId}}
    PaymentRequired     = {x402Version: 2, accepts: [PaymentRequirements]}
    PaymentPayload      = {x402Version: 2, accepted: PaymentRequirements,
                           payload: {paymentProof: <64-hex block hash>}}
    SettleResponse      = {success, transaction, network, payer, amount}

The resource server is stateless except for the facilitator: the one-time
`payTo` is derived deterministically from (server master seed, requestId) and the
amount is a fixed per-resource price, so the server can re-derive the exact
requirements it issued for any presented `accepted` value and reject a payload
whose `accepted` does not match what the server would have issued. No funds move
here; settlement is binding an on-chain proof exactly once (the facilitator's
atomic claim). This is local, self-hostable code — nothing here calls outward.
"""
from __future__ import annotations

import base64
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Sequence
from urllib.parse import urlparse

from nano_mcp.facilitator import Facilitator
from nano_mcp.oneshot import derive_one_time_account, new_request_id

# x402 wire headers (HTTP header names are case-insensitive; lowercased here).
PAYMENT_REQUIRED_HEADER = "payment-required"
PAYMENT_SIGNATURE_HEADER = "payment-signature"
PAYMENT_RESPONSE_HEADER = "payment-response"

_X402_VERSION = 2
_HEX64 = re.compile(r"^[0-9A-Fa-f]{64}$")


# ---------------------------------------------------------------- wire helpers


def b64encode_json(obj: dict) -> str:
    """Encode a JSON object as a base64 UTF-8 string (the x402 PAYMENT-* value)."""
    raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def b64decode_json(value: str) -> dict:
    """Decode a base64 PAYMENT-* header into its JSON object, tolerant to both
    standard and URL-safe alphabets and to missing padding.

    A header that decodes to well-formed JSON which is not an object (a bare
    number, string, list or null) is rejected here rather than handed on: every
    caller goes straight to `.get()`, so a non-object would raise AttributeError
    inside the request handler and drop the connection instead of answering 400.
    """
    s = value.strip()
    s = s.replace("-", "+").replace("_", "/")
    s += "=" * (-len(s) % 4)
    obj = json.loads(base64.b64decode(s.encode("ascii")))
    if not isinstance(obj, dict):
        raise ValueError("PAYMENT-* header must decode to a JSON object")
    return obj


def payment_required_obj(
    requirements: dict,
    resource: dict | None = None,
    error: str | None = None,
    x402_version: int = _X402_VERSION,
) -> dict:
    """Build the x402 `PaymentRequired` object (what the 402 header encodes)."""
    obj: dict = {"x402Version": x402_version, "accepts": [requirements]}
    if resource is not None:
        obj["resource"] = resource
    if error is not None:
        obj["error"] = error
    return obj


def derive_requirements(
    master_secret: bytes,
    request_id: str,
    amount_raw: str,
    max_timeout: int = 60,
) -> dict:
    """The PaymentRequirements the resource server issues for a request: a
    deterministic one-time nano_ `payTo` derived from (master, requestId) plus
    the exact raw amount. Deterministic and stateless, so the server can
    re-derive what it would have issued for any presented `accepted`."""
    pay_to = derive_one_time_account(master_secret, request_id).address
    return {
        "scheme": "exact",
        "network": "nano:live",
        "asset": "XNO",
        "amount": str(amount_raw),
        "payTo": pay_to,
        "maxTimeoutSeconds": max_timeout,
        "extra": {"requestId": request_id},
    }


def build_payment_payload(requirements: dict, proof: str, x402_version: int = _X402_VERSION) -> dict:
    """The client-side PaymentPayload presented in the payment-signature header."""
    if not _HEX64.match(proof):
        raise ValueError("paymentProof must be a 64-hex Nano block hash")
    return {
        "x402Version": x402_version,
        "accepted": requirements,
        "payload": {"paymentProof": proof},
    }


def _accepted_matches(server_requirements: dict, accepted: dict) -> str | None:
    """Return an error string if a presented `accepted` differs from the
    requirements the server itself would issue; None if they agree."""
    for key in ("scheme", "network", "asset", "amount", "payTo"):
        if accepted.get(key) != server_requirements.get(key):
            return f"accepted.{key} does not match the requirements issued for this request"
    if accepted.get("extra", {}).get("requestId") != server_requirements["extra"]["requestId"]:
        return "accepted.extra.requestId does not match the requirements issued for this request"
    return None


# ---------------------------------------------------------------- resource app


class ResourceApp:
    """The x402 resource-server state for the `exact`-on-`nano` scheme.

    Encapsulates the full handshake as two pure actions against a caller-supplied
    `Facilitator` (which owns the >=2-RPC fail-closed verification and the atomic
    single-use claim). `begin()` issues the 402 `PaymentRequired`; `complete()`
    verifies and settles a presented `PaymentPayload` and, only on settlement
    success, authorizes serving the protected resource. All money math and every
    proof bind live in the facilitator; this class determines *whether* to serve.
    """

    def __init__(
        self,
        master_secret: bytes,
        facilitator: Facilitator,
        amount_raw: str,
        resource: dict | None = None,
        max_timeout: int = 60,
        request_id_gen: Callable[[], str] = new_request_id,
    ):
        if not isinstance(master_secret, bytes) or len(master_secret) < 16:
            raise ValueError("master_secret must be bytes of at least 16 bytes")
        self.master_secret = master_secret
        self.facilitator = facilitator
        self.amount_raw = str(amount_raw)
        self.resource = resource or {
            "url": "https://localhost/premium-data",
            "description": "Protected premium data",
            "mimeType": "application/json",
        }
        self.max_timeout = max_timeout
        self._request_id_gen = request_id_gen

    def begin(self) -> dict:
        """Issue a fresh one-time nano requirement and the 402 PaymentRequired."""
        request_id = self._request_id_gen()
        requirements = derive_requirements(
            self.master_secret, request_id, self.amount_raw, self.max_timeout
        )
        return {
            "request_id": request_id,
            "requirements": requirements,
            "payment_required": payment_required_obj(requirements, resource=self.resource),
        }

    def complete(self, payload: dict) -> dict:
        """Verify + settle a presented PaymentPayload against a fresh derivation
        of the server's own requirements for that request. Returns a result dict
        the HTTP layer turns into a response:

          {ok: True,  settlement: <SettleResponse dict>, resource: self.resource}
          {ok: False, error_reason: str, error_message: str, invalid: True}
        """
        accepted = payload.get("accepted") or {}
        request_id = str((accepted.get("extra") or {}).get("requestId") or "")
        if not request_id:
            return {
                "ok": False,
                "invalid": True,
                "error_reason": "invalid",
                "error_message": "payload.accepted.extra.requestId is required",
            }
        server_requirements = derive_requirements(
            self.master_secret, request_id, self.amount_raw, self.max_timeout
        )
        mismatch = _accepted_matches(server_requirements, accepted)
        if mismatch is not None:
            return {
                "ok": False,
                "invalid": True,
                "error_reason": "invalid",
                "error_message": mismatch,
            }

        # Verify (>=2 independent RPCs, fail-closed), then settle (re-verify +
        # atomic single-use claim). The facilitator never trusts a prior result.
        verify = self.facilitator.verify(server_requirements, payload)
        if not verify.get("isValid"):
            return {
                "ok": False,
                "invalid": True,
                "error_reason": verify.get("invalidReason", "unverified"),
                "error_message": verify.get("invalidMessage", "payment could not be verified"),
            }
        settlement = self.facilitator.settle(server_requirements, payload)
        if not settlement.get("success"):
            return {
                "ok": False,
                "invalid": False,
                "settlement": settlement,
                "error_reason": settlement.get("errorReason", "not-settled"),
                "error_message": settlement.get("errorMessage", "payment could not be settled"),
            }
        return {"ok": True, "settlement": settlement, "resource": self.resource}


# ---------------------------------------------------------------- HTTP adapters


def make_resource_handler(
    app: ResourceApp, protected_handler: Callable[[dict], dict] | None = None
) -> type[BaseHTTPRequestHandler]:
    """Build a BaseHTTPRequestHandler serving one protected resource.

    `protected_handler(resource_info) -> body dict` returns the result body
    (defaults to echo the resource). The handler implements the x402 wire:

      GET /<path> (no payment-signature)     -> 402 + payment-required header
      GET /<path> (valid payment-signature)  -> 200 + body + payment-response
      GET /<path> (invalid/spent signature)  -> 402 (+ 400 on bad encoding)

    Every called route runs `app.begin()`/`app.complete()`; the resource body is
    ONLY produced after `complete()` reports ok (settlement success).
    """

    def runner() -> dict:
        if protected_handler is None:
            return {"result": "ok"}
        return protected_handler(app.resource)

    class Handler(BaseHTTPRequestHandler):
        def _respond(self, code: int, body: dict, headers: dict[str, str]) -> None:
            body_bytes = json.dumps(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body_bytes)))
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body_bytes)

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path != "/":
                self._respond(404, {"error": "not found"}, {})
                return
            sig = self.headers.get(PAYMENT_SIGNATURE_HEADER)
            if sig is None:
                started = app.begin()
                encoded = b64encode_json(started["payment_required"])
                self._respond(
                    402,
                    {"error": "payment required", "accepts": [started["requirements"]]},
                    {PAYMENT_REQUIRED_HEADER: encoded},
                )
                return
            try:
                payload = b64decode_json(sig)
            except Exception:
                self._respond(400, {"error": "invalid payment-signature encoding"}, {})
                return
            result = app.complete(payload)
            if result.get("ok"):
                headers = {
                    PAYMENT_RESPONSE_HEADER: b64encode_json(result["settlement"]),
                }
                self._respond(200, runner(), headers)
                return
            # Invalid or spent proof: never serve. A spent proof carries no
            # fresh requirement (matches the facilitator's duplicate disposition).
            if result.get("invalid"):
                started = app.begin()
                encoded = b64encode_json(started["payment_required"])
                headers = {PAYMENT_REQUIRED_HEADER: encoded}
                body = {
                    "error": "payment required",
                    "reason": result.get("error_reason"),
                    "message": result.get("error_message"),
                }
                self._respond(402, body, headers)
                return
            body = {"error": "not-served", "reason": result.get("error_reason"),
                    "message": result.get("error_message")}
            self._respond(402, body, {})

        def log_message(self, fmt: str, *args: object) -> None:  # noqa: N802
            return

    return Handler


def serve_resource(
    app: ResourceApp, protected_handler: Callable[[dict], dict] | None = None,
    host: str = "127.0.0.1", port: int = 8033,
) -> ThreadingHTTPServer:
    """Run the resource server as a self-hostable HTTP server. Callers own
    threading: the returned server has a serve_forever loop to start."""
    return ThreadingHTTPServer((host, port), make_resource_handler(app, protected_handler))


def pay_and_fetch(client, url: str, payer, path: str = "/"):
    """The x402 two-request wire client used by the demo and tests.

    `client` is anything exposing `get(url, headers=...)` returning an object
    with `.status_code`, `.text`, `.headers` (httpx.Client). `payer` exposes
    `pay(payTo, amount_raw) -> <64-hex block hash>` (mirror of the TS NanoPayer).
    Returns (status_code, body_text, settlement_dict|None).
    """
    first = client.get(url)
    assert first.status_code == 402, f"expected 402, got {first.status_code}"
    required = b64decode_json(first.headers[PAYMENT_REQUIRED_HEADER])
    requirements = required["accepts"][0]
    proof = payer.pay(requirements["payTo"], requirements["amount"])
    payload = build_payment_payload(requirements, proof)
    second = client.get(
        url, headers={PAYMENT_SIGNATURE_HEADER: b64encode_json(payload)}
    )
    settlement = None
    if second.status_code == 200 and PAYMENT_RESPONSE_HEADER in second.headers:
        settlement = b64decode_json(second.headers[PAYMENT_RESPONSE_HEADER])
    return second.status_code, second.text, settlement


__all__ = [
    "PAYMENT_REQUIRED_HEADER",
    "PAYMENT_SIGNATURE_HEADER",
    "PAYMENT_RESPONSE_HEADER",
    "ResourceApp",
    "build_payment_payload",
    "b64decode_json",
    "b64encode_json",
    "derive_requirements",
    "make_resource_handler",
    "pay_and_fetch",
    "payment_required_obj",
    "serve_resource",
]