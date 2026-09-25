"""Laws for where the approvals database goes when nobody says.

Every other test in this suite hands `ApprovalStore` an explicit `path`, so the default branch -
the only one production takes, through `PaymentService()` and `server_from_env()` - was never
executed by a test. It wrote `approvals.db` *inside the installed package*:

    $ pip install "nano-mcp @ git+...@v0.1.0"
    >>> ApprovalStore().path
    '/…/site-packages/nano_mcp/approvals.db'

    # the same install, run as a non-root user, which is how a hardened container runs it
    $ su probe -c '… -c "from nano_mcp.store import ApprovalStore; ApprovalStore()"'
    sqlite3.OperationalError: unable to open database file

A stranger following the README's pinned install cannot start the server, and the error names
nothing they can act on.
"""
import os

import pytest

from nano_mcp import store as store_mod
from nano_mcp.store import ApprovalStore


@pytest.fixture
def clean_env(tmp_path, monkeypatch):
    """No inherited store setting, a private HOME, and no legacy database anywhere near.

    `raising=False` on `_PACKAGE_DB` on purpose: the name does not exist on the code this law
    accuses, and the point is that the law fails there on its assertion rather than on collection.
    """
    monkeypatch.delenv("NANO_MCP_STORE", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(store_mod, "_PACKAGE_DB", str(tmp_path / "absent" / "approvals.db"),
                        raising=False)
    return tmp_path


def test_the_default_store_is_not_written_inside_the_installed_package(clean_env):
    """The defect, stated as the property it broke: an installed package's own directory is not a
    place to keep runtime state, because the process running the server usually cannot write it."""
    path = os.path.abspath(ApprovalStore().path)
    package_dir = os.path.dirname(os.path.abspath(store_mod.__file__))
    assert os.path.commonpath([path, package_dir]) != package_dir, (
        f"the approvals database defaults to {path}, inside the installed package")
    assert os.path.isdir(os.path.dirname(path)), "the directory is created, not merely named"


def test_the_default_store_is_usable_where_it_lands(clean_env):
    """Landing somewhere unwritable would only move the failure, so claim through it for real."""
    s = ApprovalStore()
    assert s.claim("req-1", "TX", "nano_addr", 1000) is True
    assert s.claim("req-1", "TX", "nano_addr", 1000) is False, "exactly-once still holds"


def test_an_operator_can_always_say_where_the_record_goes(clean_env, monkeypatch):
    chosen = str(clean_env / "chosen.db")
    monkeypatch.setenv("NANO_MCP_STORE", chosen)
    assert store_mod.default_store_path() == chosen


def test_a_deployment_already_running_keeps_its_approvals(tmp_path, monkeypatch):
    """Moving the default must not orphan a store that exists: an approval this server forgets is a
    payment that can authorize a second call, which is the one thing this table prevents."""
    monkeypatch.delenv("NANO_MCP_STORE", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    legacy = tmp_path / "pkg" / "approvals.db"
    legacy.parent.mkdir(parents=True)
    monkeypatch.setattr(store_mod, "_PACKAGE_DB", str(legacy))

    ApprovalStore(path=str(legacy)).claim("req-old", "TX", "nano_addr", 1000)
    assert store_mod.default_store_path() == str(legacy)
    assert ApprovalStore().claim("req-old", "TX", "nano_addr", 1000) is False, (
        "the old approval was forgotten, so the same payment authorizes a second call")
