"""Nano units: raw <-> nano.

1 nano = 10**30 raw. All RPC actions deal in integer raw amounts; floating point is never
used for money so no precision is ever lost.

Decimal *arithmetic* is rounded to the active context precision, which defaults to 28
significant digits -- fewer than the 31 a whole-XNO raw balance carries. So these
conversions are done by rescaling the exponent (Decimal's constructor and int() are exact
and context-free) rather than by multiplying or dividing by 10**30.
"""
from decimal import Decimal

RAW_PER_NANO = Decimal("1000000000000000000000000000000")  # 10**30
RAW_EXPONENT = 30  # 1 nano = 10**RAW_EXPONENT raw


def _rescale(d: Decimal, shift: int) -> Decimal:
    """d * 10**shift, exactly: rebuild the Decimal with a moved exponent."""
    sign, digits, exponent = d.as_tuple()
    return Decimal((sign, digits, exponent + shift))


def nano_to_raw(amount: str | Decimal | int) -> int:
    """Convert a nano amount (string or Decimal) to integer raw (10**30 scale).

    Raises ValueError if the amount has more than 30 decimal places or is negative.
    """
    d = Decimal(str(amount))
    if not d.is_finite():
        raise ValueError("amount must be a finite number")
    if d < 0:
        raise ValueError("amount must be positive")
    scaled = _rescale(d, RAW_EXPONENT)
    raw = int(scaled)
    if scaled != raw:
        raise ValueError("amount has more precision than 10^-30 nano")
    return raw


def raw_to_nano(raw: int) -> Decimal:
    """Convert raw (10**30 scale) to a nano Decimal."""
    if not isinstance(raw, int):
        raise TypeError("raw must be an int")
    return _rescale(Decimal(raw), -RAW_EXPONENT)


def nano_str(raw: int) -> str:
    """Format raw as a plain decimal nano string (no exponent), e.g. '0.000001'.

    Trailing zeros are trimmed for readability. Trimming is done on the digits,
    not with Decimal.normalize(), which would round to the context precision.
    """
    text = format(raw_to_nano(raw), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"