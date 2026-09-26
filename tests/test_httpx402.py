"""Block 14 tests: the x402 `exact`-on-`nano` Resource Server over HTTP (strategy L0).

Blocks 10-13 built the x402 *exact*-on-`nano` spec + reference implementation + a
self-hostable facilitator (`/supported`, `/verify`, `/settle`) that fails closed
on >=2 independent RPC endpoints. The missing half of the protocol (strategy law
L0: "Any 402 seller can accept XNO ... returns HTTP 402 with a nano requirement,
then serves the result after a real XNO payment") is the **Resource Server**: the
HTTP endpoint that answers `402 Payment Required` with a `payment-required`
header carrying the nano requirement, then serves the protected result after the
client retries with a `payment-signature` payload that the facilitator verifies
and settles.

This test drives the REAL wire over a live in-process HTTP server:

  L19 — a nano exact resource server returns 402 + a `payment-required` header
        with a one-time nano_ payTo and exact amount, and once the client
        presents a settled `payment-signature` payload it serves the protected
        result with a `payment-response` header (one payment, one resource).
  L20 — a spent payment proof is refused, so one payment never serves the
        resource a second time.

No funds move and no live node is touched: the RPC endpoints are stub
`RpcEndpoint` objects backed by an in-memory block store that emits the REAL
Nano block_info shape (block_account / contents.type / contents.destination /
confirmed as the STRING "true") that block 13 taught the verifier to parse.
"""
import threading
from http.server import ThreadingHTTPServer

import httpx
import pytest

from nano_mcp.facilitator import Facilitator, FacilitatorConfig, RpcEndpoint
from nano_mcp.httpx402 import (
    PAYMENT_REQUIRED_HEADER,
    PAYMENT_RESPONSE_HEADER,
    PAYMENT_SIGNATURE_HEADER,
    ResourceApp,
    b64decode_json,
    b64encode_json,
    build_payment_payload,
    make_resource_handler,
    pay_and_fetch,
)
from nano_mcp.oneshot import derive_one_time_account

AMOUNT_RAW = "1000000000000000000000000000000"  # 1 XNO raw
PAYER = "nano_3p1zmep1qax1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f9"


class BlockStore:
    """In-memory block_info store keyed by block hash, emitting the REAL Nano
    node shape: block_account / amount / confirmed:"true" / contents.{type,
    destination}."""

    def __init__(self):
        self.blocks: dict[str, dict] = {}

    def add(self, block_hash: str, *, pay_to: str, amount: str = AMOUNT_RAW,
            confirmed: bool = True):
        self.blocks[block_hash] = {
            "block_account": PAYER,
            "amount": str(amount),
            "confirmed": "true" if confirmed else "false",
            "contents": {"type": "send", "destination": pay_to},
        }

    def call(self, action: str, params: dict) -> dict:
        block = self.blocks.get(params.get("hash", ""))
        if block is None:
            raise RuntimeError(f"unknown block {params.get('hash')}")
        return block


def make_endpoint(store: BlockStore, url: str = "https://rpc.nano.to") -> RpcEndpoint:
    return RpcEndpoint(url=url, call=store.call)


def _proof_for(idx: int) -> str:
    return f"{idx:040x}{'C' * 24}"  # 40 + 24 = 64 hex


class WalletPayer:
    """The client's wallet in the test: 'broadcasts' a confirmed send of exactly
    amount to pay_to into both independent nodes, so the proof is confirmable.
    Returns the 64-hex block hash (the paymentProof)."""

    def __init__(self, stores: list[BlockStore]):
        self.stores = stores
        self.sent: list[tuple[str, str, str]] = []

    def pay(self, pay_to: str, amount_raw: str) -> str:
        proof = _proof_for(len(self.sent) + 1)
        for store in self.stores:
            store.add(proof, pay_to=pay_to, amount=amount_raw)
        self.sent.append((pay_to, amount_raw, proof))
        return proof


def _build_stack(broken: bool = False):
    store_a, store_b = BlockStore(), BlockStore()
    endpoints = [
        make_endpoint(store_a),
        make_endpoint(store_b, "https://secondary.nano.city"),
    ]
    if broken:
        endpoints[1] = RpcEndpoint(
            url="https://broken.example",
            call=lambda a, p: (_ for _ in ()).throw(RuntimeError("down")),
        )
    facilitator = Facilitator(FacilitatorConfig(endpoints=endpoints))
    app = ResourceApp(
        master_secret=bytes(range(32)),
        facilitator=facilitator,
        amount_raw=AMOUNT_RAW,
        resource={"url": "https://localhost/premium-data",
                  "description": "premium data", "mimeType": "application/json"},
    )

    def handler(resource: dict) -> dict:
        return {"ok": True, "data": "the protected result", "resource": resource["description"]}

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_resource_handler(app, handler))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return app, store_a, store_b, httpd


@pytest.fixture
def stack():
    app, store_a, store_b, httpd = _build_stack()
    client = httpx.Client(base_url=f"http://127.0.0.1:{httpd.server_address[1]}")
    payer = WalletPayer([store_a, store_b])
    try:
        yield app, client, payer
    finally:
        client.close()
        httpd.shutdown()
        httpd.server_close()


# --------------------------------------------------------------- wire mechanics


def test_b64_headers_round_trip():
    obj = {"x402Version": 2, "accepts": [{"scheme": "exact", "payTo": "nano_abc"}]}
    assert b64decode_json(b64encode_json(obj)) == obj


def test_one_time_payto_is_distinct_per_request():
    app, _client, _payer = _app_only()
    r1 = app.begin()
    r2 = app.begin()
    assert r1["request_id"] != r2["request_id"]
    assert r1["requirements"]["payTo"] != r2["requirements"]["payTo"]
    assert r1["requirements"]["payTo"].startswith("nano_")
    assert r1["requirements"]["scheme"] == "exact"
    assert r1["requirements"]["network"] == "nano:live"
    assert r1["requirements"]["asset"] == "XNO"
    assert r1["requirements"]["amount"] == AMOUNT_RAW


def _app_only():
    app, _a, _b, httpd = _build_stack()
    # close the throwaway http server immediately; only the app is under test here
    httpd.shutdown()
    httpd.server_close()
    return app, None, None


# ------------------------------------------------- L19: the full HTTP handshake


def test_402_then_serves_after_settled_payment(stack):
    """First GET returns 402 with a nano payment-required header; after the
    client presents a verified+settled payment-signature the server serves the
    protected result 200 with a payment-response header."""
    app, client, payer = stack

    first = client.get("/")
    assert first.status_code == 402
    assert PAYMENT_REQUIRED_HEADER in first.headers
    required = b64decode_json(first.headers[PAYMENT_REQUIRED_HEADER])
    assert required["x402Version"] == 2
    req = required["accepts"][0]
    assert req["scheme"] == "exact"
    assert req["network"] == "nano:live"
    assert req["asset"] == "XNO"
    assert req["amount"] == AMOUNT_RAW
    assert req["payTo"].startswith("nano_")  # the one-time server-owned address

    # The client derives/signs a send to that exact payTo/amount, then retries.
    proof = payer.pay(req["payTo"], req["amount"])
    payload = build_payment_payload(req, proof)
    second = client.get("/", headers={PAYMENT_SIGNATURE_HEADER: b64encode_json(payload)})
    assert second.status_code == 200
    assert '"the protected result"' in second.text
    assert PAYMENT_RESPONSE_HEADER in second.headers
    settle = b64decode_json(second.headers[PAYMENT_RESPONSE_HEADER])
    assert settle["success"] is True
    assert settle["transaction"] == proof
    assert settle["network"] == "nano:live"


def test_wire_client_completes_the_two_request_dance(stack):
    """pay_and_fetch performs the full 402 -> pay -> retry -> 200 flow and
    returns the served body plus the decoded settlement response."""
    _app, client, payer = stack
    status, body, settlement = pay_and_fetch(client, "/", payer)
    assert status == 200
    assert "the protected result" in body
    assert settlement is not None
    assert settlement["success"] is True
    assert settlement["transaction"] == payer.sent[-1][2]


def test_protected_handler_runs_only_on_success(stack):
    _app, client, payer = stack
    status, body, _ = pay_and_fetch(client, "/", payer)
    assert status == 200
    assert '"ok": true' in body


# ---------------------------------------------- L20: single-use / replay refusal


def test_spent_proof_never_serves_twice(stack):
    """Replaying the SAME payment-signature header a second time yields 402,
    never a second resource: the facilitator's atomic claim binds one proof to
    one request exactly once."""
    _app, client, payer = stack

    first = client.get("/")
    required = b64decode_json(first.headers[PAYMENT_REQUIRED_HEADER])
    req = required["accepts"][0]
    proof = payer.pay(req["payTo"], req["amount"])
    payload = build_payment_payload(req, proof)
    header = {PAYMENT_SIGNATURE_HEADER: b64encode_json(payload)}

    ok = client.get("/", headers=header)
    assert ok.status_code == 200
    replay = client.get("/", headers=header)
    assert replay.status_code == 402  # spent; the facilitator returns duplicate
    assert "the protected result" not in replay.text


def test_unconfirmed_proof_is_refused(stack):
    """A proof the nodes report as unconfirmed never serves the resource."""
    _app, client, payer = stack
    first = client.get("/")
    required = b64decode_json(first.headers[PAYMENT_REQUIRED_HEADER])
    req = required["accepts"][0]
    # register an UNCONFIRMED block on both stores
    for store in payer.stores:
        store.add(_proof_for(99), pay_to=req["payTo"], confirmed=False)
    payload = build_payment_payload(req, _proof_for(99))
    resp = client.get("/", headers={PAYMENT_SIGNATURE_HEADER: b64encode_json(payload)})
    assert resp.status_code == 402
    assert "the protected result" not in resp.text


def test_tampered_accepted_is_refused(stack):
    """Changing amount in the presented `accepted` is refused: the server
    re-derives the requirements it issued and rejects a payload that does not
    match (structural request binding, no memo)."""
    _app, client, payer = stack
    first = client.get("/")
    required = b64decode_json(first.headers[PAYMENT_REQUIRED_HEADER])
    req = dict(required["accepts"][0])
    req["amount"] = "2" + req["amount"][1:]  # tamper the exact amount
    payload = build_payment_payload(req, payer.pay(req["payTo"], req["amount"]))
    resp = client.get("/", headers={PAYMENT_SIGNATURE_HEADER: b64encode_json(payload)})
    assert resp.status_code == 402
    assert "the protected result" not in resp.text


def test_fail_closed_single_endpoint_refuses():
    """One of the two independent endpoints being unreachable refuses the whole
    payment: the resource server stays 402 and never serves (strategy L1)."""
    app, store_a, _store_b, httpd = _build_stack(broken=True)
    client = httpx.Client(base_url=f"http://127.0.0.1:{httpd.server_address[1]}")
    payer = WalletPayer([store_a])
    try:
        first = client.get("/")
        assert first.status_code == 402
        required = b64decode_json(first.headers[PAYMENT_REQUIRED_HEADER])
        req = required["accepts"][0]
        payload = build_payment_payload(req, payer.pay(req["payTo"], req["amount"]))
        resp = client.get("/", headers={PAYMENT_SIGNATURE_HEADER: b64encode_json(payload)})
        assert resp.status_code == 402
        assert "the protected result" not in resp.text
    finally:
        client.close()
        httpd.shutdown()
        httpd.server_close()


def test_missing_request_id_in_accepted_is_refused(stack):
    _app, client, payer = stack
    first = client.get("/")
    required = b64decode_json(first.headers[PAYMENT_REQUIRED_HEADER])
    req = dict(required["accepts"][0])
    req["extra"] = {}  # drop the requestId that names the one-time address
    payload = build_payment_payload(req, payer.pay(req["payTo"], req["amount"]))
    resp = client.get("/", headers={PAYMENT_SIGNATURE_HEADER: b64encode_json(payload)})
    assert resp.status_code == 402
    assert "the protected result" not in resp.text


def test_bad_payment_signature_encoding_is_400(stack):
    _app, client, _payer = stack
    resp = client.get("/", headers={PAYMENT_SIGNATURE_HEADER: "not-base64!!!"})
    assert resp.status_code == 400

def test_payment_signature_that_is_valid_json_but_not_an_object_is_400(stack):
    """A payment-signature whose base64 decodes to well-formed JSON that is not
    an object must be answered 400, not drop the connection.

    `complete()` reads `payload.get("accepted")`, so a bare number or `null`
    used to raise AttributeError inside do_GET; BaseHTTPRequestHandler has no
    handler for that, so the client got no response at all.
    """
    _app, client, _payer = stack
    for encoded in ("MTIz", "bnVsbA==", "WzEsMl0="):  # 123, null, [1,2]
        resp = client.get("/", headers={PAYMENT_SIGNATURE_HEADER: encoded})
        assert resp.status_code == 400, f"{encoded!r} -> {resp.status_code}"


def test_b64decode_json_refuses_a_non_object():
    for encoded in ("MTIz", "bnVsbA==", "WzEsMl0=", "ImEi"):  # 123, null, [1,2], "a"
        with pytest.raises(ValueError):
            b64decode_json(encoded)
