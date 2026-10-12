"""A paid request must settle whatever shape `requirements.extra` arrives in.

`settle()` re-verified the proof on every node and only then read the request
id as `requirements.get("extra", {}).get("requestId")`. The `{}` default covers
a missing key only: `"extra": null` gave None, `"extra": "abc"` a str, and
`.get` on either raised AttributeError after the payment had been confirmed.
Nothing above `do_POST` catches it, so /settle closed the connection with no
verdict and the payer's confirmed send could never be settled. `verify()` never
reads `extra`, so it said isValid true for the same pair.

The nodes here confirm the block, so every case below reaches the claim step.
"""
from __future__ import annotations

import pytest

from nano_mcp.facilitator import (
    ClaimStore,
    Facilitator,
    FacilitatorConfig,
    RpcEndpoint,
    consumption_key,
)

BLOCK = "BA96F62D4EA651A21DA4282809F2541EA42481CA35018129F29B406EF3FE36C0"
PAYER = "nano_3p1zmep1qax1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f9"
PAY_TO = "nano_3t6k35gi95xu6tergt6p69ck76ogmitsa8mnijtpxm9fkcm736xtoncuohr3"
AMOUNT = "1000000000000000000000000000000"

MISSING = object()


def _confirming_fac(tmp_path):
    def call(action, params):
        assert action == "block_info"
        return {
            "hash": BLOCK,
            "confirmed": True,
            "subtype": "send",
            "amount": AMOUNT,
            "account": PAYER,
            "link_as_account": PAY_TO,
        }

    return Facilitator(
        FacilitatorConfig(
            endpoints=[
                RpcEndpoint(url="https://stub-a.invalid", call=call),
                RpcEndpoint(url="https://stub-b.invalid", call=call),
            ],
            claim_store=ClaimStore(path=str(tmp_path / "claims.db")),
        )
    )


def _pair(extra):
    req = {
        "scheme": "exact",
        "network": "nano:live",
        "amount": AMOUNT,
        "asset": "XNO",
        "payTo": PAY_TO,
    }
    if extra is not MISSING:
        req["extra"] = extra
    payload = {
        "accepted": {"network": "nano:live", "asset": "XNO", "amount": AMOUNT, "payTo": PAY_TO},
        "payload": {"paymentProof": BLOCK},
    }
    return req, payload


@pytest.mark.parametrize(
    "extra", [MISSING, None, {}], ids=["extra-absent", "extra-null", "extra-empty-object"]
)
def test_a_paid_request_with_no_request_id_settles_once(tmp_path, extra):
    fac = _confirming_fac(tmp_path)
    req, payload = _pair(extra)
    first = fac.settle(req, payload)
    assert first["success"] is True
    assert first["payer"] == PAYER
    again = fac.settle(req, payload)
    assert again["success"] is False
    assert again["errorReason"] == "duplicate"
    assert fac.claim_store.is_claimed(consumption_key(BLOCK, "")) is True


@pytest.mark.parametrize(
    "extra", ["abc", "", 0, ["requestId"]], ids=["str", "empty-str", "zero", "list"]
)
def test_a_non_object_extra_is_refused_with_a_verdict_and_claims_nothing(tmp_path, extra):
    fac = _confirming_fac(tmp_path)
    req, payload = _pair(extra)
    verdict = fac.verify(req, payload)
    assert verdict["isValid"] is False
    assert verdict["invalidReason"] == "bad-requirements"
    settled = fac.settle(req, payload)
    assert settled["success"] is False
    assert settled["errorReason"] == "invalid"
    assert fac.claim_store.is_claimed(consumption_key(BLOCK, "")) is False


def test_the_request_id_in_an_object_extra_still_binds_the_claim(tmp_path):
    fac = _confirming_fac(tmp_path)
    req, payload = _pair({"requestId": "req_1"})
    assert fac.settle(req, payload)["success"] is True
    assert fac.claim_store.is_claimed(consumption_key(BLOCK, "req_1")) is True
    assert fac.claim_store.is_claimed(consumption_key(BLOCK, "")) is False
