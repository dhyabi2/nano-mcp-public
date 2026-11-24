"""Every endpoint has to agree on who paid, and a payer none of them names is refused.

`verify_block_on_independent_endpoints` asks >=2 independent RPCs for the same
block and refuses unless each one confirms a send of exactly the required
amount to the required payee (strategy law L1: no single node is trusted). The
payer escaped that: it was read off `confirmed_sends[0]` - whichever endpoint
was consulted first - and never compared against the others, and a node that
omitted `block_account` altogether produced `"payer": ""` on an isValid
verdict rather than a refusal.

Both matter because the payer is published as verified fact. `/verify` and
`/settle` answer it (`facilitator.py`'s own docstring lists `payer` in each),
a resource server binding entitlement or a refund to the sender reads it from
there, and `evidence.append_nano_tx` writes it into the journal
`scorecard.count_external_receipts` counts external receipts from - where a
blank payer is in no `NANO_AGENT_OWN_ACCOUNTS` set and so counts as an
outside agent paying us.

The remedy is the one the empty payee three lines above already gets: a sender
this code cannot read, or cannot get two nodes to agree on, is a sender it
cannot vouch for, so it refuses. Nothing else moves - the amount, the payee,
the proof and the claim key are untouched, and a block every endpoint agrees
on verifies exactly as before.
"""
from __future__ import annotations

import os

import pytest

from nano_mcp.facilitator import (
    ClaimStore,
    Facilitator,
    FacilitatorConfig,
    RpcEndpoint,
    verify_block_on_independent_endpoints,
)

BLOCK = "ab" * 32
PAY_TO = "nano_3t6k35gi95xu6tergt6p69ck76ogmitsa8mnijtpxm9fkcm736xtoncuohr3"
AMOUNT = "1000000000000000000000000000000"
PAYER = "nano_1aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
STRANGER = "nano_1bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


class Node:
    """A node that confirms the block, naming `payer` as its sender.

    `payer=None` omits `block_account` the way a node that does not report it
    would, which is what `NormalizedBlock.account` reads as "".
    """

    def __init__(self, payer: str | None):
        self.payer = payer
        self.asked: list[str] = []

    def call(self, action, params):
        assert action == "block_info"
        self.asked.append(params["hash"])
        reply = {
            "hash": BLOCK.upper(),
            "confirmed": True,
            "subtype": "send",
            "amount": AMOUNT,
            "link_as_account": PAY_TO,
        }
        if self.payer is not None:
            reply["block_account"] = self.payer
        return reply


def _endpoints(*payers):
    nodes = [Node(p) for p in payers]
    return [
        RpcEndpoint(url=f"https://stub-{i}.invalid", call=n.call)
        for i, n in enumerate(nodes)
    ], nodes


def _fac(tmp_path, *payers):
    endpoints, nodes = _endpoints(*payers)
    return Facilitator(
        FacilitatorConfig(
            endpoints=endpoints,
            claim_store=ClaimStore(path=str(tmp_path / "claims.db")),
        )
    ), nodes


def _pair(request_id="req_payer"):
    req = {
        "scheme": "exact",
        "network": "nano:live",
        "amount": AMOUNT,
        "asset": "XNO",
        "payTo": PAY_TO,
        "extra": {"requestId": request_id},
    }
    payload = {
        "accepted": {
            "network": "nano:live",
            "asset": "XNO",
            "amount": AMOUNT,
            "payTo": PAY_TO,
        },
        "payload": {"paymentProof": BLOCK},
    }
    return req, payload


# ------------------------------------------------- the endpoints must agree


def test_two_endpoints_naming_different_payers_refuse():
    endpoints, _ = _endpoints(PAYER, STRANGER)
    res = verify_block_on_independent_endpoints(endpoints, BLOCK, PAY_TO, AMOUNT)
    assert res.ok is False, (
        "two independent nodes named different senders for one block and the "
        f"verification passed, reporting {res.confirmed_sends[0]['payer']!r} on "
        "one node's word alone"
    )
    assert "disagree about who paid" in (res.reason or ""), res.reason


def test_verify_refuses_when_the_endpoints_disagree_about_the_payer(tmp_path):
    fac, _ = _fac(tmp_path, PAYER, STRANGER)
    verdict = fac.verify(*_pair())
    assert verdict["isValid"] is False, (
        f"verify published payer={verdict.get('payer')!r} as verified while the "
        "two endpoints it consulted disagreed about who sent the block"
    )
    assert verdict["invalidReason"] == "unconfirmed"
    assert "payer" not in verdict


def test_settle_refuses_when_the_endpoints_disagree_about_the_payer(tmp_path):
    fac, _ = _fac(tmp_path, PAYER, STRANGER)
    settled = fac.settle(*_pair())
    assert settled["success"] is False, (
        f"settle receipted payer={settled.get('payer')!r} on one node's word"
    )
    assert settled["errorReason"] == "unconfirmed"
    assert "payer" not in settled


def test_a_disagreement_does_not_spend_the_claim(tmp_path):
    """A refused settle must leave the proof unclaimed, or the payer who really
    did send it can never settle."""
    fac, _ = _fac(tmp_path, PAYER, STRANGER)
    assert fac.settle(*_pair())["success"] is False
    from nano_mcp.facilitator import consumption_key

    assert fac.claim_store.is_claimed(consumption_key(BLOCK, "req_payer")) is False


@pytest.mark.parametrize("order", [(PAYER, STRANGER), (STRANGER, PAYER)])
def test_the_disagreement_is_refused_whichever_node_is_asked_first(tmp_path, order):
    fac, _ = _fac(tmp_path, *order)
    assert fac.verify(*_pair())["isValid"] is False


def test_three_endpoints_refuse_when_only_the_last_disagrees(tmp_path):
    fac, _ = _fac(tmp_path, PAYER, PAYER, STRANGER)
    verdict = fac.verify(*_pair())
    assert verdict["isValid"] is False
    assert verdict["extra"]["consulted"] == 3


# -------------------------------------------------- a payer nobody can read


def test_a_node_that_omits_the_payer_refuses():
    endpoints, _ = _endpoints(None, None)
    res = verify_block_on_independent_endpoints(endpoints, BLOCK, PAY_TO, AMOUNT)
    assert res.ok is False, (
        "a block_info reply with no block_account verified, and the payer it "
        "published was the empty string"
    )
    assert "no payer account" in (res.reason or ""), res.reason


def test_verify_refuses_a_blank_payer_rather_than_publishing_one(tmp_path):
    fac, _ = _fac(tmp_path, None, None)
    verdict = fac.verify(*_pair())
    assert verdict["isValid"] is False, (
        f"verify answered isValid with payer={verdict.get('payer')!r}; a blank "
        "payer is in no NANO_AGENT_OWN_ACCOUNTS set, so evidence.should_log "
        "counts it as an outside agent having paid us"
    )
    assert verdict["invalidReason"] == "unconfirmed"


def test_one_endpoint_omitting_the_payer_refuses_too(tmp_path):
    fac, _ = _fac(tmp_path, PAYER, None)
    assert fac.verify(*_pair())["isValid"] is False


def test_a_blank_payer_would_be_journalled_as_an_external_receipt():
    """Why the blank matters rather than just reading badly."""
    from nano_mcp.evidence import should_log

    assert should_log("", {PAYER}) is True


# ----------------------------------------------------------- the control


def test_an_agreed_payer_still_verifies_and_settles(tmp_path):
    """The whole point: a block every endpoint reads the same way is unaffected."""
    fac, nodes = _fac(tmp_path, PAYER, PAYER, PAYER)
    verdict = fac.verify(*_pair())
    assert verdict["isValid"] is True, verdict
    assert verdict["payer"] == PAYER
    assert verdict["extra"]["confirmedOn"] == 3
    settled = fac.settle(*_pair())
    assert settled["success"] is True, settled
    assert settled["payer"] == PAYER
    assert settled["amount"] == AMOUNT
    for node in nodes:
        # once for the verify above, once for the settle: settle re-verifies
        # on-chain rather than trusting the earlier verdict.
        assert node.asked == [BLOCK, BLOCK]
    # and still exactly once
    assert fac.settle(*_pair())["errorReason"] == "duplicate"


def test_the_payer_is_still_read_from_a_real_nodes_account_field(tmp_path):
    """`account` (the spec/stub spelling) works as well as `block_account`."""

    class SpecNode:
        def call(self, action, params):
            return {
                "hash": BLOCK.upper(),
                "confirmed": True,
                "subtype": "send",
                "amount": AMOUNT,
                "account": PAYER,
                "link_as_account": PAY_TO,
            }

    nodes = [SpecNode(), SpecNode()]
    endpoints = [
        RpcEndpoint(url=f"https://spec-{i}.invalid", call=n.call)
        for i, n in enumerate(nodes)
    ]
    res = verify_block_on_independent_endpoints(endpoints, BLOCK, PAY_TO, AMOUNT)
    assert res.ok is True, res.reason
    assert res.confirmed_sends[0]["payer"] == PAYER
