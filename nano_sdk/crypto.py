"""Nano key/account primitives, validated against the live rpc.nano.to node.

Derivation (docs.nano.org/integration-guides/the-basics):
    PrivK[i] = blake2b(outLen=32, input=seed || uint32be(i))
    PublicKey = ed25519-blake2b-512(PrivK)      # the Ed25519 curve with Blake2b-512 as hash
    Address   = "nano_" + b32(pubkey, 52) + b32(blake2b-40(pubkey)[::-1], 8)

Encoding notes verified against rpc.nano.to account_key / known addresses:
  * the 32-byte public key is encoded as a BIG-ENDIAN 256-bit integer into a fixed-width
    52 character Nano base32 string (alphabet "13456789abcdefghijkmnopqrstuwxyz");
  * the checksum is blake2b(pubkey, digest_size=5) with its bytes REVERSED (little-endian),
    encoded into a fixed-width 8 character string.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

import ed25519_blake2b

NANO_ALPHABET = "13456789abcdefghijkmnopqrstuwxyz"
_ALPHABET_INDEX = {c: i for i, c in enumerate(NANO_ALPHABET)}
# Anchored with \Z, not $: in Python "$" also matches immediately before a
# trailing newline, so "nano_<60 chars>\n" matched and then reached the base32
# decoder, which raised KeyError('\n') out of functions documented to raise
# ValueError / return a bool. A trailing newline is malformed, not valid.
ADDRESS_RE = re.compile(r"\A(nano|xrb)_[13][13456789abcdefghijkmnopqrstuwxyz]{59}\Z")


def _b32_fixedwidth(value: int, ndigits: int) -> str:
    """Base-32 encode a non-negative big-endian integer into exactly ndigits characters."""
    return "".join(NANO_ALPHABET[(value >> (5 * (ndigits - 1 - i))) & 0x1F] for i in range(ndigits))


def _b32_decode(s: str) -> int:
    n = 0
    for c in s:
        n = (n << 5) | _ALPHABET_INDEX[c]
    return n


def derive_private_key(seed: bytes | str, index: int = 0) -> bytes:
    """Derive a 32-byte account private key from a 32-byte seed + index (Nano spec)."""
    if isinstance(seed, str):
        seed = bytes.fromhex(seed)
    if len(seed) != 32:
        raise ValueError("seed must be 32 bytes (64 hex chars)")
    return hashlib.blake2b(seed + int(index).to_bytes(4, "big"), digest_size=32).digest()


def public_key(private_key: bytes | str) -> bytes:
    """Derive the 32-byte Ed25519-Blake2b public key from a 32-byte private key."""
    if isinstance(private_key, str):
        private_key = bytes.fromhex(private_key)
    return ed25519_blake2b.SigningKey(private_key).get_verifying_key().to_bytes()


def checksum(public_key: bytes) -> bytes:
    """5-byte Nano address checksum (blake2b-40 with little-endian byte order)."""
    return hashlib.blake2b(public_key, digest_size=5).digest()[::-1]


def address_from_public_key(public_key: bytes) -> str:
    """Encode a 32-byte public key into a nano_ address."""
    return "nano_" + _b32_fixedwidth(int.from_bytes(public_key, "big"), 52) + _b32_fixedwidth(
        int.from_bytes(checksum(public_key), "big"), 8
    )


def public_key_from_address(address: str) -> bytes:
    """Decode a nano_ address into its 32-byte public key, verifying the checksum.

    Raises ValueError if the address is malformed or the checksum is wrong.
    """
    if not ADDRESS_RE.match(address):
        raise ValueError("malformed nano/xrb address")
    body = address[5:]
    pub_n = _b32_decode(body[:52])
    ck_n = _b32_decode(body[52:])
    assert pub_n < (1 << 256), "public key field wider than 256 bits"
    pub = pub_n.to_bytes(32, "big")
    if ck_n.to_bytes(5, "big") != checksum(pub):
        raise ValueError("address checksum mismatch (possible typo)")
    return pub


def validate_address(address: str) -> bool:
    """True if address is well-formed AND its checksum verifies."""
    try:
        public_key_from_address(address)
        return True
    except ValueError:
        return False


@dataclass(frozen=True)
class Account:
    """A Nano account derived from a seed + index: private key, public key, address."""

    private_key: bytes
    public_key: bytes
    address: str

    @property
    def representative(self) -> str:
        return self.address

    def __repr__(self) -> str:  # never print the private key
        return f"Account(address={self.address})"


def derive_account(seed: bytes | str, index: int = 0) -> Account:
    """Derive a full account (private key, public key, address) from seed + index."""
    priv = derive_private_key(seed, index)
    pub = public_key(priv)
    return Account(private_key=priv, public_key=pub, address=address_from_public_key(pub))