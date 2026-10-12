"""A requirement's amount must be a plain decimal string, or it is refused.

`_check_requirements` gated the amount on `parse_raw`, which hands its parts to
`int()`. `int()` accepts a sign, `_` separators and any Unicode decimal digit,
so `parse_raw("-0.5")` is `int("-0") * 10**30 + 5 * 10**29` = +0.5 XNO: a
requirement for amount "-0.5" verified isValid against a 0.5 XNO send. "+1",
"1_0", "1." and Arabic-Indic digits read as amounts as well, and a JSON number
1.0 reached `parse_raw` through `str()` as "1.0".

The nodes here confirm a send of exactly what each case would parse to, so the
only thing that can refuse it is the requirements check.
"""
from __future__ import annotations

import sys
from decimal import Decimal

import pytest

from nano_mcp.facilitator import (
    ClaimStore,
    Facilitator,
    FacilitatorConfig,
    RpcEndpoint,
    parse_raw,
    verify_block_on_independent_endpoints,
)

BLOCK = "BA96F62D4EA651A21DA4282809F2541EA42481CA35018129F29B406EF3FE36C0"
PAYER = "nano_3p1zmep1qax1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f9"
PAY_TO = "nano_3t6k35gi95xu6tergt6p69ck76ogmitsa8mnijtpxm9fkcm736xtoncuohr3"


def _endpoints(sent_raw: int):
    def call(action, params):
        assert action == "block_info"
        return {
            "hash": BLOCK,
            "confirmed": True,
            "subtype": "send",
            "amount": str(sent_raw),
            "account": PAYER,
            "link_as_account": PAY_TO,
        }

    return [
        RpcEndpoint(url="https://stub-a.invalid", call=call),
        RpcEndpoint(url="https://stub-b.invalid", call=call),
    ]


def _fac(tmp_path, sent_raw: int):
    return Facilitator(
        FacilitatorConfig(
            endpoints=_endpoints(sent_raw),
            claim_store=ClaimStore(path=str(tmp_path / "claims.db")),
        )
    )


def _pair(amount):
    req = {
        "scheme": "exact",
        "network": "nano:live",
        "amount": amount,
        "asset": "XNO",
        "payTo": PAY_TO,
    }
    payload = {
        "accepted": {"network": "nano:live", "asset": "XNO", "amount": amount, "payTo": PAY_TO},
        "payload": {"paymentProof": BLOCK},
    }
    return req, payload


REFUSED = [
    pytest.param("-0.5", 5 * 10**29, id="negative-decimal"),
    pytest.param("+1", 1, id="plus-sign"),
    pytest.param("1_0", 10, id="underscore-separator"),
    pytest.param("١", 1, id="arabic-indic-digit"),
    pytest.param("1.", 10**30, id="trailing-dot"),
    pytest.param(1.0, 10**30, id="json-float"),
    pytest.param(" 1", 1, id="leading-space"),
    pytest.param("1\n", 1, id="trailing-newline"),
]


@pytest.mark.parametrize("amount,sent_raw", REFUSED)
def test_a_non_plain_amount_is_refused_even_when_a_matching_send_is_confirmed(
    tmp_path, amount, sent_raw
):
    # Precondition: the lenient parser does read this as the confirmed send.
    assert parse_raw(str(amount)) == sent_raw
    fac = _fac(tmp_path, sent_raw)
    req, payload = _pair(amount)
    verdict = fac.verify(req, payload)
    assert verdict["isValid"] is False
    assert verdict["invalidReason"] == "bad-amount"
    settled = fac.settle(req, payload)
    assert settled["success"] is False


@pytest.mark.parametrize("amount,sent_raw", REFUSED)
def test_the_exported_verifier_refuses_a_non_plain_amount(amount, sent_raw):
    res = verify_block_on_independent_endpoints(_endpoints(sent_raw), BLOCK, PAY_TO, amount)
    assert res.ok is False
    assert res.consulted == 0


@pytest.mark.parametrize(
    "amount,sent_raw",
    [
        ("1", 1),
        ("1000000000000000000000000000000", 10**30),
        ("0.5", 5 * 10**29),
        ("0.000001", 10**24),
        ("1." + "0" * 29 + "1", 10**30 + 1),
    ],
    ids=["one-raw", "one-xno-raw", "half-xno-decimal", "micro-xno-decimal", "thirty-digit-fraction"],
)
def test_a_plain_amount_still_verifies_and_settles(tmp_path, amount, sent_raw):
    fac = _fac(tmp_path, sent_raw)
    req, payload = _pair(amount)
    assert fac.verify(req, payload)["isValid"] is True
    assert fac.settle(req, payload)["success"] is True


def test_zero_is_still_refused_as_before(tmp_path):
    fac = _fac(tmp_path, 0)
    req, payload = _pair("0")
    verdict = fac.verify(req, payload)
    assert verdict["isValid"] is False
    assert verdict["invalidReason"] == "bad-amount"


# An int amount is raw and verified on main (a Python caller passing 1000, a
# JSON number posted to /verify, the exported verifier called with 10**30); it
# is read exactly like str(int). bool is an int subclass and is not an amount.
@pytest.mark.parametrize("amount", [1000, 10**30], ids=["int-1000", "int-one-xno"])
def test_an_int_amount_is_raw_and_still_verifies_and_settles(tmp_path, amount):
    fac = _fac(tmp_path, amount)
    req, payload = _pair(amount)
    assert fac.verify(req, payload)["isValid"] is True
    assert fac.settle(req, payload)["success"] is True


@pytest.mark.parametrize("amount", [1000, 10**30], ids=["int-1000", "int-one-xno"])
def test_the_exported_verifier_reads_an_int_amount_as_raw(amount):
    res = verify_block_on_independent_endpoints(_endpoints(amount), BLOCK, PAY_TO, amount)
    assert res.ok is True
    assert res.confirmed_on == 2


NON_STRING_REFUSED = [
    pytest.param(True, 1, id="bool"),
    pytest.param(1.0, 10**30, id="float"),
    pytest.param(Decimal("0.5"), 5 * 10**29, id="decimal"),
    pytest.param(-1000, 0, id="negative-int"),
]


@pytest.mark.parametrize("amount,sent_raw", NON_STRING_REFUSED)
def test_a_bool_float_decimal_or_negative_int_amount_is_refused(tmp_path, amount, sent_raw):
    fac = _fac(tmp_path, sent_raw)
    req, payload = _pair(amount)
    verdict = fac.verify(req, payload)
    assert verdict["isValid"] is False
    assert verdict["invalidReason"] == "bad-amount"
    assert fac.settle(req, payload)["success"] is False
    res = verify_block_on_independent_endpoints(_endpoints(sent_raw), BLOCK, PAY_TO, amount)
    assert res.ok is False
    assert res.consulted == 0


@pytest.mark.parametrize("amount", ["0", "0.0", "0" * 31], ids=["zero", "zero-decimal", "zeros"])
def test_the_exported_verifier_refuses_a_zero_amount_against_a_zero_send(amount):
    # A 0-raw send matches a 0 amount on every node; that is not a payment.
    res = verify_block_on_independent_endpoints(_endpoints(0), BLOCK, PAY_TO, amount)
    assert res.ok is False
    assert res.consulted == 0
    assert "positive" in res.reason


@pytest.mark.parametrize("amount", ["1" * 5000, 10**5000], ids=["long-string", "huge-int"])
def test_an_amount_past_the_int_digit_limit_is_refused_not_raised(tmp_path, amount):
    # "1" * 5000 matches the digit pattern, then int() refuses it (digit limit)
    # with ValueError, which escaped the exported verifier.
    # (A Python without the limit - before 3.10.7 - reads it as a real amount,
    # which the 1-raw send then fails to match.)
    res = verify_block_on_independent_endpoints(_endpoints(1), BLOCK, PAY_TO, amount)
    assert res.ok is False
    if hasattr(sys, "get_int_max_str_digits"):
        assert res.consulted == 0
        assert len(res.reason) < 200
