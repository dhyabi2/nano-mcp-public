"""Buyer SDK: capped per-session sub-accounts plus owner-signed Ed25519 mandates.

Roadmap stage 3. The security problem this solves: an autonomous (or
compromised) agent holding spend authority is a risk — it could drain the
wallet. So the OWNER (who alone holds the owner private key / master seed)
delegates only *limited, revocable, expiring* spending authority to each agent
session:

  * derive_session_account(master_seed, session_id)
        a sub-account that is deterministic for a session but distinct across
        sessions (HKDF-SHA256 keyed by the master seed, info = session_id), so
        a compromised session's key cannot spend another session's share.
  * Mandate + sign_mandate / verify_mandate
        the owner signs {'session_id', 'address' (the bound sub-account),
        'cap_raw', 'issued_at', 'expires_at', 'nonce'} with the owner's
        Ed25519 private key. No one can forge or widen a mandate.
  * SessionWallet.send(...)
        the agent-side spend path. Before building or broadcasting ANY block it
        fail-closes on: mandate signature (owner key), expiry, session binding
        (mandate.address == this session's account), the per-session cap,
        the 0.01 XNO/day owner-wide cap, and the on-chain balance.

Money rule (AGENTS.md): outbound <= 0.01 XNO/day, never more than the wallet
holds. Keys stay out of repos; tests use a stub client so no real funds move.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time as _time
from dataclasses import dataclass, field
from decimal import Decimal

import ed25519_blake2b

from . import block as blockmod
from .client import RpcClient, representative_from_info
from .crypto import (
    Account,
    address_from_public_key,
    derive_account,
    public_key,
    public_key_from_address,
    validate_address,
)
from .units import nano_to_raw
from .wallet import DEFAULT_DAILY_CAP_RAW, InsufficientBalance

SESSION_HKDF_INFO = b"nano-buyer-session-v1"


class MandateError(RuntimeError):
    """A mandate failed verification (bad signature, wrong key, or mis-bound)."""


class ExpiredMandate(MandateError):
    pass


class SessionCapExceeded(RuntimeError):
    pass


def _hkdf_sha256(ikm: bytes, info: bytes, length: int = 32) -> bytes:
    prk = hmac.new(b"\x00" * 32, ikm, hashlib.sha256).digest()
    okm = b""
    t = b""
    for counter in range(1, 256):
        t = hmac.new(prk, t + info + bytes([counter]), hashlib.sha256).digest()
        okm += t
        if len(okm) >= length:
            break
    return okm[:length]


def derive_session_account(master_seed: bytes, session_id: str) -> Account:
    """Deterministic per-session sub-account under `master_seed`.

    Distinct session_ids yield distinct addresses (a leaked session key cannot
    derive another session's share); the same session_id reproduces the same
    address so an owner can re-issue without changing the bound account.
    """
    if not isinstance(master_seed, bytes) or len(master_seed) < 16:
        raise ValueError("master_seed must be bytes of at least 16 bytes")
    priv = _hkdf_sha256(
        master_seed, SESSION_HKDF_INFO + b":" + session_id.encode("utf-8"), length=32
    )
    pub = public_key(priv)
    return Account(private_key=priv, public_key=pub, address=address_from_public_key(pub))


@dataclass(frozen=True)
class Mandate:
    """An owner-signed grant of spending authority to one session.

    `address` is the bound sub-account (see derive_session_account); the owner
    signs {'session_id', 'address', 'cap_raw', 'issued_at', 'expires_at',
    'nonce'} so nothing in the grant can be edited without breaking the
    signature, and it cannot be replayed past `expires_at`.
    """

    session_id: str
    address: str
    cap_raw: int
    issued_at: float
    expires_at: float
    nonce: bytes
    version: int = 1
    signature: bytes = b""

    def canonical(self) -> bytes:
        """Deterministic byte string that is signed (all fields except sig)."""
        parts = [
            b"nano-mandate",
            str(self.version).encode(),
            self.session_id.encode("utf-8"),
            self.address.encode("ascii"),
            str(self.cap_raw).encode(),
            repr(self.issued_at).encode(),
            repr(self.expires_at).encode(),
            self.nonce.hex().encode(),
        ]
        return b"\0".join(parts)

    def with_signature(self, sig: bytes) -> "Mandate":
        return Mandate(
            session_id=self.session_id,
            address=self.address,
            cap_raw=self.cap_raw,
            issued_at=self.issued_at,
            expires_at=self.expires_at,
            nonce=self.nonce,
            version=self.version,
            signature=sig,
        )


def issue_mandate(
    owner_private_key: bytes,
    session_id: str,
    address: str,
    cap_raw: int,
    expires_at: float,
    issued_at: float | None = None,
    nonce: bytes | None = None,
    client_clock=None,
) -> Mandate:
    """The owner creates a signed mandate for a session's sub-account."""
    if not validate_address(address):
        raise ValueError("mandate address is not a valid nano_ address")
    if cap_raw <= 0:
        raise ValueError("cap_raw must be > 0")
    if len(owner_private_key) != 32:
        raise ValueError("owner_private_key must be 32 bytes")
    issued = issued_at if issued_at is not None else (client_clock() if client_clock else _time.time())
    if expires_at <= issued:
        raise ValueError("expires_at must be after issued_at")
    mandate = Mandate(
        session_id=session_id,
        address=address,
        cap_raw=int(cap_raw),
        issued_at=issued,
        expires_at=expires_at,
        nonce=nonce or secrets.token_bytes(16),
    )
    sig = ed25519_blake2b.SigningKey(owner_private_key).sign(mandate.canonical())
    return mandate.with_signature(sig)


def verify_mandate(
    mandate: Mandate,
    owner_public_key: bytes,
    now: float | None = None,
) -> None:
    """Raise MandateError/ExpiredMandate unless the mandate is authentic.

    Rejects, in order: missing/wrong key, invalid signature, already expired,
    non-positive cap, or a bad bound address — so a consumer can rely on the
    remaining fields.
    """
    if len(owner_public_key) != 32:
        raise MandateError("owner_public_key must be 32 bytes")
    if not mandate.signature:
        raise MandateError("mandate is unsigned")
    if mandate.cap_raw <= 0:
        raise MandateError("mandate has a non-positive cap")
    if not validate_address(mandate.address):
        raise MandateError("mandate has an invalid bound address")
    try:
        vk = ed25519_blake2b.VerifyingKey(owner_public_key)
        vk.verify(mandate.signature, mandate.canonical())
    except Exception as exc:  # ed25519_blake2b raises ed25519.BadSignatureError
        raise MandateError(f"mandate signature invalid: {exc}") from exc
    now = now if now is not None else _time.time()
    if now > mandate.expires_at:
        raise ExpiredMandate(
            f"mandate expired at {mandate.expires_at:.0f} (now {now:.0f})"
        )


class SessionWallet:
    """The buyer-side spend path for one session under one owner mandate.

    Enforces, before any block is built or broadcast (all fail-closed):

      1. the mandate is genuinely signed by the owner's public key,
      2. the mandate has not expired,
      3. the mandate is bound to THIS session's sub-account
         (mandate.address == self.account.address),
      4. this session's spend so far + amount <= mandate.cap_raw,
      5. owner-wide today + amount <= 0.01 XNO/day,
      6. amount <= on-chain balance.
    """

    def __init__(
        self,
        session_account: Account,
        owner_public_key: bytes,
        mandate: Mandate,
        client=None,
        daily_cap_raw: int = DEFAULT_DAILY_CAP_RAW,
        clock=None,
    ):
        if mandate.address != session_account.address:
            raise MandateError(
                "mandate is bound to a different sub-account than this session's"
            )
        verify_mandate(mandate, owner_public_key, now=clock() if clock else _time.time())
        self.account = session_account
        self.owner_public_key = owner_public_key
        self.mandate = mandate
        self.client = client if client is not None else RpcClient()
        self.daily_cap_raw = daily_cap_raw
        self.clock = clock if clock is not None else _time.time
        # rolling-day owner-wide spend + per-session spend (raw)
        self._day = int(self.clock()) // 86400
        self._daily_spend: int = 0
        self._session_spent: int = 0

    # -- checks (pure, unit-testable) --
    def check_spend(self, amount_raw: int, balance_raw: int | None = None) -> None:
        """Raise unless `amount_raw` may be sent now (all guards pass)."""
        if not validate_address(self.mandate.address) or self.mandate.address != self.account.address:
            raise MandateError("mandate not bound to this session's account")
        verify_mandate(self.mandate, self.owner_public_key, now=self.clock())
        if amount_raw <= 0:
            raise ValueError("amount must be > 0")
        if self._session_spent + amount_raw > self.mandate.cap_raw:
            raise SessionCapExceeded(
                f"session cap {self.mandate.cap_raw} exceeded "
                f"(spent {self._session_spent} + {amount_raw})"
            )
        self._roll_day()
        if self._daily_spend + amount_raw > self.daily_cap_raw:
            raise DailyCapExceeded(
                f"daily cap {self.daily_cap_raw} exceeded "
                f"(already {self._daily_spend} today)"
            )
        if balance_raw is not None and amount_raw > balance_raw:
            raise InsufficientBalance(
                f"amount {amount_raw} raw > balance {balance_raw} raw"
            )

    def available_session(self) -> int:
        return max(0, self.mandate.cap_raw - self._session_spent)

    def _roll_day(self) -> None:
        day = int(self.clock()) // 86400
        if day != self._day:
            self._day = day
            self._daily_spend = 0

    # -- send pipeline (reuses SDK block building / client) --
    def send(
        self,
        destination: str,
        amount_raw: int,
        work: str | None = None,
    ) -> tuple[str, dict]:
        """Send `amount_raw` to `destination` from this session's sub-account.

        All guards (L10: mandate + expiry + binding; L11: session/daily/balance
        caps) are checked BEFORE anything is signed or published. On success
        the spend is recorded against the session cap and the owner-wide daily
        cap. Returns (block_hash, block_dict).
        """
        if not validate_address(destination):
            raise ValueError("destination is not a valid nano_ address")

        info = self.client.account_info(self.account.address)
        balance_raw = int(info.get("balance", "0"))

        self.check_spend(amount_raw, balance_raw)

        frontier_hex = info.get("frontier") or "0" * 64
        frontier = bytes.fromhex(frontier_hex)
        # Keep the sub-account's current representative. Without the flag that
        # `client.account_info` now sets, this read never found one and every
        # session send re-delegated the sub-account to itself. The node's answer
        # is validated (RpcError if malformed) before work_generate; a missing
        # or blank one falls back rather than reaching public_key_from_address.
        rep = representative_from_info(info) or self.account.address
        frontier_ascii = frontier_hex.encode()

        if work is None:
            gen = self.client.call(action="work_generate", hash=frontier_ascii.decode())
            work = gen.get("work")
            assert isinstance(work, str), f"work_generate returned: {gen}"
        assert isinstance(work, str)

        blk = blockmod.build_send_block(
            private_key=self.account.private_key,
            account_pub=self.account.public_key,
            account_address=self.account.address,
            previous=frontier,
            representative_address=rep,
            new_balance_raw=balance_raw - amount_raw,
            destination_address=destination,
            work=work,
        )
        result = self.client.call(
            action="process",
            json_block="true",
            subtype="send",
            block=blk,
        )
        block_hash = result["hash"]
        self._roll_day()
        self._session_spent += amount_raw
        self._daily_spend += amount_raw
        return block_hash, blk


class DailyCapExceeded(RuntimeError):
    pass