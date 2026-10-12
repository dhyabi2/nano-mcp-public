"""One payment settles once, whatever hex case its block hash is spelt in.

A Nano node looks a block hash up case-insensitively: `ab..`, `AB..` and `Ab..`
all name the same confirmed send. The facilitator accepted either case and
built the single-use claim key from the proof exactly as sent, so each case
spelling got its own key and one payment settled once per spelling for the same
requestId. The proof check also used `re.match` with `^...$`, and `$` matches
before a trailing "\\n", so `<hash>\\n` was one more distinct key.

The nodes here look hashes up case-insensitively, the way a real node does, and
record every hash they are asked for.
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
from nano_mcp.httpx402 import derive_requirements
from nano_mcp.paidtool import PaidToolServer

BLOCK = "ab" * 32
PAYER = "nano_3p1zmep1qax1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f9"
PAY_TO = "nano_3t6k35gi95xu6tergt6p69ck76ogmitsa8mnijtpxm9fkcm736xtoncuohr3"
AMOUNT = "1000000000000000000000000000000"
SPELLINGS = [BLOCK.lower(), BLOCK.upper(), "Ab" * 32]


class CaseInsensitiveNode:
    def __init__(self, pay_to: str):
        self.pay_to = pay_to
        self.asked: list[str] = []

    def call(self, action, params):
        assert action == "block_info"
        h = params["hash"]
        self.asked.append(h)
        # Lenient on case and surrounding whitespace, so the facilitator's own
        # check is what has to refuse a malformed proof.
        if h.strip().upper() != BLOCK.upper():
            raise RuntimeError(f"unknown block {h!r}")
        return {
            "hash": BLOCK.upper(),
            "confirmed": True,
            "subtype": "send",
            "amount": AMOUNT,
            "account": PAYER,
            "link_as_account": self.pay_to,
        }


def _endpoints(pay_to):
    a, b = CaseInsensitiveNode(pay_to), CaseInsensitiveNode(pay_to)
    return [
        RpcEndpoint(url="https://stub-a.invalid", call=a.call),
        RpcEndpoint(url="https://stub-b.invalid", call=b.call),
    ], (a, b)


def _fac(tmp_path):
    endpoints, nodes = _endpoints(PAY_TO)
    fac = Facilitator(
        FacilitatorConfig(
            endpoints=endpoints,
            claim_store=ClaimStore(path=str(tmp_path / "claims.db")),
        )
    )
    return fac, nodes


def _pair(proof, request_id="req_case"):
    req = {
        "scheme": "exact",
        "network": "nano:live",
        "amount": AMOUNT,
        "asset": "XNO",
        "payTo": PAY_TO,
        "extra": {"requestId": request_id},
    }
    payload = {
        "accepted": {"network": "nano:live", "asset": "XNO", "amount": AMOUNT, "payTo": PAY_TO},
        "payload": {"paymentProof": proof},
    }
    return req, payload


def test_one_payment_settles_once_across_hex_case_spellings(tmp_path):
    fac, _ = _fac(tmp_path)
    results = [fac.settle(*_pair(p)) for p in SPELLINGS]
    assert [r["success"] for r in results] == [True, False, False]
    assert [r.get("errorReason") for r in results[1:]] == ["duplicate", "duplicate"]
    assert fac.claim_store.is_claimed(consumption_key(BLOCK.upper(), "req_case")) is True


def test_settle_asks_the_node_for_the_uppercase_hash(tmp_path):
    fac, nodes = _fac(tmp_path)
    assert fac.settle(*_pair("Ab" * 32))["success"] is True
    for node in nodes:
        assert node.asked == [BLOCK.upper()]


def test_verify_asks_the_node_for_the_uppercase_hash(tmp_path):
    fac, nodes = _fac(tmp_path)
    assert fac.verify(*_pair(BLOCK.lower()))["isValid"] is True
    for node in nodes:
        assert node.asked == [BLOCK.upper()]


@pytest.mark.parametrize("suffix", ["\n", " ", "\r\n"], ids=["lf", "space", "crlf"])
def test_a_proof_with_trailing_whitespace_is_malformed(tmp_path, suffix):
    fac, nodes = _fac(tmp_path)
    verdict = fac.verify(*_pair(BLOCK + suffix))
    assert verdict["isValid"] is False
    assert verdict["invalidReason"] == "proof missing"
    settled = fac.settle(*_pair(BLOCK + suffix))
    assert settled["success"] is False
    assert settled["errorReason"] == "invalid"
    for node in nodes:
        assert node.asked == []


def test_a_trailing_newline_does_not_open_a_second_claim(tmp_path):
    fac, _ = _fac(tmp_path)
    assert fac.settle(*_pair(BLOCK))["success"] is True
    again = fac.settle(*_pair(BLOCK + "\n"))
    assert again["success"] is False


def test_paid_tool_serves_once_across_hex_case_spellings(tmp_path):
    master = bytes(range(32))
    request_id = "req_tool_case"
    pay_to = derive_requirements(master, request_id, AMOUNT)["payTo"]
    endpoints, _ = _endpoints(pay_to)
    paid = PaidToolServer(
        master_secret=master,
        facilitator=Facilitator(
            FacilitatorConfig(
                endpoints=endpoints,
                claim_store=ClaimStore(path=str(tmp_path / "claims.db")),
            )
        ),
        amount_raw=AMOUNT,
        tools={"premium_data": lambda resource: {"data": "secret"}},
    )
    results = [paid.execute("premium_data", request_id, p) for p in SPELLINGS]
    assert [r["success"] for r in results] == [True, False, False]
    assert [r["error_reason"] for r in results[1:]] == ["duplicate", "duplicate"]
