"""A malformed payment payload must be REFUSED with a verdict, never crash the request.

`settle()` read the proof as `payload.get("payload").get("paymentProof")` on its
first line, before `_check_requirements` had looked at anything. The check
refuses a non-object `payload`, but on /settle it ran too late: `payload: [1]`
raised AttributeError while reading the proof, and nothing above `do_POST`
catches it, so the connection closed with no verdict. `verify()` ran the check
first and answered.

Neither path checked the object the proof sits in. `payload.payload` was read
as `(payload.get("payload") or {}).get(...)`, so `"x"`, `1` or `[1]` reached
`.get` on a str, int or list and both /verify and /settle dropped the
connection.

Every node here raises if consulted, so a payload refused on its face is also
shown never to reach a node.
"""
from __future__ import annotations

import json
import threading
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
AMOUNT = "1000000000000000000000000000000"


def _fac(tmp_path):
    def call(action, params):  # pragma: no cover - must not run
        raise AssertionError(f"a node was consulted for a malformed payload: {action}")

    return Facilitator(
        FacilitatorConfig(
            endpoints=[
                RpcEndpoint(url="https://stub-a.invalid", call=call),
                RpcEndpoint(url="https://stub-b.invalid", call=call),
            ],
            claim_store=ClaimStore(path=str(tmp_path / "claims.db")),
        )
    )


def _requirements():
    return {
        "scheme": "exact",
        "network": "nano:live",
        "amount": AMOUNT,
        "asset": "XNO",
        "payTo": PAY_TO,
    }


def _payload(inner):
    return {
        "accepted": {"network": "nano:live", "asset": "XNO", "amount": AMOUNT, "payTo": PAY_TO},
        "payload": inner,
    }


NON_OBJECT_PAYLOADS = [[1], "x", 1, True]
NON_OBJECT_INNER = ["x", 1, [1], [PROOF], True]


@pytest.mark.parametrize("payload", NON_OBJECT_PAYLOADS, ids=repr)
def test_settle_refuses_a_non_object_payload_before_reading_the_proof(tmp_path, payload):
    res = _fac(tmp_path).settle(_requirements(), payload)
    assert res["success"] is False
    assert res["errorReason"] == "invalid"
    assert res["transaction"] == ""


@pytest.mark.parametrize("inner", NON_OBJECT_INNER, ids=repr)
def test_verify_refuses_a_non_object_payload_payload(tmp_path, inner):
    res = _fac(tmp_path).verify(_requirements(), _payload(inner))
    assert res["isValid"] is False
    assert res["invalidReason"] == "bad-request"


@pytest.mark.parametrize("inner", NON_OBJECT_INNER, ids=repr)
def test_settle_refuses_a_non_object_payload_payload(tmp_path, inner):
    res = _fac(tmp_path).settle(_requirements(), _payload(inner))
    assert res["success"] is False
    assert res["errorReason"] == "invalid"
    assert res["transaction"] == ""


def test_a_refused_settle_still_reports_the_proof_it_was_given(tmp_path):
    """Moving the read after the check must not lose the proof from the answer."""
    req = _requirements()
    req["scheme"] = "upto"
    res = _fac(tmp_path).settle(req, _payload({"paymentProof": PROOF}))
    assert res["success"] is False
    assert res["transaction"] == PROOF


@pytest.mark.parametrize(
    "body",
    [
        {"payload": [1]},
        {"payload": "x"},
        {"payload": {"payload": "x"}},
        {"payload": {"payload": [1]}},
    ],
    ids=["payload-list", "payload-str", "inner-str", "inner-list"],
)
def test_the_http_surface_answers_a_malformed_payload_with_json(tmp_path, body):
    """The defect as a client sees it: a verdict, not a dropped connection."""
    srv = serve(_fac(tmp_path), host="127.0.0.1", port=0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        payload = body["payload"]
        if isinstance(payload, dict):
            payload = _payload(payload["payload"])
        data = json.dumps({"requirements": _requirements(), "payload": payload}).encode("utf-8")
        for path in ("/verify", "/settle"):
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}{path}",
                data=data,
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


# ---- controls: an absent or empty proof object is still "proof missing" ----


@pytest.mark.parametrize("inner", [None, {}, {"paymentProof": "zz"}], ids=["null", "empty", "not-hex"])
def test_an_absent_or_bad_proof_is_still_proof_missing(tmp_path, inner):
    fac = _fac(tmp_path)
    res = fac.verify(_requirements(), _payload(inner))
    assert res["isValid"] is False
    assert res["invalidReason"] == "proof missing"
    settled = fac.settle(_requirements(), _payload(inner))
    assert settled["success"] is False
    assert settled["errorMessage"] == "bad paymentProof"
