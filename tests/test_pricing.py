"""Block 6 — dollar-priced quotes (L8 exact median XNO amount, L9 30s expiry).

L8: a USD price converts to the exact XNO raw amount via the MEDIAN of three
    independent price sources; the conversion is pure computation (no money is
    held or converted).
L9: a dollar quote expires in 30 seconds or less and verify_payment refuses a
    payment made after the window.
"""
from __future__ import annotations

import time
from decimal import Decimal

import pytest

import nano_mcp.pricing as pricing
from nano_mcp.pricing import (
    DEFAULT_SOURCES,
    QUOTE_TTL_SECONDS,
    exact_xno_amount,
    fetch_median_xno_usd,
    median,
    usd_to_xno_raw,
)
from nano_mcp.service import PaymentService
from nano_mcp.store import ApprovalStore
from nano_sdk.units import RAW_PER_NANO, nano_str


# ---- helpers: fixed-rate sources (offline) ----
def _src(v: str):
    return lambda: Decimal(v)


def _rate(*vals: str):
    return _src(vals[0])() if len(vals) == 1 else None


# ============================= L8 =============================
def test_median_returns_middle_value():
    assert median([Decimal("0.30"), Decimal("0.34"), Decimal("0.35")]) == Decimal("0.34")
    assert median([Decimal("1"), Decimal("3")]) == Decimal("2")


def test_median_requires_at_least_one():
    with pytest.raises(ValueError):
        median([])


def test_fetch_median_tolerates_one_failure():
    good_ones = [_src("0.30"), _src("0.34"), _src("0.35")]
    # failing source index 1 must not break the quote: median of the two
    # healthy (0.30, 0.35) is their mean 0.325.
    three = [good_ones[0], lambda: (_ for _ in ()).throw(RuntimeError("down")), good_ones[2]]
    assert fetch_median_xno_usd(tuple(three)) == Decimal("0.325")


def test_fetch_median_requires_two_healthy():
    two_down = [
        lambda: (_ for _ in ()).throw(RuntimeError("down")),
        lambda: (_ for _ in ()).throw(RuntimeError("down")),
        _src("0.34"),
    ]
    with pytest.raises(RuntimeError):
        fetch_median_xno_usd(tuple(two_down))


def test_usd_to_xno_raw_rounds_up_never_underpays():
    # $1.00 at $0.34/XNO -> 2.941176... XNO -> ceil to a whole raw.
    #
    # `expect` is derived with exact integer arithmetic, NOT from the same
    # Decimal expression the implementation uses. It used to be
    #
    #     int((Decimal("1.00") / Decimal("0.34") * RAW_PER_NANO)
    #         .to_integral_value(rounding="ROUND_CEILING"))
    #
    # which rounds to the active context precision exactly as the old
    # implementation did, so this law restated the bug and passed beside it.
    from fractions import Fraction

    raw = usd_to_xno_raw(Decimal("1.00"), Decimal("0.34"))
    q = Fraction(Decimal("1.00")) * int(RAW_PER_NANO) / Fraction(Decimal("0.34"))
    expect = -((-q.numerator) // q.denominator)
    assert raw == expect
    # the seller receives at least the quoted USD value at that rate -- checked in
    # exact arithmetic, since a rounded Decimal product can satisfy it either way
    assert Fraction(raw) * Fraction(Decimal("0.34")) >= Fraction(Decimal("1.00")) * int(RAW_PER_NANO)


def test_usd_to_xno_raw_rejects_non_positive():
    with pytest.raises(ValueError):
        usd_to_xno_raw(Decimal("0"), Decimal("0.34"))
    with pytest.raises(ValueError):
        usd_to_xno_raw(Decimal("1.0"), Decimal("0"))


def test_exact_xno_amount_uses_fixed_rate_offline():
    # fixed median rate 0.34 USD/XNO for a $1.00 price
    sources = (_src("0.30"), _src("0.34"), _src("0.35"))
    raw, rate = exact_xno_amount("1.00", sources=sources)
    assert rate == Decimal("0.34")
    expect = usd_to_xno_raw(Decimal("1.00"), Decimal("0.34"))
    assert raw == expect
    # exact: amount == price / median rate, rounded up at raw scale.
    # The lower bound is taken in exact arithmetic: written as
    # int(Decimal("1.00") / Decimal("0.34") * RAW_PER_NANO) it is itself rounded
    # to the context precision and can land ABOVE the true amount, which is how
    # it went red against a quote that is now exactly right.
    from fractions import Fraction

    bound = Fraction(Decimal("1.00")) * int(RAW_PER_NANO) / Fraction(Decimal("0.34"))
    assert raw >= bound


def test_quote_usd_returns_exact_median_amount_and_no_money_moves(tmp_path):
    """L8: quote_usd returns the exact XNO amount from the median of three
    injected sources, carrying price_usd, rate and expiry; no balance exists or
    is transferred — verify it is pure computation (no RPC client contact)."""
    store = ApprovalStore(path=str(tmp_path / "s.db"))
    svc = PaymentService(
        master_secret=b"S" * 32,
        store=store,
        rate_source=_src("0.34"),
        clock=lambda: 1000.0,
    )
    q = svc.quote_usd("1.00", request_id="req-a")
    assert q.price_usd == "1.00"
    assert q.rate_xno_usd == "0.34"
    assert q.price_raw == usd_to_xno_raw(Decimal("1.00"), Decimal("0.34"))
    d = q.as_dict()
    assert set(d) >= {"request_id", "address", "price_raw", "price_usd", "rate_xno_usd", "expires_at"}
    # never converts or holds: quote is a computation, not a transfer
    assert d["address"].startswith("nano_")
    import nano_sdk.crypto as crypto
    assert crypto.validate_address(d["address"])


class _ContactingClient:
    """RpcClient stand-in that fails if quote_usd ever talks to the chain —
    proves the quote path performs NO balance change or transfer."""

    def __init__(self):
        self.contacted = False
        self.balance = "1000000000000000000000000000000"

    def account_balance(self, *a, **k):
        self.contacted = True
        return {"balance": self.balance}

    def account_history(self, *a, **k):
        self.contacted = True
        return {"history": []}

    def process(self, *a, **k):
        self.contacted = True
        raise AssertionError("quote_usd must never broadcast a block")

    def work_generate(self, *a, **k):
        self.contacted = True
        raise AssertionError("quote_usd must never generate PoW")


def test_quote_usd_is_pure_computation_no_chain_contact(tmp_path):
    """L8 (hard test): the dollar quote performs NO balance change or transfer —
    a client that records every contact is never touched while quoting."""
    spy = _ContactingClient()
    store = ApprovalStore(path=str(tmp_path / "p.db"))
    svc = PaymentService(
        master_secret=b"S" * 32,
        client=spy,  # type: ignore[arg-type]
        store=store,
        rate_source=_src("0.34"),
        clock=lambda: 1000.0,
    )
    q = svc.quote_usd("1.00", request_id="req-pure")
    assert q.price_raw > 0
    assert not spy.contacted, "quote_usd must not touch the chain (no custody/no conversion)"


# ============================= L9 =============================
def test_quote_expires_in_30_seconds_or_less(tmp_path):
    store = ApprovalStore(path=str(tmp_path / "e.db"))
    svc = PaymentService(
        master_secret=b"S" * 32,
        store=store,
        rate_source=_src("0.34"),
        clock=lambda: 5000.0,
    )
    q = svc.quote_usd("0.50", request_id="req-x")
    assert q.expires_at is not None
    assert q.expires_at - 5000.0 <= QUOTE_TTL_SECONDS
    assert q.expires_at - 5000.0 <= 30.0
    assert not q.expired(now=5000.0)
    assert q.expired(now=q.expires_at + 0.001)  # type: ignore[operator]


def test_verify_refuses_expired_quote__does_not_approve(tmp_path):
    """L9: a dollar quote paid after its window returns status='expired' and is
    never approved."""
    store = ApprovalStore(path=str(tmp_path / "x.db"))
    svc = PaymentService(
        master_secret=b"S" * 32,
        store=store,
        rate_source=_src("0.34"),
        clock=lambda: 1000.0,
    )
    q = svc.quote_usd("1.00", request_id="req-exp")
    # advance the clock past the expiry
    svc.clock = lambda: q.expires_at + 60.0
    out = svc.verify_payment("req-exp", q.price_raw, require_onchain=False)
    assert out["status"] == "expired"
    # not approved, so a re-check does not turn spent
    assert not store.is_approved("req-exp")


def test_verify_accepts_quote_within_window(tmp_path):
    store = ApprovalStore(path=str(tmp_path / "y.db"))
    svc = PaymentService(
        master_secret=b"S" * 32,
        store=store,
        rate_source=_src("0.34"),
        clock=lambda: 1000.0,
    )
    q = svc.quote_usd("1.00", request_id="req-ok")
    out = svc.verify_payment("req-ok", q.price_raw, require_onchain=False)
    assert out["status"] == "approved"
    assert store.is_approved("req-ok")


# ===================== live network tests (3 sources) =====================
@pytest.mark.network
def test_live_median_of_three_sources_returns_numeric():
    """Live: three independent public sources each return a numeric USD/XNO rate
    and the median is between the observed min and max."""
    rates = []
    for src in DEFAULT_SOURCES:
        rates.append(Decimal(str(src())))
    assert len(rates) == 3
    for r in rates:
        assert r > 0
    m = median(rates)
    assert min(rates) <= m <= max(rates)
    # sanity: a sane XNO price in USD
    assert Decimal("0.005") < m < Decimal("100")

def test_the_quoted_amount_does_not_depend_on_the_process_decimal_precision():
    """`usd_to_xno_raw` divided and multiplied in Decimal, and both round to
    `decimal.getcontext().prec` -- a process-global nothing in this codebase
    sets, which any other library in the same process may change. A raw XNO
    amount carries 31 significant digits; the default context keeps 28.

    So the same price at the same rate produced different amounts:

        $1.00 at 0.34 USD/XNO, prec=28 -> 2941176470588235294117647059000
        $1.00 at 0.34 USD/XNO, prec=50 -> 2941176470588235294117647058824

    For an `exact`-scheme payment the amount IS the contract: a buyer and a
    seller who agree on the price and the rate can still disagree on what must
    be paid, and the facilitator refuses the difference. This is the same fault
    nano_sdk/units.py documents having fixed for balances, one module over.
    """
    from decimal import localcontext

    cases = [("1.00", "0.34"), ("0.01", "0.7331"), ("1", "3"), ("0.25", "1.07")]
    for price, rate in cases:
        answers = set()
        for prec in (20, 28, 34, 50, 80):
            with localcontext() as ctx:
                ctx.prec = prec
                answers.add(usd_to_xno_raw(Decimal(price), Decimal(rate)))
        assert len(answers) == 1, (
            f"${price} at {rate} USD/XNO quotes {sorted(answers)} depending on "
            f"decimal.getcontext().prec"
        )


def test_the_quoted_amount_is_the_exact_ceiling_so_the_seller_never_under_receives():
    """The docstring promises "no precision is ever lost" and "rounded UP
    (ceiling) ... so the seller never under-receives". At the default precision
    the multiplication rounded to 28 significant digits BEFORE the ceiling was
    applied, so the ceiling had nothing left to round up and the result came out
    below the true one:

        $1 at 3 USD/XNO   exact 333333333333333333333333333334
                          got   333333333333333333333333333300   (34 raw short)

    The expected value here is derived with exact integer arithmetic through
    Fraction, never from the implementation's own expression -- which is why
    `test_usd_to_xno_raw_rounds_up_never_underpays` passed while the fault was
    live: it built its `expect` from the same rounded formula.
    """
    from fractions import Fraction

    def exact_ceiling(price: str, rate: str) -> int:
        q = Fraction(Decimal(price)) * 10**30 / Fraction(Decimal(rate))
        return -((-q.numerator) // q.denominator)

    for price, rate in [("1", "3"), ("1", "7"), ("1.00", "0.34"), ("0.01", "0.7331"),
                        ("0.25", "1.07"), ("2.50", "0.9999"), ("0.000001", "1.23456789")]:
        want = exact_ceiling(price, rate)
        got = usd_to_xno_raw(Decimal(price), Decimal(rate))
        assert got == want, f"${price} at {rate}: quoted {got}, exact ceiling {want}"
        # and the promise itself, checked in exact arithmetic rather than Decimal
        assert Fraction(got) * Fraction(Decimal(rate)) >= Fraction(Decimal(price)) * 10**30


def test_a_non_finite_price_or_rate_is_refused_by_name():
    """A rate source that answers NaN must be told apart from a bug here.

    Before the guard, NaN reached `<= 0` and raised `decimal.InvalidOperation`
    with an EMPTY message, and an infinity reached `Fraction()` and raised
    `OverflowError: cannot convert Infinity to integer ratio`. Neither produced a
    wrong amount - this function has always failed closed - but a caller reading
    either one cannot tell which input was at fault, or whether the fault is in
    the price, the rate, or the conversion itself.
    """
    for price, rate in (
        (Decimal("1.00"), Decimal("NaN")),
        (Decimal("NaN"), Decimal("0.33")),
        (Decimal("1.00"), Decimal("Infinity")),
        (Decimal("Infinity"), Decimal("0.33")),
        (Decimal("1.00"), Decimal("-Infinity")),
    ):
        with pytest.raises(ValueError, match="finite"):
            usd_to_xno_raw(price, rate)


def test_the_positivity_refusals_still_hold_and_still_name_their_argument():
    """The guard above must not have shadowed the two refusals after it."""
    with pytest.raises(ValueError, match="price_usd must be positive"):
        usd_to_xno_raw(Decimal("0"), Decimal("0.33"))
    with pytest.raises(ValueError, match="price_usd must be positive"):
        usd_to_xno_raw(Decimal("-1"), Decimal("0.33"))
    with pytest.raises(ValueError, match="rate_xno_usd must be positive"):
        usd_to_xno_raw(Decimal("1.00"), Decimal("0"))
    with pytest.raises(ValueError, match="rate_xno_usd must be positive"):
        usd_to_xno_raw(Decimal("1.00"), Decimal("-0.33"))


def test_the_amount_is_unchanged_for_every_finite_input():
    """The guard is refusal-only: no finite price or rate answers differently.

    Checked against the exact rational ceiling rather than against the previous
    implementation, so this is a statement about the number being right and not
    merely about it being the same.
    """
    import math
    import random
    from fractions import Fraction as F

    random.seed(11)
    for _ in range(5000):
        price = Decimal(str(random.randint(1, 10**6))).scaleb(-random.randint(0, 6))
        rate = Decimal(str(random.randint(1, 10**9))).scaleb(-random.randint(0, 9))
        assert usd_to_xno_raw(price, rate) == math.ceil(F(price) * 10**30 / F(rate))
