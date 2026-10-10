"""A malformed `requirements` must be REFUSED with a verdict, never crash the request.

`Facilitator._check_requirements` compared `requirements` to the payload's
`accepted` block and enforced nothing else, although `REQUIRED_SECTIONS` had
listed the mandatory fields all along. A field absent from *both* halves is
`None == None`, so it passed; `verify`/`settle` then indexed
`requirements["payTo"]` and `requirements["amount"]` directly, and
`verify_block_on_independent_endpoints` called `parse_raw(amount)` outside any
`try`. The resulting `KeyError`/`ValueError` is caught nowhere above
`make_handler`'s `do_POST`, so a POST to /verify or /settle was answered with a
closed connection and no body at all - indistinguishable, to a paying agent,
from the facilitator being down.

These laws drive the real `Facilitator` and the real HTTP surface. Every node is
a stub that raises if consulted, so a request that should be refused on its face
is also shown never to reach a node.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from nano_mcp.facilitator import (
    ClaimStore,
    Facilitator,
    FacilitatorConfig,
    RpcEndpoint,
    serve,
)

PROOF = "A" * 64
PAY_TO = "nano_1payto11111111111111111111111111111111111111111111111111111hc"


def _exploding_endpoints():
    """Two endpoints that fail the test if the facilitator consults them."""

    def call(action, params):  # pragma: no cover - must not run
        raise AssertionError(f"a node was consulted for a malformed request: {action}")

    return [
        RpcEndpoint(url="https://stub-a.invalid", call=call),
        RpcEndpoint(url="https://stub-b.invalid", call=call),
    ]


def _fac(tmp_path):
    return Facilitator(
        FacilitatorConfig(
            endpoints=_exploding_endpoints(),
            claim_store=ClaimStore(path=str(tmp_path / "claims.db")),
        )
    )


def _pair(**overrides):
    """A requirements/payload pair that is consistent across both halves."""
    req = {
        "scheme": "exact",
        "network": "nano:live",
        "asset": "XNO",
        "amount": "1000000000000000000000000",
        "payTo": PAY_TO,
    }
    req.update(overrides)
    for key in list(req):
        if req[key] is None:
            del req[key]
    accepted = {
        "network": req.get("network"),
        "asset": req.get("asset"),
    }
    if "amount" in req:
        accepted["amount"] = req["amount"]
    if "payTo" in req:
        accepted["payTo"] = req["payTo"]
    return req, {"accepted": accepted, "payload": {"paymentProof": PROOF}}


MALFORMED = [
    pytest.param({"payTo": None}, id="payTo-absent-from-both-halves"),
    pytest.param({"amount": None}, id="amount-absent-from-both-halves"),
    pytest.param({"amount": "abc"}, id="amount-not-a-number"),
    pytest.param({"amount": "1e30"}, id="amount-in-exponent-form"),
    pytest.param({"amount": ""}, id="amount-empty-string"),
    pytest.param({"amount": "-1"}, id="amount-negative"),
    pytest.param({"amount": "0"}, id="amount-zero"),
    pytest.param({"payTo": "   "}, id="payTo-blank"),
]


@pytest.mark.parametrize("overrides", MALFORMED)
def test_verify_returns_a_refusal_instead_of_raising(tmp_path, overrides):
    req, payload = _pair(**overrides)
    res = _fac(tmp_path).verify(req, payload)
    assert res["isValid"] is False
    assert res["invalidReason"] in ("bad-requirements", "bad-amount", "bad-payto")


@pytest.mark.parametrize("overrides", MALFORMED)
def test_settle_returns_a_refusal_instead_of_raising(tmp_path, overrides):
    req, payload = _pair(**overrides)
    res = _fac(tmp_path).settle(req, payload)
    assert res["success"] is False
    assert res["errorReason"] == "invalid"
    assert res["transaction"] == PROOF


def test_a_non_object_requirements_block_is_refused(tmp_path):
    fac = _fac(tmp_path)
    assert fac.verify([], {"accepted": {}, "payload": {}})["isValid"] is False
    assert fac.verify({"scheme": "exact"}, [])["isValid"] is False
    _, payload = _pair()
    payload["accepted"] = ["not", "an", "object"]
    assert fac.verify(_pair()[0], payload)["isValid"] is False


def test_the_http_surface_answers_a_malformed_request_with_json(tmp_path):
    """The defect as a client sees it: a verdict, not a dropped connection.

    `_read_json` in this twin already refuses a body that is not a JSON object
    and answers 400. That guard is upstream of this one and does not help here:
    the body below IS a well-formed object, and the crash was in
    `_check_requirements`' callers reading fields off it.
    """
    fac = _fac(tmp_path)
    srv = serve(fac, host="127.0.0.1", port=8138)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        req, payload = _pair(payTo=None)
        body = json.dumps({"requirements": req, "payload": payload}).encode("utf-8")
        for path in ("/verify", "/settle"):
            request = urllib.request.Request(
                f"http://127.0.0.1:8138{path}",
                data=body,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=10) as resp:
                answer = json.loads(resp.read().decode("utf-8"))
            assert resp.status == 200
            if path == "/verify":
                assert answer["isValid"] is False
            else:
                assert answer["success"] is False
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.mark.parametrize(
    "amount",
    ["0.000001", "1", "1000000000000000000000000000000"],
    ids=["decimal-xno", "one-raw", "one-whole-xno"],
)
def test_a_well_formed_amount_still_reaches_the_nodes(tmp_path, amount):
    """The guard must not narrow what `parse_raw` already reads.

    `parse_raw` accepts a decimal XNO string as well as integer raw, so those
    amounts have to pass the requirements check and go on to be looked up on the
    nodes. `verify_block_on_independent_endpoints` catches every endpoint
    exception itself, so a stub that raises proves nothing; these endpoints
    record the calls instead. Reaching them - and being refused as
    `unconfirmed`, not as a bad requirement - is the control that must hold
    whether or not the fix is present.
    """
    consulted: list[str] = []

    def call(action, params):
        consulted.append(params.get("hash", ""))
        raise RuntimeError("stub node has no blocks")

    fac = Facilitator(
        FacilitatorConfig(
            endpoints=[
                RpcEndpoint(url="https://stub-a.invalid", call=call),
                RpcEndpoint(url="https://stub-b.invalid", call=call),
            ],
            claim_store=ClaimStore(path=str(tmp_path / "claims.db")),
        )
    )
    req, payload = _pair(amount=amount)
    res = fac.verify(req, payload)
    assert res["isValid"] is False
    assert res["invalidReason"] == "unconfirmed"
    assert consulted == [PROOF, PROOF]
