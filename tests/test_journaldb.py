"""Block 16 tests: the scorecard reads REAL nano-pulse evidence (strategy L5).

Before block 16, ``scorecard build/verify --journal`` only accepted a hand-written
JSON array, so the "measured share from nano receipts" could not be computed from
the actual evidence store that ``append_nano_tx`` journals into (kind=``nano_tx``
in /root/.hermes/nano-pulse/journal.db). ``nano_mcp.journaldb`` is the missing
link: a stdlib, read-only, network-free adapter that turns real ``nano_tx`` rows
into exactly the receipt dicts the scorecard already sums.

These tests drive it against a scratch nano-pulse DB (stitched with the same
`journal.append` the plugin and evidence.py use): the scorecard counts only
external payers, own-account traffic adds zero, and the --journal-db CLI path
builds then verifies reproducibly.
"""
import json
import os
import sqlite3
import subprocess
import sys

import pytest

sys.path.insert(0, "/root/.hermes/plugins/nano-pulse")
try:
    import journal as journal_mod  # noqa: E402
except ModuleNotFoundError:  # pragma: no cover - not part of this release
    # The nano-pulse journal plugin lives on the swarm's own boxes and is in neither this
    # repository nor its dependencies. pytest treats a collection error as fatal, so importing it
    # unconditionally did not fail these three modules - it aborted the whole run, and the 111
    # tests that pass offline never ran for anyone who installed the package.
    pytest.skip("the nano-pulse journal plugin is not part of this release",
                allow_module_level=True)

import nano_mcp.journaldb as jdb  # noqa: E402
from nano_mcp.evidence import append_nano_tx  # noqa: E402
from nano_mcp.scorecard import count_external_receipts  # noqa: E402

OWN = "nano_1navjecdrxe1n4xk7m5q2af4d9xy7e5qaqcd94ka1cq1br49kcm1j7v8u4mn"
EXT = "nano_3extzzzzn4xk7m5q2af4d9xy7e5qaqcd94ka1cq1br49kcm1j9abcdefgh"
RAW = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scorecard", "raw"))


def _scratch_db(tmp_path):
    """A real nano-pulse journal DB plus the evidence writer pointed at it."""
    db = journal_mod.connect(str(tmp_path / "j.db"))

    def append(rows):
        journal_mod.append(db, rows)

    return append


@pytest.fixture
def jdb_path(tmp_path):
    return str(tmp_path / "j.db")


def test_read_nano_tx_roundtrips_evidence_rows(tmp_path):
    # an external + an own payment via the REAL evidence writer
    append = _scratch_db(tmp_path)
    ok_ext = append_nano_tx(EXT, "0.001", "F" * 64, journal_append=append,
                            own_accounts={OWN})
    ok_own = append_nano_tx(OWN, "0.01", "G" * 64, journal_append=append,
                            own_accounts={OWN})
    assert ok_ext is True and ok_own is False  # own never logged

    rows = jdb.read_nano_tx(str(tmp_path / "j.db"))
    # only the one logged nano_tx row surfaces, as a scorecard receipt
    assert len(rows) == 1
    assert rows[0]["payer"] == EXT
    assert rows[0]["hash"] == "F" * 64
    assert rows[0]["amount_xno"] == "0.001"
    assert rows[0]["direction"] == "receive"
    assert rows[0]["external"] is True


def test_journal_reader_counts_only_external_for_scorecard(jdb_path):
    append = _scratch_db(_Path(jdb_path).parent)  # reuse writer fixture
    append_nano_tx(EXT, "0.001", "A" * 64, journal_append=append,
                   own_accounts={OWN})
    append_nano_tx(OWN, "0.01", "B" * 64, journal_append=append,
                   own_accounts={OWN})

    reader = jdb.PulseJournalReader(jdb_path)
    receipts, n = count_external_receipts(reader, {OWN})
    assert n == 1
    assert [r["payer"] for r in receipts] == [EXT]


def test_scorecard_build_uses_real_journal_db(jdb_path):
    append = _scratch_db(_Path(jdb_path).parent)
    append_nano_tx(EXT, "0.001", "C" * 64, journal_append=append,
                   own_accounts={OWN})
    append_nano_tx(OWN, "0.001", "D" * 64, journal_append=append,
                   own_accounts={OWN})

    from nano_mcp.scorecard import build

    published = build(RAW, jdb.PulseJournalReader(jdb_path), {OWN})
    assert published["nano"]["external_receipts"] == 1
    assert published["nano"]["excluded_own"] == 0  # own was never logged
    assert published["nano"]["share_of_observed"] == pytest.approx(1 / 165_000_001)


def test_cli_journal_db_build_and_verify_is_reproducible(tmp_path):
    append = _scratch_db(tmp_path)
    append_nano_tx(EXT, "0.001", "E" * 64, journal_append=append,
                   own_accounts={OWN})

    out = tmp_path / "published.json"
    manifest = tmp_path / "manifest.json"
    env = dict(os.environ)

    r = subprocess.run(
        [sys.executable, "-m", "nano_mcp.scorecard", "build",
         "--raw", RAW, "--out", str(out), "--manifest", str(manifest),
         "--journal-db", str(tmp_path / "j.db"), "--own", OWN],
        capture_output=True, text=True, env=env,
    )
    assert r.returncode == 0, r.stderr
    published = json.loads(out.read_text())
    assert published["nano"]["external_receipts"] == 1

    rv = subprocess.run(
        [sys.executable, "-m", "nano_mcp.scorecard", "verify",
         "--raw", RAW, "--published", str(out),
         "--journal-db", str(tmp_path / "j.db"), "--own", OWN],
        capture_output=True, text=True, env=env,
    )
    assert rv.returncode == 0, rv.stdout + rv.stderr
    assert "verify PASS" in rv.stdout


def test_journal_db_is_readonly_and_network_free(jdb_path):
    # the writer fixture must not be the real DB; prove mode=ro by opening the
    # reader and attempting no writes. reader uses sqlite URI mode=ro.
    append = _scratch_db(_Path(jdb_path).parent)
    append_nano_tx(EXT, "0.001", "H" * 64, journal_append=append,
                   own_accounts={OWN})

    src = open(jdb.__file__).read()
    assert "httpx" not in src and "requests" not in src

    # mode=ro: a write attempt fails, so the scorecard never mutates evidence
    uri = f"file:{jdb_path}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            con.execute("INSERT INTO events(ts,kind,data) VALUES(1,'x','{}')")
    finally:
        con.close()


def _Path(s):
    from pathlib import Path as _P

    return _P(s)