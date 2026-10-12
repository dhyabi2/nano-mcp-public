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
    assert list(_rows(path)) == [f"nano:live {BLOCK.upper()} Req_X"]
    ClaimStore(path=path)
    assert list(_rows(path)) == [f"nano:live {BLOCK.upper()} Req_X"]
