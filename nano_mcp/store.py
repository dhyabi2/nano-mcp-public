"""Exactly-once approval store for the nano-mcp pay-per-call server.

The brainstorm (see .ledger/PLAN.md block 4) converged on using a SQLite-backed
`INSERT ... PRIMARY KEY` as a *claiming lock*: approving a request_id is an
atomic insert that succeeds exactly once, so the same payment can never authorize
two calls, even across server restarts or under concurrency. A payment whose
request has already been approved is refused (replay-safe).

The on-chain check itself (does a matching send to the one-time address exist?)
lives in the server's verify step; this store *records* the approval once.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import closing

SCHEMA = """
CREATE TABLE IF NOT EXISTS approvals (
    request_id  TEXT PRIMARY KEY,
    tx_hash     TEXT,
    status      TEXT NOT NULL,
    paid_addr   TEXT,
    amount_raw  TEXT,
    approved_at REAL
);
CREATE TABLE IF NOT EXISTS quote_expiry (
    request_id  TEXT PRIMARY KEY,
    expires_at  REAL NOT NULL
);
"""


# Where the store used to default to: beside the installed package. Kept as a name so an existing
# deployment can still be found, and so the law that this is no longer the default can point at it.
_PACKAGE_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "approvals.db")


def default_store_path() -> str:
    """Where the approvals database lives when the caller does not name a path.

    It used to be `_PACKAGE_DB` - inside the installed package - which is the one directory an
    installed application must not write to. On a system-wide install, a distro package, or a
    container image built as root and run as a non-root user (the ordinary hardened pattern),
    site-packages is not writable by the process, so constructing the store raised

        sqlite3.OperationalError: unable to open database file

    and the MCP server could not start at all. Nothing in the suite saw it: every test passes an
    explicit `path`, so this branch was only ever taken in production.

    Order of precedence, and why:
      1. NANO_MCP_STORE, so an operator can always say where the record goes;
      2. an existing `_PACKAGE_DB`, so a deployment already running keeps its approvals - losing
         them would make every already-approved request_id claimable a second time, which is the
         one property this store exists to provide;
      3. $XDG_STATE_HOME/nano-mcp (else ~/.local/state/nano-mcp), which is writable by the user the
         server runs as and survives reinstalling the package.
    """
    env = os.environ.get("NANO_MCP_STORE")
    if env:
        return env
    if os.path.exists(_PACKAGE_DB):
        return _PACKAGE_DB
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    directory = os.path.join(base, "nano-mcp")
    os.makedirs(directory, exist_ok=True)
    return os.path.join(directory, "approvals.db")


class ApprovalStore:
    """Persistent exactly-once record of approved request_ids."""

    def __init__(self, path: str | None = None):
        self.path = path or default_store_path()
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def _row(self, request_id: str) -> tuple | None:
        # A single shared sqlite connection is used with check_same_thread=False;
        # every access (reads included) must serialize on the same lock as writes
        # or concurrent verify/claim traffic races on the connection and raises
        # sqlite3.InterfaceError.
        with self._lock, closing(self._conn.execute(
            "SELECT tx_hash, status, paid_addr, amount_raw, approved_at FROM approvals WHERE request_id=?",
            (request_id,),
        )) as cur:
            return cur.fetchone()

    def is_approved(self, request_id: str) -> bool:
        row = self._row(request_id)
        return row is not None and row[1] == "approved"

    def claim(
        self,
        request_id: str,
        tx_hash: str,
        paid_addr: str,
        amount_raw: int,
    ) -> bool:
        """Atomically mark request_id as approved. Returns True only on the first
        (and only) success; returns False if it was already approved."""
        with self._lock:
            try:
                with closing(self._conn.execute(
                    "INSERT INTO approvals (request_id, tx_hash, status, paid_addr, amount_raw, approved_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (request_id, tx_hash, "approved", paid_addr, str(amount_raw), __import__("time").time()),
                )) as cur:
                    ...
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                # already claimed -> replay refused
                return False

    def get(self, request_id: str) -> dict | None:
        row = self._row(request_id)
        if row is None:
            return None
        return {
            "tx_hash": row[0],
            "status": row[1],
            "paid_addr": row[2],
            "amount_raw": int(row[3]) if row[3] else 0,
            "approved_at": row[4],
        }

    # ---- dollar-quote expiry record (block 6) ----
    def record_quote_expiry(self, request_id: str, expires_at: float) -> None:
        """Remember when a dollar quote for request_id expires so verify_payment
        can refuse a payment made after the 30s honour window."""
        with self._lock, closing(self._conn.execute(
            "INSERT INTO quote_expiry (request_id, expires_at) VALUES (?,?) "
            "ON CONFLICT(request_id) DO UPDATE SET expires_at=excluded.expires_at",
            (request_id, expires_at),
        )):
            ...
        self._conn.commit()

    def quote_expiry(self, request_id: str) -> float | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT expires_at FROM quote_expiry WHERE request_id=?", (request_id,)
            ).fetchone()
        return row[0] if row else None
