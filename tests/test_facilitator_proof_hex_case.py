"""One payment settles once, whatever hex case its block hash is spelt in.

A Nano node looks a block hash up case-insensitively: `ab..`, `AB..` and `Ab..`
all name the same confirmed send. The facilitator accepted either case and
built the single-use claim key from the proof exactly as sent, so each case
spelling got its own key and one payment settled once per spelling for the same
requestId. The proof check also used `re.match` with `^...$`, and `$` matches
before a trailing "\\n", so `<hash>\\n` was one more distinct key.

Only the claim key is case-normalised: the node is asked for, and the receipt
echoes, the spelling the caller sent. Claim stores written by the previous
release hold keys in whatever spelling was sent; opening one gives each such
claim its canonical key, so a payment settled before the upgrade stays spent.

The nodes here look hashes up case-insensitively, the way a real node does, and
record every hash they are asked for.
"""
from __future__ import annotations

import sqlite3

import pytest

from nano_mcp.facilitator import (
    ClaimStore,
    Facilitator,
    FacilitatorConfig,
    RpcEndpoint,
    consumption_key,
)
from nano_mcp.httpx402 import derive_requirements
from nano_mcp.paidtool import PaidToolServer

BLOCK = "ab" * 32
PAYER = "nano_3p1zmep1qax1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f9"
PAY_TO = "nano_3t6k35gi95xu6tergt6p69ck76ogmitsa8mnijtpxm9fkcm736xtoncuohr3"
AMOUNT = "1000000000000000000000000000000"
SPELLINGS = [BLOCK.lower(), BLOCK.upper(), "Ab" * 32]


class CaseInsensitiveNode:
    def __init__(self, pay_to: str):
        self.pay_to = pay_to
        self.asked: list[str] = []

    def call(self, action, params):
        assert action == "block_info"
        h = params["hash"]
        self.asked.append(h)
        # Lenient on case and surrounding whitespace, so the facilitator's own
        # check is what has to refuse a malformed proof.
        if h.strip().upper() != BLOCK.upper():
            raise RuntimeError(f"unknown block {h!r}")
        return {
            "hash": BLOCK.upper(),
            "confirmed": True,
            "subtype": "send",
            "amount": AMOUNT,
            "account": PAYER,
            "link_as_account": self.pay_to,
        }


def _endpoints(pay_to):
    a, b = CaseInsensitiveNode(pay_to), CaseInsensitiveNode(pay_to)
    return [
        RpcEndpoint(url="https://stub-a.invalid", call=a.call),
        RpcEndpoint(url="https://stub-b.invalid", call=b.call),
    ], (a, b)


def _fac(tmp_path):
    endpoints, nodes = _endpoints(PAY_TO)
    fac = Facilitator(
        FacilitatorConfig(
            endpoints=endpoints,
            claim_store=ClaimStore(path=str(tmp_path / "claims.db")),
        )
    )
    return fac, nodes


def _pair(proof, request_id="req_case"):
    req = {
        "scheme": "exact",
        "network": "nano:live",
        "amount": AMOUNT,
        "asset": "XNO",
        "payTo": PAY_TO,
        "extra": {"requestId": request_id},
    }
    payload = {
        "accepted": {"network": "nano:live", "asset": "XNO", "amount": AMOUNT, "payTo": PAY_TO},
        "payload": {"paymentProof": proof},
    }
    return req, payload


def test_one_payment_settles_once_across_hex_case_spellings(tmp_path):
    fac, _ = _fac(tmp_path)
    results = [fac.settle(*_pair(p)) for p in SPELLINGS]
    assert [r["success"] for r in results] == [True, False, False]
    assert [r.get("errorReason") for r in results[1:]] == ["duplicate", "duplicate"]
    assert fac.claim_store.is_claimed(consumption_key(BLOCK.upper(), "req_case")) is True


def test_settle_asks_the_node_for_the_hash_as_sent(tmp_path):
    fac, nodes = _fac(tmp_path)
    assert fac.settle(*_pair("Ab" * 32))["success"] is True
    for node in nodes:
        assert node.asked == ["Ab" * 32]


def test_verify_asks_the_node_for_the_hash_as_sent(tmp_path):
    fac, nodes = _fac(tmp_path)
    assert fac.verify(*_pair(BLOCK.lower()))["isValid"] is True
    for node in nodes:
        assert node.asked == [BLOCK.lower()]


@pytest.mark.parametrize("spelling", SPELLINGS, ids=["lower", "upper", "mixed"])
def test_the_receipt_echoes_the_callers_spelling(tmp_path, spelling):
    fac, _ = _fac(tmp_path)
    settled = fac.settle(*_pair(spelling))
    assert settled["success"] is True
    assert settled["transaction"] == spelling
    again = fac.settle(*_pair(spelling))
    assert again["errorReason"] == "duplicate"
    assert again["transaction"] == spelling


@pytest.mark.parametrize("spelling", SPELLINGS, ids=["lower", "upper", "mixed"])
def test_is_claimed_finds_a_settled_payment_in_any_spelling(tmp_path, spelling):
    fac, _ = _fac(tmp_path)
    assert fac.settle(*_pair(BLOCK.lower()))["success"] is True
    assert fac.claim_store.is_claimed(consumption_key(spelling, "req_case")) is True
    assert fac.claim_store.is_claimed(consumption_key(spelling, "REQ_CASE")) is False


class CaseSensitiveNode:
    """A custom adapter or cache that only knows the lowercase spelling."""

    def __init__(self, pay_to: str):
        self.inner = CaseInsensitiveNode(pay_to)

    def call(self, action, params):
        if params["hash"] != BLOCK.lower():
            raise RuntimeError(f"unknown block {params['hash']!r}")
        return self.inner.call(action, params)


def test_a_case_sensitive_node_accepts_the_spelling_it_knows(tmp_path):
    a, b = CaseSensitiveNode(PAY_TO), CaseSensitiveNode(PAY_TO)
    fac = Facilitator(
        FacilitatorConfig(
            endpoints=[
                RpcEndpoint(url="https://stub-a.invalid", call=a.call),
                RpcEndpoint(url="https://stub-b.invalid", call=b.call),
            ],
            claim_store=ClaimStore(path=str(tmp_path / "claims.db")),
        )
    )
    assert fac.verify(*_pair(BLOCK.lower()))["isValid"] is True
    settled = fac.settle(*_pair(BLOCK.lower()))
    assert settled["success"] is True
    assert settled["transaction"] == BLOCK.lower()
    assert fac.settle(*_pair(BLOCK.lower()))["errorReason"] == "duplicate"


@pytest.mark.parametrize("suffix", ["\n", " ", "\r\n"], ids=["lf", "space", "crlf"])
def test_a_proof_with_trailing_whitespace_is_malformed(tmp_path, suffix):
    fac, nodes = _fac(tmp_path)
    verdict = fac.verify(*_pair(BLOCK + suffix))
    assert verdict["isValid"] is False
    assert verdict["invalidReason"] == "proof missing"
    settled = fac.settle(*_pair(BLOCK + suffix))
    assert settled["success"] is False
    assert settled["errorReason"] == "invalid"
    for node in nodes:
        assert node.asked == []


def test_a_trailing_newline_does_not_open_a_second_claim(tmp_path):
    fac, _ = _fac(tmp_path)
    assert fac.settle(*_pair(BLOCK))["success"] is True
    again = fac.settle(*_pair(BLOCK + "\n"))
    assert again["success"] is False


def test_paid_tool_serves_once_across_hex_case_spellings(tmp_path):
    master = bytes(range(32))
    request_id = "req_tool_case"
    pay_to = derive_requirements(master, request_id, AMOUNT)["payTo"]
    endpoints, _ = _endpoints(pay_to)
    paid = PaidToolServer(
        master_secret=master,
        facilitator=Facilitator(
            FacilitatorConfig(
                endpoints=endpoints,
                claim_store=ClaimStore(path=str(tmp_path / "claims.db")),
            )
        ),
        amount_raw=AMOUNT,
        tools={"premium_data": lambda resource: {"data": "secret"}},
    )
    results = [paid.execute("premium_data", request_id, p) for p in SPELLINGS]
    assert [r["success"] for r in results] == [True, False, False]
    assert [r["error_reason"] for r in results[1:]] == ["duplicate", "duplicate"]


def test_paid_tool_refuses_a_malformed_proof_without_raising(tmp_path):
    master = bytes(range(32))
    request_id = "req_tool_bad"
    pay_to = derive_requirements(master, request_id, AMOUNT)["payTo"]
    endpoints, nodes = _endpoints(pay_to)
    paid = PaidToolServer(
        master_secret=master,
        facilitator=Facilitator(
            FacilitatorConfig(
                endpoints=endpoints,
                claim_store=ClaimStore(path=str(tmp_path / "claims.db")),
            )
        ),
        amount_raw=AMOUNT,
        tools={"premium_data": lambda resource: {"data": "secret"}},
    )
    for proof in (BLOCK + "\n", "zz", ""):
        out = paid.execute("premium_data", request_id, proof)
        assert out["success"] is False and out["ok"] is False
        assert out["error_reason"] == "invalid"
        assert "result" not in out
    for node in nodes:
        assert node.asked == []


# --------------------------------------------------- claim stores from before

OLD_SCHEMA = """
CREATE TABLE IF NOT EXISTS claims (
    consumption_key TEXT PRIMARY KEY,
    claimed_at      REAL NOT NULL
);
"""
OLD_RID = "Req_OldMixed"  # a request_id with case of its own
OLD_KEYS = {
    # The previous release built the key from the proof exactly as sent.
    f"nano:live {BLOCK.lower()} {OLD_RID}": 100.0,
    f"nano:live {'aB' * 32} other_req": 200.0,
    f"nano:live {BLOCK.lower()}\n newline_req": 300.0,
    f"nano:live {BLOCK.lower()} ": 400.0,  # empty request_id
    "not a claim key": 500.0,
    f"nano:live {'ab' * 31}zz {OLD_RID}": 600.0,  # hash field not hex
}


def _old_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.executemany("INSERT INTO claims VALUES (?,?)", list(OLD_KEYS.items()))
    conn.commit()
    conn.close()


def _rows(path):
    conn = sqlite3.connect(path)
    try:
        return dict(conn.execute("SELECT consumption_key, claimed_at FROM claims"))
    finally:
        conn.close()


def _fac_on(path, pay_to=PAY_TO):
    endpoints, _ = _endpoints(pay_to)
    return Facilitator(
        FacilitatorConfig(endpoints=endpoints, claim_store=ClaimStore(path=path))
    )


@pytest.mark.parametrize("spelling", SPELLINGS + ["aB" * 32], ids=["lower", "upper", "mixed", "mixed2"])
@pytest.mark.parametrize("request_id", [OLD_RID, "other_req", "newline_req", ""])
def test_a_payment_settled_before_the_upgrade_stays_spent(tmp_path, spelling, request_id):
    path = str(tmp_path / "claims.db")
    _old_db(path)
    fac = _fac_on(path)
    settled = fac.settle(*_pair(spelling, request_id))
    assert settled["success"] is False
    assert settled["errorReason"] == "duplicate"


def test_request_id_case_is_not_folded_by_the_migration(tmp_path):
    path = str(tmp_path / "claims.db")
    _old_db(path)
    fac = _fac_on(path)
    rows = _rows(path)
    assert f"nano:live {BLOCK.upper()} {OLD_RID}" in rows
    assert f"nano:live {BLOCK.upper()} {OLD_RID.lower()}" not in rows
    # A different request_id differing only in case is a different request.
    assert fac.settle(*_pair(BLOCK, OLD_RID.lower()))["success"] is True


def test_the_migration_keeps_old_rows_and_unparseable_keys(tmp_path):
    path = str(tmp_path / "claims.db")
    _old_db(path)
    ClaimStore(path=path)
    rows = _rows(path)
    for key, claimed_at in OLD_KEYS.items():
        assert rows[key] == claimed_at
    assert rows[f"nano:live {BLOCK.upper()} {OLD_RID}"] == 100.0
    assert rows[f"nano:live {BLOCK.upper()} other_req"] == 200.0
    assert rows[f"nano:live {BLOCK.upper()} newline_req"] == 300.0
    assert rows[f"nano:live {BLOCK.upper()} "] == 400.0
    assert len(rows) == len(OLD_KEYS) + 4


def test_the_migration_is_idempotent(tmp_path):
    path = str(tmp_path / "claims.db")
    _old_db(path)
    ClaimStore(path=path)
    first = _rows(path)
    ClaimStore(path=path)
    ClaimStore(path=path)
    assert _rows(path) == first


def test_two_stores_opening_an_old_file_at_once_agree(tmp_path):
    import threading

    path = str(tmp_path / "claims.db")
    _old_db(path)
    errors = []

    def open_it():
        try:
            ClaimStore(path=path)
        except Exception as err:  # noqa: BLE001
            errors.append(err)

    threads = [threading.Thread(target=open_it) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(_rows(path)) == len(OLD_KEYS) + 4


def test_a_fresh_store_is_unaffected(tmp_path):
    path = str(tmp_path / "claims.db")
    fac = _fac_on(path)
    assert _rows(path) == {}
    assert fac.settle(*_pair("aB" * 32, "Req_X"))["success"] is True
    expected = {f"nano:live {BLOCK.upper()} Req_X", f"nano:live {'aB' * 32} Req_X"}
    assert set(_rows(path)) == expected
    ClaimStore(path=path)
    assert set(_rows(path)) == expected


def test_after_a_rollback_the_previous_release_finds_the_claim(tmp_path):
    path = str(tmp_path / "claims.db")
    fac = _fac_on(path)
    for spelling in (BLOCK.lower(), "aB" * 32):
        assert fac.settle(*_pair(spelling, "rid_" + spelling[:2]))["success"] is True
    rows = _rows(path)
    # The previous release looked the key up exactly as sent.
    assert f"nano:live {BLOCK.lower()} rid_ab" in rows
    assert f"nano:live {'aB' * 32} rid_aB" in rows


def _unmigrated(path):
    _old_db(path)
    assert _rows(path)  # old keys only, user_version 0


def test_claims_written_during_a_rollback_are_migrated_on_the_next_open(tmp_path):
    """Upgrade, roll back (the previous release writes a lower-case claim),
    roll forward: the claim written during the rollback is spent in every
    spelling."""
    path = str(tmp_path / "claims.db")
    _unmigrated(path)
    ClaimStore(path=path)
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO claims VALUES (?,?)", (f"nano:live {BLOCK.lower()} late", 9.0))
    conn.commit()
    conn.close()
    fac = _fac_on(path)
    assert f"nano:live {BLOCK.upper()} late" in _rows(path)
    for spelling in SPELLINGS:
        assert fac.settle(*_pair(spelling, "late"))["errorReason"] == "duplicate"


@pytest.mark.parametrize("user_version", [0, 1, 7])
def test_the_users_user_version_is_left_alone_and_does_not_skip_the_migration(
    tmp_path, user_version
):
    path = str(tmp_path / "claims.db")
    _old_db(path)
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version={user_version}")
    conn.commit()
    conn.close()
    fac = _fac_on(path)
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == user_version
    conn.close()
    assert fac.settle(*_pair(BLOCK.upper(), OLD_RID))["errorReason"] == "duplicate"


def test_a_migration_skipped_at_open_is_retried_before_the_next_claim(tmp_path):
    """Another connection holds the write lock while the store opens: the
    migration is skipped, the open still succeeds, and once the lock is
    released the next settle migrates first, so a pre-upgrade lower-case claim
    replayed in upper case is refused (not settled a second time)."""
    path = str(tmp_path / "claims.db")
    _unmigrated(path)
    holder = sqlite3.connect(path)
    holder.execute("PRAGMA journal_mode=WAL")
    holder.execute("BEGIN IMMEDIATE")
    try:
        store = ClaimStore(path=path)
    finally:
        holder.rollback()
        holder.close()
    assert store._migration_pending is True
    assert f"nano:live {BLOCK.upper()} {OLD_RID}" not in _rows(path)
    fac = _fac_with(store)
    assert fac.settle(*_pair(BLOCK.upper(), OLD_RID))["errorReason"] == "duplicate"
    assert store._migration_pending is False
    assert f"nano:live {BLOCK.upper()} {OLD_RID}" in _rows(path)
    for spelling in SPELLINGS:
        assert fac.settle(*_pair(spelling, OLD_RID))["errorReason"] == "duplicate"


class NeverMigrates(ClaimStore):
    """A built-in store whose migration cannot run (as for a file that stays
    locked or read-only)."""

    def _migrate_locked(self):
        self._migration_pending = True


@pytest.mark.parametrize("spelling", SPELLINGS + ["aB" * 32], ids=["lower", "upper", "mixed", "mixed2"])
@pytest.mark.parametrize("request_id", [OLD_RID, "other_req", "newline_req", ""])
def test_while_unmigrated_a_claim_under_any_old_spelling_counts(tmp_path, spelling, request_id):
    path = str(tmp_path / "claims.db")
    _unmigrated(path)
    store = NeverMigrates(path=path)
    fac = _fac_with(store)
    assert fac.settle(*_pair(spelling, request_id))["errorReason"] == "duplicate"
    assert store.is_claimed(consumption_key(spelling, request_id)) is True
    assert set(_rows(path)) == set(OLD_KEYS)  # nothing migrated
    # A new payment still settles once.
    assert fac.settle(*_pair(spelling, "fresh"))["success"] is True
    assert fac.settle(*_pair(BLOCK.upper(), "fresh"))["errorReason"] == "duplicate"
    # The request_id stays case-sensitive in the old-spelling lookup.
    assert store.is_claimed(consumption_key(BLOCK, OLD_RID.lower())) is False


def test_an_open_with_nothing_to_migrate_takes_no_write_lock(tmp_path):
    import time

    path = str(tmp_path / "claims.db")
    _old_db(path)
    ClaimStore(path=path)  # migrates
    holder = sqlite3.connect(path)
    holder.execute("PRAGMA journal_mode=WAL")
    holder.execute("BEGIN IMMEDIATE")
    try:
        started = time.monotonic()
        store = ClaimStore(path=path)
        elapsed = time.monotonic() - started
    finally:
        holder.rollback()
        holder.close()
    # busy_timeout is 5 s; the read-only check finds nothing and returns at once.
    assert elapsed < 2.0
    assert store._migration_pending is False


def test_a_read_only_unmigrated_file_still_opens(tmp_path):
    path = str(tmp_path / "claims.db")
    _unmigrated(path)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.close()

    class ReadOnlyStore(ClaimStore):
        def __init__(self, p):
            self.path = p
            import threading

            self._lock = threading.Lock()
            self._conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, check_same_thread=False)
            self._canonicalise_existing_keys()

    store = ReadOnlyStore(path)
    assert store._migration_pending is True
    assert set(_rows(path)) == set(OLD_KEYS)  # nothing written
    # Not migrated, but the claim is found under its old spelling.
    assert store.is_claimed(consumption_key(BLOCK, OLD_RID)) is True
    assert store.is_claimed(consumption_key(BLOCK, "never_claimed")) is False
    fac = _fac_with(store)
    assert fac.settle(*_pair(BLOCK.upper(), OLD_RID))["errorReason"] == "duplicate"


def test_unusual_rows_do_not_stop_the_file_opening(tmp_path):
    path = str(tmp_path / "claims.db")
    _old_db(path)
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO claims VALUES (NULL, 1.0)")
    conn.execute("INSERT INTO claims VALUES (?, 2.0)", (f"nano:live {'ef' * 32} blob".encode(),))
    conn.execute("INSERT INTO claims VALUES (?, 'not a time')", (f"nano:live {'ef' * 32} text_time",))
    conn.execute("INSERT INTO claims VALUES (?, 3.0)", (f"nano:live {'Ef' * 32} text_time",))
    conn.commit()
    conn.close()
    ClaimStore(path=path)
    rows = _rows(path)
    assert f"nano:live {'EF' * 32} text_time" in rows
    assert f"nano:live {BLOCK.upper()} {OLD_RID}" in rows
    assert not any(isinstance(k, str) and k == f"nano:live {'EF' * 32} blob" for k in rows)


@pytest.mark.parametrize("key", list(OLD_KEYS) + [
    f"nano:live {'ab' * 32}",           # no separator after the hash
    f"nano:live {'ab' * 32}\nx",        # newline not followed by a space
    f"nano:live {'ab' * 32}  two_spaces",
    f"nano:live {'ab' * 32} r\u00e9q",   # non-ASCII request_id
])
def test_the_sql_migration_agrees_with_canonical_key(tmp_path, key):
    from nano_mcp.facilitator import _canonical_key

    path = str(tmp_path / "claims.db")
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.execute("INSERT INTO claims VALUES (?, 1.0)", (key,))
    conn.commit()
    conn.close()
    ClaimStore(path=path)
    assert set(_rows(path)) == {key, _canonical_key(key)}


# ------------------------------------- custom stores: one claim, canonical key


class DictStore:
    """A custom claim store over a set of keys; records every call."""

    def __init__(self, keys=()):
        self.keys = set(keys)
        self.claimed: list[str] = []
        self.asked: list[str] = []

    def claim(self, key):
        self.claimed.append(key)
        if key in self.keys:
            return False
        self.keys.add(key)
        return True

    def is_claimed(self, key):
        self.asked.append(key)
        return key in self.keys


def _fac_with(store):
    endpoints, _ = _endpoints(PAY_TO)
    return Facilitator(FacilitatorConfig(endpoints=endpoints, claim_store=store))


@pytest.mark.parametrize("spelling", SPELLINGS, ids=["lower", "upper", "mixed"])
def test_a_custom_store_gets_exactly_one_claim_of_the_canonical_key(spelling):
    store = DictStore()
    fac = _fac_with(store)
    assert fac.settle(*_pair(spelling))["success"] is True
    assert store.claimed == [f"nano:live {BLOCK.upper()} req_case"]
    assert store.asked == []  # never asked is_claimed, as on main
    for other in SPELLINGS:
        assert fac.settle(*_pair(other))["errorReason"] == "duplicate"
    assert store.asked == []
    assert store.keys == {f"nano:live {BLOCK.upper()} req_case"}


class LowerCasingStore(DictStore):
    """Normalises keys itself (or is backed by a case-insensitive table)."""

    def claim(self, key):
        return super().claim(key.lower())


class WrapsBuiltin:
    """A custom store delegating to the built-in ClaimStore."""

    def __init__(self, path):
        self.inner = ClaimStore(path=path)
        self.claim = self.inner.claim


class AsyncIsClaimed(DictStore):
    async def is_claimed(self, key):  # would be a truthy coroutine if called
        return False


class ClaimOnlyStore:
    def __init__(self):
        self.keys = set()

    def claim(self, key):
        if key in self.keys:
            return False
        self.keys.add(key)
        return True


def _raising(key, *extra):
    raise RuntimeError("store down")


@pytest.mark.parametrize("spelling", SPELLINGS, ids=["lower", "upper", "mixed"])
@pytest.mark.parametrize("kind", ["lower-casing", "wraps-builtin", "async-is_claimed", "raising-is_claimed", "no-is_claimed"])
def test_an_honest_first_payment_settles_once_in_any_custom_store(tmp_path, kind, spelling):
    if kind == "lower-casing":
        store = LowerCasingStore()
    elif kind == "wraps-builtin":
        store = WrapsBuiltin(str(tmp_path / "claims.db"))
    elif kind == "async-is_claimed":
        store = AsyncIsClaimed()
    elif kind == "raising-is_claimed":
        store = DictStore()
        store.is_claimed = _raising
    else:
        store = ClaimOnlyStore()
    fac = _fac_with(store)
    assert fac.settle(*_pair(spelling))["success"] is True
    for other in SPELLINGS:
        assert fac.settle(*_pair(other))["errorReason"] == "duplicate"


class StrictStore(DictStore):
    """A store whose plain insert raises on a duplicate (like a UNIQUE key)."""

    def add(self, key):
        if key in self.keys:
            raise ValueError(f"UNIQUE constraint failed: {key!r}")
        self.keys.add(key)


def _owner_migration(keys):
    """The README's upgrade snippet for a custom store, run as written: add the
    upper-case form of each key only if absent (old keys are kept)."""
    import re

    store = StrictStore(keys)
    old = re.compile(r"nano:live ([0-9A-Fa-f]{64})\n? (.*)", re.S)
    for key in list(store.keys):
        m = old.fullmatch(key)
        if not m:
            continue
        new = f"nano:live {m[1].upper()} {m[2]}"
        if new != key:
            store.claim(new)
    return store.keys


MIGRATION_KEYS = {
    **OLD_KEYS,
    f"nano:live {'CD' * 32} already_upper": 700.0,
    # Two old spellings of one payment: one upper-case key between them.
    f"nano:live {'cd' * 32} two_spellings": 800.0,
    f"nano:live {'Cd' * 32} two_spellings": 801.0,
}


def test_the_readme_upgrade_snippet_agrees_with_canonical_key():
    from nano_mcp.facilitator import _canonical_key

    expected = set(MIGRATION_KEYS) | {_canonical_key(k) for k in MIGRATION_KEYS}
    assert _owner_migration(MIGRATION_KEYS) == expected


def test_a_plain_insert_of_every_upper_case_key_would_fail_part_way():
    """Why the snippet adds only if absent."""
    store = StrictStore(MIGRATION_KEYS)
    with pytest.raises(ValueError, match="UNIQUE"):
        for key in sorted(MIGRATION_KEYS):
            if key.startswith("nano:live ") and len(key) > 74:
                store.add(f"nano:live {key[10:74].upper()} {key[75:].lstrip()}")


def test_the_readme_snippet_is_the_one_tested():
    from pathlib import Path

    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text()
    assert "if new != key:" in readme
    assert "my_store.claim(new)" in readme


@pytest.mark.parametrize("spelling", SPELLINGS + ["aB" * 32], ids=["lower", "upper", "mixed", "mixed2"])
@pytest.mark.parametrize("request_id", [OLD_RID, "other_req", "newline_req", ""])
def test_a_custom_store_migrated_by_its_owner_keeps_old_claims_spent(spelling, request_id):
    store = DictStore(_owner_migration(OLD_KEYS))
    fac = _fac_with(store)
    assert fac.settle(*_pair(spelling, request_id))["errorReason"] == "duplicate"


def test_an_unmigrated_custom_store_is_the_documented_upgrade_step():
    """Not migrated by its owner, a custom store holds only the old lower-case
    key, and settle claims only the canonical key: the README's upgrade step
    says to add the upper-case keys first. This pins that call shape."""
    store = DictStore({f"nano:live {BLOCK.lower()} req_case"})
    fac = _fac_with(store)
    fac.settle(*_pair(BLOCK.lower()))
    assert store.claimed == [f"nano:live {BLOCK.upper()} req_case"]
    assert store.asked == []


# ---------------------------------------------------- ClaimStore subclasses


def test_a_subclass_overriding_claim_is_treated_as_a_custom_store(tmp_path):
    path = str(tmp_path / "claims.db")

    class AuditedStore(ClaimStore):
        def __init__(self, p):
            super().__init__(p)
            self.claimed = []

        def claim(self, key):
            self.claimed.append(key)
            return super().claim(key)

    store = AuditedStore(path)
    fac = _fac_with(store)
    assert fac.settle(*_pair(BLOCK.lower()))["success"] is True
    assert store.claimed == [f"nano:live {BLOCK.upper()} req_case"]
    # No as-sent row: that is written only for the built-in claim().
    assert set(_rows(path)) == {f"nano:live {BLOCK.upper()} req_case"}
    for spelling in SPELLINGS:
        assert fac.settle(*_pair(spelling))["errorReason"] == "duplicate"


def test_a_subclass_with_its_own_backend_skipping_init_does_not_raise():
    class OwnBackend(ClaimStore):
        def __init__(self):  # no super().__init__: no _conn, no _lock
            self.keys = set()

        def claim(self, key):
            if key in self.keys:
                return False
            self.keys.add(key)
            return True

    store = OwnBackend()
    fac = _fac_with(store)
    assert fac.settle(*_pair("Ab" * 32))["success"] is True
    assert store.keys == {f"nano:live {BLOCK.upper()} req_case"}
    assert fac.settle(*_pair(BLOCK.lower()))["errorReason"] == "duplicate"


def test_the_builtin_store_records_the_as_sent_key_only_for_a_non_canonical_proof(tmp_path):
    path = str(tmp_path / "claims.db")
    fac = _fac_on(path)
    assert fac.settle(*_pair(BLOCK.upper(), "r1"))["success"] is True
    assert fac.settle(*_pair("Ab" * 32, "r2"))["success"] is True
    assert set(_rows(path)) == {
        f"nano:live {BLOCK.upper()} r1",
        f"nano:live {BLOCK.upper()} r2",
        f"nano:live {'Ab' * 32} r2",
    }
