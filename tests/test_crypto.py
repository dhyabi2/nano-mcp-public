"""Crypto/units tests. Address vectors verified against the live rpc.nano.to account_key RPC."""
import hashlib

import pytest

from nano_sdk import (
    address_from_public_key,
    derive_account,
    derive_private_key,
    nano_str,
    nano_to_raw,
    public_key,
    public_key_from_address,
    raw_to_nano,
    validate_address,
)

# (public_key_hex, nano_address) ground truth from rpc.nano.to account_key
NODE_PAIRS = [
    (
        "351B53345493B1F3D05980354C6D41ED32BEFEB2664834B95B7DA06292D9D023",
        "nano_1faucet7b6xjyha7m13objpn5ubkquzd6ska8kwopzf1ecbfmn35d1zey3ys",
    ),
    (
        "E89208DD038FBB269987689621D52292AE9C35941A7484756ECCED92A65093BA",
        "nano_3t6k35gi95xu6tergt6p69ck76ogmitsa8mnijtpxm9fkcm736xtoncuohr3",
    ),
    (
        "00" * 32,
        "nano_1111111111111111111111111111111111111111111111111111hifc8npp",
    ),
]

# docs.nano.org/integration-guides/the-basics canonical derivation vector
DOCS_SEED = "0000000000000000000000000000000000000000000000000000000000000001"
DOCS_PRIV = "1495F2D49159CC2EAAAA97EBB42346418E1268AFF16D7FCA90E6BAD6D0965520"

# docs.nano.org/integration-guides/key-management key_expand example (priv -> public -> account)
DOCS_PRIV_EXPAND = "781186FB9EF17DB6E3D1056550D9FAE5D5BBADA6A6BC370E4CBB938B1DC71DA3"
DOCS_PUB_EXPAND = "3068BB1CA04525BB0E416C485FE6A67FD52540227D267CC8B6E8DA958A7FA039"
DOCS_ADDR_EXPAND = "nano_1e5aqegc1jb7qe964u4adzmcezyo6o146zb8hm6dft8tkp79za3sxwjym5rx"


def test_private_key_derivation_matches_official_vector():
    priv = derive_private_key(DOCS_SEED, index=1)
    assert priv.hex().upper() == DOCS_PRIV


def test_public_key_derivation_matches_docs_keyexpand():
    # docs.nano.org key-management key_expand example: priv -> public
    assert public_key(DOCS_PRIV_EXPAND).hex().upper() == DOCS_PUB_EXPAND


def test_address_matches_docs_keyexpand():
    # docs.nano.org key-management key_expand example: pub -> nano_ address
    assert address_from_public_key(bytes.fromhex(DOCS_PUB_EXPAND)) == DOCS_ADDR_EXPAND


@pytest.mark.parametrize("pub_hex,addr", NODE_PAIRS)
def test_address_encoding_matches_node(pub_hex, addr):
    assert address_from_public_key(bytes.fromhex(pub_hex)) == addr


@pytest.mark.parametrize("pub_hex,addr", NODE_PAIRS)
def test_address_decoding_and_checksum(pub_hex, addr):
    pub = public_key_from_address(addr)
    assert pub.hex().upper() == pub_hex
    assert validate_address(addr)


@pytest.mark.parametrize("pub_hex,addr", NODE_PAIRS)
def test_legacy_xrb_prefix_decodes_to_the_same_key(pub_hex, addr):
    # ADDRESS_RE accepts the legacy "xrb_" prefix, so decoding must accept it too.
    # "xrb_" is one character shorter than "nano_"; slicing the body at a fixed
    # offset dropped its first character and made every xrb_ address unusable.
    legacy = "xrb_" + addr.split("_", 1)[1]
    assert public_key_from_address(legacy).hex().upper() == pub_hex
    assert validate_address(legacy) is True


def test_validate_address_never_raises_on_regex_matching_input():
    # validate_address is documented to return a bool, and it only catches
    # ValueError; an AssertionError escaped it for xrb_ input because the
    # mis-sliced body decoded to more than 256 bits.
    legacy = "xrb_" + NODE_PAIRS[0][1].split("_", 1)[1]
    tampered = legacy[:-1] + ("1" if legacy[-1] != "1" else "3")
    assert validate_address(legacy) is True
    assert validate_address(tampered) is False


def test_validate_rejects_bad_checksum():
    good = NODE_PAIRS[0][1]
    tampered = good[:-1] + ("1" if good[-1] != "1" else "3")
    assert validate_address(tampered) is False


def test_validate_rejects_garbage():
    assert validate_address("nano_zzzz") is False
    assert validate_address("bitcoin1q..." ) is False


def test_derive_is_deterministic_and_index_unique():
    a1 = derive_account(DOCS_SEED, index=0)
    a2 = derive_account(DOCS_SEED, index=0)
    a3 = derive_account(DOCS_SEED, index=1)
    assert a1 == a2
    assert a1.address == a2.address
    assert a1.address != a3.address
    assert a1.private_key != a3.private_key
    # private key never leaks in repr
    assert a1.private_key.hex() not in repr(a1)


def test_public_key_derivation_consistent():
    a = derive_account(DOCS_SEED, index=0)
    assert public_key(a.private_key) == a.public_key


def test_units_roundtrip():
    assert nano_to_raw("1") == 10**30
    assert nano_to_raw("0.000001") == 10**24
    assert raw_to_nano(10**24) * 10**30 == 10**24
    assert nano_str(10**24) == "0.000001"


def test_units_reject_overflow_precision():
    with pytest.raises(ValueError):
        nano_to_raw("0." + "0" * 29 + "123456")  # more than 30 decimals


# A whole-XNO balance in raw carries 31 significant digits, three more than the
# 28 that Decimal arithmetic keeps by default. Multiplying or dividing by 10**30
# therefore rounds real balances.
FULL_PRECISION_RAW = [
    3141592653589793238462643383279,  # ~3.14 XNO
    9999999999999999999999999999999,  # one raw short of 10 XNO
    10**30 + 1,                       # 1 XNO + 1 raw
]


@pytest.mark.parametrize("raw", FULL_PRECISION_RAW)
def test_raw_to_nano_is_exact_for_full_precision_balances(raw):
    assert nano_to_raw(raw_to_nano(raw)) == raw


@pytest.mark.parametrize("raw", FULL_PRECISION_RAW)
def test_nano_str_never_rounds_a_balance(raw):
    # Rounding up here would report more XNO than the account holds.
    digits = str(raw).rjust(31, "0")
    expected = f"{digits[:-30]}.{digits[-30:]}".rstrip("0").rstrip(".")
    assert nano_str(raw) == expected


def test_nano_to_raw_accepts_a_full_30_decimal_amount():
    amount = "1.000000000000000000000000000001"  # 1 XNO + 1 raw, exactly representable
    assert nano_to_raw(amount) == 10**30 + 1


def test_an_address_with_a_trailing_newline_is_malformed_not_a_crash():
    """`$` in a Python regex also matches just before a trailing newline, so
    "nano_<60 chars>\\n" matched ADDRESS_RE, reached the base32 decoder and raised
    KeyError('\\n') — out of `public_key_from_address`, documented to raise
    ValueError, and out of `validate_address`, documented to return a bool.

    A trailing newline is the ordinary shape of an address read from a file or a
    config, and `Wallet.send` validates its destination through this path, so the
    caller got a KeyError instead of "invalid destination".

    Both prefixes are exercised: the legacy `xrb_` prefix decodes correctly since
    the fixed-offset slice in `public_key_from_address` was replaced (#4), so it
    must be refused with the same trailing newline too.
    """
    addr = derive_account(DOCS_SEED).address
    assert validate_address(addr) is True

    assert validate_address(addr + "\n") is False
    with pytest.raises(ValueError):
        public_key_from_address(addr + "\n")

    # the same for the other whitespace a reader leaves behind
    for suffix in ("\r\n", "\r", " ", "\t", "\n\n"):
        assert validate_address(addr + suffix) is False

    legacy = "xrb_" + addr.split("_", 1)[1]
    assert validate_address(legacy) is True
    assert validate_address(legacy + "\n") is False
    with pytest.raises(ValueError):
        public_key_from_address(legacy + "\n")


def test_the_newline_guard_does_not_change_any_valid_address():
    """The anchor change must refuse only the malformed tail: every address the
    library itself produces still decodes to the key it was built from."""
    for i in range(25):
        acct = derive_account(DOCS_SEED, i)
        assert validate_address(acct.address) is True
        assert public_key_from_address(acct.address) == acct.public_key


def test_the_ascii_bytes_of_a_hex_key_are_refused_not_silently_accepted():
    """A 64-byte input is not a 32-byte private key, and must not be treated as one.

    `ed25519_blake2b.SigningKey` also accepts the 64-byte "seed || verifying key"
    form, in which bytes 32:64 are returned as the public key VERBATIM. So the 64
    ASCII bytes of a hex-text key -- what `.encode()` gives, and what
    `open(path, "rb").read()` gives for a key file -- produced a well-formed,
    checksum-valid address with no private key behind it. Money sent to such an
    address is unspendable by anyone.
    """
    hex_key = "00" * 31 + "01"
    correct = public_key(bytes.fromhex(hex_key))
    assert public_key(hex_key) == correct  # the hex-string form is unaffected

    ascii_bytes = hex_key.encode()
    assert len(ascii_bytes) == 64
    with pytest.raises(ValueError):
        public_key(ascii_bytes)


def test_public_key_accepts_exactly_32_bytes_and_refuses_every_other_length():
    """`derive_private_key` already guards its length; this one did not, and 64 was
    the length that slipped through into a wrong-but-valid-looking address."""
    assert len(public_key(b"\x01" * 32)) == 32
    for n in (0, 1, 16, 31, 33, 48, 64, 96):
        with pytest.raises(ValueError):
            public_key(b"\x01" * n)


def test_address_from_public_key_refuses_a_key_that_is_not_32_bytes():
    """The encoder took any length and silently emitted a broken address.

    `address_from_public_key` documents "a 32-byte public key" and checked nothing.
    `_b32_fixedwidth(..., 52)` writes exactly 52 characters whatever the value, so a
    key that is too long loses its high bytes and one that is too short is
    zero-extended -- while `checksum()` is taken over the bytes as given. What comes
    back is 65 characters, starts with `nano_`, holds nothing but alphabet
    characters, and is not an address. The library's own validator rejects its own
    encoder's output:

        address_from_public_key(b"\\x11" * 33)  -> 'nano_46aj46aj…ooceg87x'   accepted
        validate_address(that)                  -> False

    It is a *value*, not an exception, so it reads as an address everywhere a string
    is displayed, pasted into an invoice or written into a manifest, and the error
    surfaces at the far end with nothing pointing back at the call that made it.

    64 bytes is what `open(keyfile, "rb").read()` returns for a file holding 64 hex
    characters; 31 bytes is the ordinary result of trimming leading zero bytes off an
    integer. `derive_private_key` guards exactly this shape already.
    """
    good = bytes.fromhex(DOCS_PUB_EXPAND)
    assert validate_address(address_from_public_key(good)) is True

    for n in (0, 16, 31, 33, 64):
        with pytest.raises(ValueError):
            address_from_public_key(b"\x11" * n)


def test_the_length_check_changes_no_address_the_library_produces():
    """It may only refuse more: every 32-byte key still encodes exactly as before."""
    assert address_from_public_key(bytes.fromhex(DOCS_PUB_EXPAND)) == DOCS_ADDR_EXPAND
    for i in range(25):
        acct = derive_account(DOCS_SEED, i)
        assert address_from_public_key(acct.public_key) == acct.address
        # bytearray and memoryview are the same 32 bytes and must still encode
        assert address_from_public_key(bytearray(acct.public_key)) == acct.address
        assert address_from_public_key(memoryview(acct.public_key)) == acct.address


# ---- nano_to_raw's documented exception contract (audit 2026-09-30) ----
#
# The docstring offers ValueError as the way to refuse a bad amount, and `Decimal(str(amount))`
# answered anything unconvertible with decimal.InvalidOperation instead -- an ArithmeticError, which
# `except ValueError` does not catch. `quote(price_nano=...)` in nano_mcp/server.py hands this
# function whatever the calling agent typed, so that was the ordinary path, not an exotic one.

MALFORMED_AMOUNTS = ["", "abc", "0x10", "not-a-price", "1,5", "--1", None, object()]

# Every spelling this function is meant to accept, with the raw it must return. The second law
# below re-checks these so the fix above cannot have moved an amount while narrowing an exception:
# a converter for money may not quietly start answering differently.
ACCEPTED_AMOUNTS = {
    "0": 0,
    "1": 10**30,
    "0.000001": 10**24,
    "1e-30": 1,
    "0.000000000000000000000000000001": 1,
    "1.000000000000000000000000000001": 10**30 + 1,
    "  1  ": 10**30,
    "1E+2": 100 * 10**30,
}


def test_nano_to_raw_refuses_a_malformed_amount_with_value_error():
    from nano_sdk.units import nano_to_raw as to_raw
    for bad in MALFORMED_AMOUNTS:
        with pytest.raises(ValueError):
            to_raw(bad)


def test_nano_to_raw_still_converts_every_accepted_amount_identically():
    from nano_sdk.units import nano_to_raw as to_raw
    for text, expected in ACCEPTED_AMOUNTS.items():
        assert to_raw(text) == expected, text
