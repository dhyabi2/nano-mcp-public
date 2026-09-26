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

def test_an_address_with_a_trailing_newline_is_malformed_not_a_crash():
    """`$` in a Python regex also matches just before a trailing newline, so
    "nano_<60 chars>\\n" matched ADDRESS_RE, reached the base32 decoder and raised
    KeyError('\\n') — out of `public_key_from_address`, documented to raise
    ValueError, and out of `validate_address`, documented to return a bool.

    A trailing newline is the ordinary shape of an address read from a file or a
    config, and `Wallet.send` validates its destination through this path, so the
    caller got a KeyError instead of "invalid destination".

    Only `nano_` addresses are exercised here: the `xrb_` prefix has a separate
    open defect in `public_key_from_address` (it slices the body at a fixed
    offset), which is not this test's subject.
    """
    addr = derive_account(DOCS_SEED).address
    assert validate_address(addr) is True

    assert validate_address(addr + "\n") is False
    with pytest.raises(ValueError):
        public_key_from_address(addr + "\n")

    # the same for the other whitespace a reader leaves behind
    for suffix in ("\r\n", "\r", " ", "\t", "\n\n"):
        assert validate_address(addr + suffix) is False


def test_the_newline_guard_does_not_change_any_valid_address():
    """The anchor change must refuse only the malformed tail: every address the
    library itself produces still decodes to the key it was built from."""
    for i in range(25):
        acct = derive_account(DOCS_SEED, i)
        assert validate_address(acct.address) is True
        assert public_key_from_address(acct.address) == acct.public_key
