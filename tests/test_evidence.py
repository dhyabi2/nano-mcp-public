"""Block 5 tests: evidence gating (L7) — external payments are journaled as
nano_tx while accounts we control are never logged.

`append_nano_tx` is the gate. It needs no live node: `should_log` decides from
NANO_AGENT_OWN_ACCOUNTS, and the writer is injected so a scratch journal db
stands in for /root/.hermes/plugins/nano-pulse/journal.py without touching the
real journal.
"""
import json
import os
import sys
import time

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

from nano_mcp import append_nano_tx, should_log  # noqa: E402

OWN = "nano_1navjecdrxe1n4xk7m5q2af4d9xy7e5qaqcd94ka1cq1br49kcm1j7v8u4mn"
EXT = "nano_3extzzzzn4xk7m5q2af4d9xy7e5qaqcd94ka1cq1br49kcm1j9abcdefgh"


def _scratch_journal(tmp_path):
    """A nano-pulse-compatible writer pointed at a scratch db."""
    db = journal_mod.connect(str(tmp_path / "j.db"))

    def append(rows):
        journal_mod.append(db, rows)

    return db, append


def _count_tx(db):
    return len(journal_mod.read_after(db, 0))


def test_should_log_external_not_own():
    own = {OWN}
    assert should_log(EXT, own) is True
    assert should_log(OWN, own) is False


def test_append_logs_external_once(tmp_path):
    db, append = _scratch_journal(tmp_path)
    ok = append_nano_tx(
        payer=EXT,
        amount_xno="0.001",
        block_hash="F" * 64,
        journal_append=append,
        own_accounts={OWN},
    )
    assert ok is True
    assert _count_tx(db) == 1
    row = journal_mod.read_after(db, 0)[0]
    assert row[2] == "nano_tx"
    data = json.loads(row[3])
    assert data["external"] is True
    assert data["direction"] == "receive"
    assert data["amount_xno"] == "0.001"


def test_append_never_logs_own_account(tmp_path):
    db, append = _scratch_journal(tmp_path)
    ok = append_nano_tx(
        payer=OWN,
        amount_xno="0.001",
        block_hash="F" * 64,
        journal_append=append,
        own_accounts={OWN},
    )
    assert ok is False
    assert _count_tx(db) == 0


def test_own_accounts_from_env_reads_comma_and_space():
    os.environ["NANO_AGENT_OWN_ACCOUNTS"] = "nano_a nano_b, nano_c"
    try:
        from nano_mcp import own_accounts_from_env

        s = own_accounts_from_env()
        assert "nano_a" in s and "nano_b" in s and "nano_c" in s
    finally:
        os.environ.pop("NANO_AGENT_OWN_ACCOUNTS", None)