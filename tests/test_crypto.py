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