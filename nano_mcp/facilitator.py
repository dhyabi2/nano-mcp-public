"""Self-hostable x402 `exact`-on-`nano` facilitator.

Implements the /supported, /verify, /settle HTTP surface specified in
draft/x402/specs/schemes/exact/scheme_exact_nano.md (block 10). This closes the
gap between what that spec claimed the Python reference builds ("implements the
/supported, /verify, /settle surface") and the product code, which previously
had no HTTP facilitator.

Semantics (faithful to the verified block-11 TS draft + block-10 spec):

  GET /supported
      -> { "scheme": "exact", "network": "nano:live", "asset": "XNO",
           "endpoints": [...] }   (what this facilitator can verify)

  POST /verify  body = { "requirements": {...}, "payload": {...} }
      -> Validates scheme/network/asset/amount/payTo consistency, that
         payload.payload.paymentProof is a 64-hex block hash, then verifies
         the block on AT LEAST TWO INDEPENDENT Nano RPC endpoints, FAILING
         CLOSED if any endpoint cannot confirm it. Returns
         { "isValid": bool, "payer"?, "extra": {confirmedOn, consulted} }.

  POST /settle  body = { "requirements": {...}, "payload": {...} }
      -> Re-runs FULL verification (never trusts a prior /verify), then binds
         the proof to the request with an ATOMIC SINGLE-USE claim keyed by
         "nano:live <block-hash> <request_id>", so one proof yields one
         resource exactly once. Returns
         { "success": bool, "transaction", "network", "payer"?, "amount"? }.

The facilitator never moves, wraps or guards funds (Nano has no smart
contracts). The client's own send IS the payment; the facilitator only verifies
the on-chain proof on >=2 independent RPCs and settles it exactly once.

The RPC endpoints are injectable so tests can drive the verification against a
stub block store without touching the live node or moving money.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Sequence
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# RPC endpoint layer (fail-closed multi-endpoint verifier)
# ---------------------------------------------------------------------------

# Checked with fullmatch: "$" also matches before a trailing "\n". A node looks
# a hash up case-insensitively, so the claim key upper-cases the hash
# (consumption_key / _canonical_key): otherwise each case spelling of one block
# would get its own claim key and settle the same payment again. The proof
# itself is passed to the node and echoed back exactly as the caller sent it.
_HEX64 = re.compile(r"[0-9A-Fa-f]{64}")

NANO_LIVE_NETWORK = "nano:live"
NANO_ASSET = "XNO"
NANO_SCHEME = "exact"

DEFAULT_ENDPOINTS = (
    "https://rpc.nano.to",
    "https://rainstorm.city/api",
)


class RpcError(RuntimeError):
    """Raised when an endpoint returns a non-2xx or an error payload."""


def parse_raw(amount: str) -> int:
    """Parse a Nano amount (raw, or decimal with up to 30 raw digits) to raw."""
    clean = str(amount).strip()
    if clean == "":
        return 0
    if "." not in clean:
        return int(clean)
    int_part, frac = clean.split(".", 1)
    frac = (frac + "0" * 30)[:30]
    return int(int_part or "0") * 10**30 + int(frac or "0")


# The only amount spellings a request may carry: ASCII digits, optionally one
# "." and 1-30 more digits. parse_raw is not a validator: it hands the parts to
# int(), which accepts a sign, "_" separators, surrounding whitespace and any
# Unicode decimal digit, so "-0.5" read as +0.5 XNO (int("-0") is 0), and "+1",
# "1_000", "1." and Arabic-Indic digits read as amounts too. A JSON number (1.0)
# reached it through str(). fullmatch, not match + "$": "$" also matches before
# a trailing newline.
_AMOUNT_RE = re.compile(r"[0-9]+(\.[0-9]{1,30})?")


def is_amount_string(amount: object) -> bool:
    """True only for a str of ASCII digits with an optional 1-30 digit fraction."""
    return isinstance(amount, str) and _AMOUNT_RE.fullmatch(amount) is not None


def amount_text(amount: object) -> str | None:
    """The plain spelling of a requirement's amount, or None if it has none.

    A str must already be plain (`is_amount_string`). A Python int - or a JSON
    integer such as `"amount": 1000` - is raw and reads exactly as `str(int)`;
    it verified on main and is still accepted. A negative int spells "-..." and
    is refused by the same pattern as "-1". bool (an int subclass, so True was
    1 raw), float, Decimal and every other type are refused: str() of them is
    not a spelling anyone quoted (1.0 -> "1.0" XNO, 1e-06 -> exponent).
    """
    if isinstance(amount, bool):
        return None
    if isinstance(amount, int):
        try:
            amount = str(int(amount))
        except ValueError:  # more digits than int -> str conversion allows
            return None
    return amount if is_amount_string(amount) else None


def _show_amount(amount: object) -> str:
    """repr() for a refusal message, bounded: repr of an int past the digit
    limit raises ValueError, and a 5000-digit string is not a useful message."""
    try:
        shown = repr(amount)
    except ValueError:
        return f"<{type(amount).__name__} too large to print>"
    return shown if len(shown) <= 60 else shown[:57] + "..."


@dataclass(frozen=True)
class NormalizedBlock:
    """Canonical decode of one node's `block_info` response.

    Because independent Nano RPC nodes return the same block with slightly
    different field names, every response is normalized here into one shape so
    the fail-closed comparison ("same amount going to payTo from a real sender")
    is decoupled from node-specific quirks. Missing critical fields are an
    error, not a silent default.
    """

    account: str          # emitter (may be blank if node omits it; the payer is
                          # not required by the spec but a blank is surfaced)
    subtype: str          # 'send' / 'state' / ''
    destination: str      # payee: contents.link_as_account on a real state block
    amount_raw: int
    confirmed: bool

    @property
    def is_send(self) -> bool:
        return self.subtype in ("send", "state", "")


def _contents_of(raw: dict) -> dict:
    """The block's contents as a mapping, however the node chose to send them.

    A node that honours `json_block: "true"` sends an object. A node that
    ignores it sends the same thing as an opaque JSON string - and the payee of
    a state block lives only in there, so reading the string is the difference
    between checking who was paid and not being able to.
    """
    contents = raw.get("contents")
    if isinstance(contents, dict):
        return contents
    if isinstance(contents, str) and contents.strip():
        try:
            parsed = json.loads(contents)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def normalize_block_info(raw: dict) -> NormalizedBlock:
    """Map a node's `block_info` response into the canonical form.

    Raises RpcError on a malformed/incomplete response: a node that returns a
    block without its confirmed flag or its amount cannot authoritatively prove
    anything, so it must fail closed rather than be treated as "not confirmed".
    """
    if not isinstance(raw, dict):
        raise RpcError("block_info: response is not a JSON object")

    # confirmed: real nodes return the STRING "true"; stubs use a bool True.
    confirmed_raw = raw.get("confirmed")
    if confirmed_raw is None:
        raise RpcError("block_info: missing 'confirmed' flag")
    confirmed = (
        confirmed_raw is True
        or str(confirmed_raw).strip().lower() == "true"
    )

    # Emitter account: block_account (real nodes) or account (spec/stub).
    account = str(raw.get("block_account") or raw.get("account") or "")

    # Subtype: contents.type (real send blocks) or subtype/type top-level.
    contents = _contents_of(raw)
    subtype = str(
        raw.get("subtype")
        or raw.get("type")
        or (contents.get("type") if isinstance(contents, dict) else None)
        or ""
    )

    # Receiver. A real node's send block is a STATE block, and a state block
    # names its payee in `contents.link_as_account`; it has no
    # `contents.destination` at all - that field belongs to the pre-state send
    # blocks nobody has been able to create since 2018. Reading only
    # `contents.destination` (plus two fields that live inside `contents`, never
    # at the top level) left `destination` EMPTY for every block a real node
    # ever returned, and the caller below then skipped the payee check
    # entirely - so any confirmed send of the right amount verified, whoever it
    # had actually paid.
    #
    # Captured from rpc.nano.to, block 64AD5A5C...FEDF, which is the shape that
    # matters: top-level `subtype: "send"`, `contents.type: "state"`,
    # `contents.link_as_account: nano_1natrium...`, and no `destination`
    # anywhere. `tests/test_facilitator_real_node_shape.py` pins it verbatim.
    #
    # `link` is deliberately NOT a fallback: it is the payee as 64 hex
    # characters, not as an account, so comparing it to a `nano_` payTo can only
    # ever refuse a payment that was in fact made.
    destination = str(
        (
            (
                contents.get("link_as_account")
                or contents.get("destination")
            )
            if isinstance(contents, dict)
            else None
        )
        or raw.get("link_as_account")
        or raw.get("destination")
        or ""
    )

    amount_raw_v = raw.get("amount")
    if amount_raw_v is None:
        raise RpcError("block_info: missing 'amount'")
    try:
        amount_raw = parse_raw(str(amount_raw_v))
    except ValueError as err:
        raise RpcError(f"block_info: unparseable amount {amount_raw_v!r}") from err

    return NormalizedBlock(
        account=account,
        subtype=subtype,
        destination=destination,
        amount_raw=amount_raw,
        confirmed=confirmed,
    )


@dataclass
class RpcEndpoint:
    """One independent Nano RPC endpoint. `call` is injectable; the default
    POSTs a JSON-RPC action to `url` (mirrors nano_sdk.client.RpcClient)."""

    url: str
    api_key: str | None = None
    timeout: float = 30.0
    call: Callable[[str, dict], dict] | None = None

    def invoke(self, action: str, **params: object) -> dict:
        if self.call is not None:
            return self.call(action, params)
        import httpx

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        resp = httpx.post(
            self.url, json={"action": action, **params}, headers=headers, timeout=self.timeout
        )
        if resp.status_code != 200:
            raise RpcError(f"{self.url} HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        if isinstance(data, dict) and isinstance(data.get("error"), str):
            raise RpcError(f"{self.url} rpc error: {data['error']}")
        return data


@dataclass
class VerificationResult:
    ok: bool
    confirmed_sends: list = field(default_factory=list)
    consulted: int = 0
    reason: str | None = None

    @property
    def confirmed_on(self) -> int:
        return len(self.confirmed_sends)


def verify_block_on_independent_endpoints(
    endpoints: Sequence[RpcEndpoint],
    block_hash: str,
    pay_to: str,
    amount: str,
) -> VerificationResult:
    """Confirm `block_hash` is a send of exactly `amount` to `pay_to` on EVERY
    configured independent endpoint; FAILS CLOSED if any endpoint errors or any
    check fails (strategy law L1: >=2 endpoints, any single-RPC outage refuses).
    """
    required = len(endpoints)
    if required < 2:
        return VerificationResult(
            ok=False,
            consulted=required,
            reason=f"fail-closed: need >=2 independent RPC endpoints, got {required}",
        )

    confirmed_sends: list[dict] = []
    failures = 0
    first_reason: str | None = None
    # Refuse before any node is asked: an amount with no plain spelling, one
    # parse_raw cannot read ("1" * 5000 matches the pattern but exceeds int()'s
    # digit limit and raised ValueError out of this function), and a zero
    # amount, which a 0-raw send would otherwise "confirm" as paid.
    text = amount_text(amount)
    if text is None:
        return VerificationResult(
            ok=False,
            consulted=0,
            reason=f"fail-closed: amount is not a plain Nano amount string: {_show_amount(amount)}",
        )
    try:
        expected = parse_raw(text)
    except ValueError:
        return VerificationResult(
            ok=False,
            consulted=0,
            reason=f"fail-closed: amount is not a readable Nano amount: {_show_amount(text)}",
        )
    if expected <= 0:
        return VerificationResult(
            ok=False,
            consulted=0,
            reason=f"fail-closed: amount must be a positive raw amount, got {_show_amount(amount)}",
        )

    for ep in endpoints:
        try:
            # `json_block: "true"` is REQUIRED, not optional: without it a node
            # returns `contents` as an opaque JSON STRING, and the payee - which
            # on a state block lives only in `contents.link_as_account` - cannot
            # be read at all. rpc.nano.to is a wrapper that normalises contents
            # to an object either way, which is why omitting this looked
            # harmless; rainstorm.city/api is a plainer proxy and does not, so
            # the live two-endpoint test refused there.
            info = ep.invoke("block_info", json_block="true", hash=block_hash)
            nb = normalize_block_info(info)
            if not nb.confirmed:
                raise RpcError(f"block not confirmed on {ep.url}")
            if not nb.is_send:
                raise RpcError(f"block subtype {nb.subtype!r} not a send on {ep.url}")
            if nb.amount_raw != expected:
                raise RpcError(f"amount {nb.amount_raw} != required {expected} on {ep.url}")
            # A payee this code could not read is a payee it cannot vouch for, so
            # it refuses. This used to default `receiver` to `pay_to` and then
            # compare the two, which is vacuously true - the check that proves
            # the money arrived at the seller was skipped in exactly the case
            # where it could not be made.
            if not nb.destination:
                raise RpcError(
                    f"block_info on {ep.url} carries no payee account, so it cannot "
                    f"prove who was paid; refusing rather than assuming payTo={pay_to}"
                )
            if nb.destination != pay_to:
                raise RpcError(
                    f"block pays {nb.destination}, not payTo={pay_to} on {ep.url}"
                )

            confirmed_sends.append(
                {
                    "block_hash": block_hash,
                    "payer": nb.account,
                    "amount": str(nb.amount_raw),
                    # The payee the BLOCK names, now that it is checked to equal
                    # pay_to. It used to be written as pay_to unconditionally,
                    # so the receipt asserted a payee that had never been read.
                    "receiver": nb.destination,
                    "confirmed": True,
                }
            )
        except Exception as err:  # noqa: BLE001 - any endpoint failure refuses
            failures += 1
            first_reason = first_reason or str(err)

    return VerificationResult(
        ok=failures == 0,
        confirmed_sends=confirmed_sends,
        consulted=required,
        reason=None if failures == 0 else (first_reason or f"confirmed on {len(confirmed_sends)}/{required}"),
    )


def build_fail_closed_config(
    custom: Sequence[RpcEndpoint] | None = None,
) -> list[RpcEndpoint]:
    """Default 2-endpoint fail-closed config, overridable by >=2 custom nodes."""
    if custom and len(custom) >= 2:
        return list(custom)
    return [RpcEndpoint(url=u) for u in DEFAULT_ENDPOINTS[:2]]


# ---------------------------------------------------------------------------
# Atomic single-use claim store (spec § Replay / single-use claim)
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS claims (
    consumption_key TEXT PRIMARY KEY,
    claimed_at      REAL NOT NULL
);
"""

# Canonical key for each old-spelling claim: hash (chars 11-74) upper-cased, a
# "\n" right after the hash dropped, the request_id kept as is. Only text keys
# with a 64-hex hash followed by " " or "\n " qualify; anything else is left.
# It runs on every open, so rows whose canonical key already exists are dropped
# early (EXISTS, a primary-key lookup) before the costlier hex GLOB; the CASE
# keeps SQLite from evaluating the GLOB first.
_CANONICAL_SQL = """'nano:live ' || upper(substr(c.consumption_key, 11, 64)) ||
       CASE WHEN substr(c.consumption_key, 75, 2) = char(10) || ' '
            THEN substr(c.consumption_key, 76) ELSE substr(c.consumption_key, 75) END"""
_MIGRATE_SQL = f"""
INSERT OR IGNORE INTO claims (consumption_key, claimed_at)
SELECT {_CANONICAL_SQL}, min(c.claimed_at)
FROM claims AS c
WHERE typeof(c.consumption_key) = 'text'
  AND substr(c.consumption_key, 1, 10) = 'nano:live '
  AND (substr(c.consumption_key, 11, 64) <> upper(substr(c.consumption_key, 11, 64))
       OR substr(c.consumption_key, 75, 1) = char(10))
  AND (substr(c.consumption_key, 75, 1) = ' ' OR substr(c.consumption_key, 75, 2) = char(10) || ' ')
  AND length(substr(c.consumption_key, 11, 64)) = 64
  AND CASE WHEN EXISTS (SELECT 1 FROM claims AS k WHERE k.consumption_key = {_CANONICAL_SQL})
           THEN 0
           ELSE substr(c.consumption_key, 11, 64) NOT GLOB '*[^0-9A-Fa-f]*' END
GROUP BY 1
"""


class ClaimStore:
    """Atomic single-use claim keyed by `nano:live <block-hash> <request_id>`.
    INSERT..PRIMARY KEY wins exactly once; a repeat returns False (spent)."""

    def __init__(self, path: str | None = None):
        self.path = path or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "claims.db"
        )
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        # WAL + busy timeout so a second handle to the same file (e.g. a test or
        # a second facilitator process) does not immediately raise
        # 'database is locked' — same discipline as store.py's read-path fix.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._canonicalise_existing_keys()

    def _canonicalise_existing_keys(self) -> None:
        """Give every claim stored under an older key spelling its canonical key.

        Keys used to carry the block hash exactly as the caller sent it (any hex
        case, and `<hash>\\n` passed the old `^...$` check). Claims are now
        looked up under the upper-case spelling, so without this a payment
        settled before the upgrade could settle once more in another spelling.

        One INSERT OR IGNORE ... SELECT, done in SQL inside one IMMEDIATE
        transaction: only text keys of the shape `nano:live <64-hex> <rid>`
        whose hash is not already canonical are read; the canonical key gets the
        earliest claimed_at; the request_id is kept byte for byte; the old row
        is kept (the previous release looks that spelling up).

        It runs on every open, not once: after a rollback to the previous
        release and a roll-forward, the claims the previous release wrote in
        between get their canonical keys too. No marker is stored, so the
        file's PRAGMA user_version is left as the user set it. Rows already
        migrated are ignored by INSERT OR IGNORE.

        It never fails the open. If the file is read-only, or another writer
        holds the lock past busy_timeout, the migration is skipped and retried
        on the next open; meanwhile Facilitator.settle still refuses a replay
        in the spelling that was stored, because it also looks the key up
        exactly as sent. What it cannot cover: a payment claimed in one
        spelling by an older release and replayed in another spelling before
        the next open, i.e. an older and a newer release writing one file at
        the same time.
        """
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    self._conn.execute(_MIGRATE_SQL)
                    self._conn.commit()
                except BaseException:
                    self._conn.rollback()
                    raise
            except sqlite3.Error:
                return

    def _claim_spellings(self, canonical: str, as_sent: str) -> bool:
        """claim(canonical), also recording the key as sent (when it differs)
        in the same transaction, so the previous release finds the claim."""
        with self._lock:
            try:
                import time

                now = time.time()
                self._conn.execute(
                    "INSERT INTO claims (consumption_key, claimed_at) VALUES (?,?)",
                    (canonical, now),
                )
                if as_sent != canonical:
                    self._conn.execute(
                        "INSERT OR IGNORE INTO claims (consumption_key, claimed_at) VALUES (?,?)",
                        (as_sent, now),
                    )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                self._conn.rollback()
                return False

    def _has_key_as_is(self, key: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM claims WHERE consumption_key=?", (key,)
            ).fetchone()
        return row is not None

    def claim(self, consumption_key: str) -> bool:
        with self._lock:
            try:
                import time

                self._conn.execute(
                    "INSERT INTO claims (consumption_key, claimed_at) VALUES (?,?)",
                    (_canonical_key(consumption_key), time.time()),
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                # already claimed -> replay refused; roll back so the aborted
                # INSERT does not leave this connection holding the write lock
                # (which would block a second handle to the same file).
                self._conn.rollback()
                return False

    def is_claimed(self, consumption_key: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM claims WHERE consumption_key=?",
                (_canonical_key(consumption_key),),
            ).fetchone()
        return row is not None


def _proof_from(payload: dict) -> tuple[str, bool]:
    """payload.payload.paymentProof exactly as sent, and whether it is a 64-hex
    hash. The case is not changed here: the node gets, and the caller gets back,
    the spelling the caller used; only the claim key is case-normalised."""
    proof = str((payload.get("payload") or {}).get("paymentProof") or "")
    return proof, _HEX64.fullmatch(proof) is not None


_KEY_PREFIX = f"{NANO_LIVE_NETWORK} "


def _canonical_key(key: str) -> str:
    """`nano:live <hash> <request_id>` with the hash upper-cased (and a trailing
    "\n" on the hash, which the old check let through, dropped). The
    request_id is case-sensitive and kept byte for byte. A key that does not
    have that shape is returned unchanged."""
    if not key.startswith(_KEY_PREFIX):
        return key
    rest = key[len(_KEY_PREFIX):]
    block_hash, tail = rest[:64], rest[64:]
    if _HEX64.fullmatch(block_hash) is None:
        return key
    if tail.startswith("\n "):
        tail = tail[1:]
    if not tail.startswith(" "):
        return key
    return _KEY_PREFIX + block_hash.upper() + tail


def consumption_key(block_hash: str, request_id: str) -> str:
    """The single-use claim key for a block hash and request. The hash is
    upper-cased, so every hex-case spelling of one block names one key."""
    return _canonical_key(f"{_KEY_PREFIX}{block_hash} {request_id}")


# ---------------------------------------------------------------------------
# Facilitator
# ---------------------------------------------------------------------------

REQUIRED_SECTIONS = ("scheme", "network", "amount", "asset", "payTo")


@dataclass
class FacilitatorConfig:
    endpoints: Sequence[RpcEndpoint]
    claim_store: ClaimStore | None = None


class Facilitator:
    """Self-hostable x402 `exact`-on-`nano` facilitator (/supported, /verify,
    /settle). Never moves funds: verifies on >=2 independent RPCs (fail-closed)
    and settles with an atomic single-use claim."""

    scheme = NANO_SCHEME
    network = NANO_LIVE_NETWORK
    asset = NANO_ASSET

    def __init__(self, config: FacilitatorConfig):
        self.endpoints = list(config.endpoints)
        self.claim_store = config.claim_store or ClaimStore()

    # -- HTTP surface ------------------------------------------------------
    def supported(self) -> dict:
        return {
            "scheme": self.scheme,
            "network": self.network,
            "asset": self.asset,
            "x402Version": 2,
            "endpoints": [ep.url for ep in self.endpoints],
        }

    def verify(self, requirements: dict, payload: dict) -> dict:
        """Verify a payment proof on >=2 independent RPC endpoints (fail-closed)."""
        req_check = self._check_requirements(requirements, payload)
        if req_check is not None:
            return req_check
        proof, well_formed = _proof_from(payload)
        if not well_formed:
            return {
                "isValid": False,
                "invalidReason": "proof missing",
                "invalidMessage": "payload.paymentProof must be a 64-hex Nano block hash",
            }
        res = verify_block_on_independent_endpoints(
            self.endpoints, proof, requirements["payTo"], requirements["amount"]
        )
        if not res.ok:
            return {
                "isValid": False,
                "invalidReason": "unconfirmed",
                "invalidMessage": res.reason
                or "proof not confirmed on all independent RPC endpoints",
                "extra": {"confirmedOn": res.confirmed_on, "consulted": res.consulted},
            }
        send = res.confirmed_sends[0]
        return {
            "isValid": True,
            "payer": send["payer"],
            "extra": {
                "confirmedOn": res.confirmed_on,
                "consulted": res.consulted,
                "amount": send["amount"],
            },
        }

    def settle(self, requirements: dict, payload: dict) -> dict:
        """Re-run FULL verification (never trust a prior /verify), then bind the
        proof to this request atomically exactly once."""
        proof, well_formed = _proof_from(payload)
        req_check = self._check_requirements(requirements, payload)
        if req_check is not None:
            return {
                "success": False,
                "errorReason": "invalid",
                "errorMessage": "requirements inconsistent",
                "transaction": proof,
                "network": self.network,
            }
        if not well_formed:
            return {
                "success": False,
                "errorReason": "invalid",
                "errorMessage": "bad paymentProof",
                "transaction": proof,
                "network": self.network,
            }

        # 1. Re-verify on-chain (MUST NOT trust the earlier /verify).
        res = verify_block_on_independent_endpoints(
            self.endpoints, proof, requirements["payTo"], requirements["amount"]
        )
        if not res.ok or not res.confirmed_sends:
            return {
                "success": False,
                "errorReason": "unconfirmed",
                "errorMessage": res.reason or "not verified on all endpoints",
                "transaction": proof,
                "network": self.network,
            }

        # 2. Atomic single-use claim (exactly-one-wins, replay-safe).
        # `extra` is absent, null or an object here (_check_requirements).
        request_id = str((requirements.get("extra") or {}).get("requestId") or "")
        key = consumption_key(proof, request_id)
        refusal = self._claim(key, f"{_KEY_PREFIX}{proof} {request_id}")
        if refusal is not None:
            reason, message = refusal
            return {
                "success": False,
                "errorReason": reason,
                "errorMessage": message,
                "transaction": proof,
                "network": self.network,
            }

        send = res.confirmed_sends[0]
        return {
            "success": True,
            "transaction": proof,
            "network": self.network,
            "payer": send["payer"],
            "amount": send["amount"],
        }

    _DUPLICATE = ("duplicate", "paymentProof already claimed for this request")

    def _claim(self, key: str, as_sent: str) -> tuple[str, str] | None:
        """Claim the canonical key once; None on success, else (errorReason,
        errorMessage). A claim recorded under the key exactly as sent (by an
        older release, or in a store that was never migrated) also counts as
        spent.

        The built-in ClaimStore (and any subclass of it) is checked for the
        as-sent key with a raw lookup, `_has_key_as_is`, so a subclass that
        overrides only claim() is still checked. Unless claim() is overridden,
        both spellings are recorded in one transaction.

        A custom store, for a proof not spelt in upper case, is first asked
        `is_claimed(as_sent)` when it has one, then `claim(as_sent)`, then
        `claim(canonical)`; a False from either claim is a duplicate. So a store
        without is_claimed still refuses a claim an older release stored under
        the as-sent key. If is_claimed raises (wrong signature, not callable,
        refuses the key, store down) settle refuses; it never succeeds on an
        error."""
        store = self.claim_store
        if isinstance(store, ClaimStore):
            if as_sent != key and store._has_key_as_is(as_sent):
                return self._DUPLICATE
            if type(store).claim is ClaimStore.claim:
                ok = store._claim_spellings(key, as_sent)
            else:
                ok = store.claim(key)
            return None if ok else self._DUPLICATE
        if as_sent != key:
            is_claimed = getattr(store, "is_claimed", None)
            if is_claimed is not None:
                try:
                    spent = is_claimed(as_sent)
                except Exception:  # noqa: BLE001 - fail closed on any store error
                    return (
                        "claim_store_error",
                        "the claim store could not check this paymentProof",
                    )
                if spent:
                    return self._DUPLICATE
            if not store.claim(as_sent):
                return self._DUPLICATE
        return None if store.claim(key) else self._DUPLICATE

    def _check_requirements(self, requirements: dict, payload: dict) -> dict | None:
        # Both halves have to be objects before anything is read off them. The
        # HTTP surface passes whatever the body held: `{"requirements": []}`
        # reached `requirements.get(...)` and raised AttributeError out of the
        # handler, so the caller got a dropped connection instead of a verdict.
        if not isinstance(requirements, dict) or not isinstance(payload, dict):
            return self._invalid(
                "bad-request", "requirements and payload must both be JSON objects"
            )
        accepted = payload.get("accepted") or {}
        if not isinstance(accepted, dict):
            return self._invalid("bad-request", "payload.accepted must be a JSON object")
        if requirements.get("scheme") != self.scheme:
            return self._invalid("bad-scheme", "scheme must be exact")
        if (
            requirements.get("network") != self.network
            or accepted.get("network") != self.network
        ):
            return self._invalid("bad-network", f"network must be {self.network}")
        if (
            requirements.get("asset") != self.asset
            or accepted.get("asset") != self.asset
        ):
            return self._invalid("bad-asset", f"asset must be {self.asset}")
        if requirements.get("amount") != accepted.get("amount"):
            return self._invalid("bad-amount", "amount mismatch between requirements and accepted")
        if requirements.get("payTo") != accepted.get("payTo"):
            return self._invalid("bad-payto", "payTo mismatch between requirements and accepted")
        # settle() reads `extra.requestId` to bind the claim, AFTER the on-chain
        # re-verify. `requirements.get("extra", {})` is only `{}` when the key is
        # absent: `"extra": null` or `"extra": "abc"` reached `.get` on a None or
        # a str, raised AttributeError, and the paid request could never be
        # settled - the connection closed with no verdict. Null reads as absent;
        # anything else that is not an object is refused here, before any node.
        extra = requirements.get("extra")
        if extra is not None and not isinstance(extra, dict):
            return self._invalid("bad-requirements", "requirements.extra must be a JSON object")

        # REQUIRED_SECTIONS was declared and never enforced, and the two checks
        # above compare requirements to `accepted` rather than to anything: a
        # field absent from BOTH is None == None and passed. `verify` and
        # `settle` then indexed `requirements["payTo"]` / `["amount"]` directly
        # and raised KeyError; a present-but-unparseable amount reached
        # `parse_raw` outside any try and raised ValueError. Neither is caught
        # anywhere above the HTTP handler, so a POST to /verify or /settle got
        # no JSON answer at all - the connection was closed mid-request, which
        # a paying agent cannot tell apart from the facilitator being down.
        missing = [
            name
            for name in REQUIRED_SECTIONS
            if not str(requirements.get(name) or "").strip()
        ]
        if missing:
            return self._invalid(
                "bad-requirements",
                "requirements is missing or empty: " + ", ".join(missing),
            )

        # The amount is the only field that has to be arithmetic, and it is read
        # once more in verify/settle. Refuse here what `parse_raw` cannot read,
        # and refuse a non-positive amount: `""` parsed to 0 and `"-1"` to -1,
        # both of which were carried all the way to the nodes as the amount to
        # match. Decimal XNO strings ("0.000001") stay valid.
        # parse_raw is not the gate: it read "-0.5" as +0.5 XNO, so a
        # requirement for a negative amount verified isValid against a 0.5 XNO
        # send, and "+1", "1_000", "1.", Arabic-Indic digits and a JSON float
        # were read as amounts too. An amount here is a str of ASCII digits
        # with an optional 1-30 digit fraction, or a non-bool int read as raw.
        # This DOES refuse amounts that main accepted: leading/trailing
        # whitespace (" 1", "1\n"), ".5", "1.", fractions over 30 digits, a
        # float (1.0), a Decimal, sign characters ("+1", "-0.5"), "_"
        # separators and non-ASCII digits ("\u0661"). Exponents ("1e3") and
        # bool were refused here already. verify_block_on_independent_endpoints
        # now refuses all of these plus a zero or negative amount; main matched
        # those against the send, or raised ValueError on "1e3" and bool.
        text = amount_text(requirements["amount"])
        if text is None:
            return self._invalid(
                "bad-amount",
                "amount must be a string of digits with an optional fraction of "
                f"at most 30 digits, or an integer raw amount, got {_show_amount(requirements['amount'])}",
            )
        try:
            amount_raw = parse_raw(text)
        except (ValueError, TypeError):
            return self._invalid(
                "bad-amount",
                f"amount is not a Nano amount: {_show_amount(text)}",
            )
        if amount_raw <= 0:
            return self._invalid(
                "bad-amount",
                f"amount must be a positive raw amount, got {_show_amount(requirements['amount'])}",
            )
        return None

    @staticmethod
    def _invalid(reason: str, message: str) -> dict:
        return {"isValid": False, "invalidReason": reason, "invalidMessage": message}


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------


def make_handler(facilitator: Facilitator) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, obj: dict) -> None:
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            obj = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(obj, dict):
                # do_POST reads `requirements`/`payload` off this with .get(),
                # so a bare number, string, list or null must be refused here
                # and answered 400 rather than raise inside the handler.
                raise ValueError("body must be a JSON object")
            return obj

        def do_GET(self) -> None:  # noqa: N802
            if urlparse(self.path).path == "/supported":
                self._send(200, facilitator.supported())
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            try:
                body = self._read_json()
            except Exception:
                self._send(400, {"error": "invalid json"})
                return
            requirements = body.get("requirements") or {}
            payload = body.get("payload") or {}
            if path == "/verify":
                self._send(200, facilitator.verify(requirements, payload))
            elif path == "/settle":
                self._send(200, facilitator.settle(requirements, payload))
            else:
                self._send(404, {"error": "not found"})

        def log_message(self, fmt: str, *args: object) -> None:  # silence
            if os.environ.get("NANO_FACILITATOR_LOGGING"):
                super().log_message(fmt, *args)

    return Handler


def serve(facilitator: Facilitator, host: str = "127.0.0.1", port: int = 8022) -> ThreadingHTTPServer:
    """Run the facilitator as a self-hostable HTTP server. Callers own
    threading: the returned server has a serve_forever loop to start."""
    return ThreadingHTTPServer((host, port), make_handler(facilitator))


__all__ = [
    "ClaimStore",
    "Facilitator",
    "FacilitatorConfig",
    "RpcEndpoint",
    "RpcError",
    "NormalizedBlock",
    "VerificationResult",
    "consumption_key",
    "make_handler",
    "normalize_block_info",
    "parse_raw",
    "serve",
    "verify_block_on_independent_endpoints",
]