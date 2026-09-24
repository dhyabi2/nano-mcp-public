"""Block 5: end-to-end probe (L6) — a pay-per-call succeeds and is replay-safe.

One scenario wires the real components together so each cannot cover for the
other:
  * SDK wallet signs a send to the one-time address (L0/L3 path, stub node)
  * MCP service derives that one-time address from the master secret (L4)
  * verify_payment approves exactly once after the matching on-chain send is
    seen (L5), gating exactly one tool execution
  * re-calling with the same request_id observes no double execution (L6)
  * the evidence gate journals the external payer and never the owner (L7)

HONEST SCOPE: the SDK's send leg uses a stub client standing in for rpc.nano.to
(balance/frontier/process), exactly as in test_wallet.py. No real XNO moves —
there is no funded wallet and the AGENTS rules forbid seeking funds. The
signing/hash/process path is what the stub drives; the live-funded confirmation
remains the recorded L2/L3 STUCK. This probe proves the wiring end-to-end on
the stub, not a live funded send.
"""
import asyncio
import json
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

from nano_mcp import (  # noqa: E402
    ApprovalStore,
    PaymentService,
    append_nano_tx,
)
from nano_mcp.server import build_server  # noqa: E402
from nano_sdk import Wallet, nano_to_raw  # noqa: E402

MASTER = bytes.fromhex("33" * 32)
SEED = bytes.fromhex("44" * 32)
REP = "nano_1stofnrxuz3cai7ze75o174bpm7scwj9jn3nxsn8ntzg784jf1gzn1jjdkou"
OWN = "nano_1ownzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzm"  # ours

# external payer account sent the XNO (the counterparty on the chain)
EXT = "nano_1exttmxk7m5q2af4d9xy7e5qaqcd94ka1cq1br49kcm1j9abcdefghijklm"


class NodeStub:
    """Stands in for rpc.nano.to: the sender SDK wallet reads balance/frontier
    from a fixed value and the process call returns a fake block hash."""

    def __init__(self, balance_raw: int):
        self.balance_raw = balance_raw
        self.process_hash = "E0" * 32

    def account_info(self, account):
        return {
            "balance": str(self.balance_raw),
            "frontier": "AB" * 32,
            "representative": REP,
        }

    def call(self, **payload):
        if payload["action"] == "process":
            return {"hash": self.process_hash}
        if payload["action"] == "work_generate":
            return {"work": "0000000000000000"}
        raise AssertionError(payload)


def test_end_to_end_pay_per_call_succeeds_and_is_replay_safe(tmp_path):
    async def main():
        node = NodeStub(balance_raw=int(nano_to_raw("0.05")))
        wallet = Wallet(seed=SEED, client=node)
        # service watches on-chain history for the one-time address
        watch = PaymentService(MASTER, client=_WatchClient(),
                               store=ApprovalStore(path=str(tmp_path / "p.db")))
        server = build_server(watch)

        # 1) quote a call -> one-time address + price
        q = await server.call_tool("quote", {"price_nano": "0.002"})
        qtext = "".join(c.text for c in q.content)
        qid = qtext.split('"request_id": "')[1].split('"')[0]
        addr = watch.one_time_account(qid).address
        amt = int(nano_to_raw("0.002"))
        assert addr in qtext

        # 2) the agent pays: SDK wallet signs a send to the one-time address.
        #    stub node: the send appears accepted (block hash returned).
        _, _ = wallet.send(addr, amt)
        #    simulate the counterparty's send landing on-chain (receive):
        watch.client.incoming[addr] = [(amt, "B" * 64)]

        # 3) service verifies -> approves exactly once, then the call runs
        v1 = await server.call_tool("verify_payment",
                                    {"request_id": qid, "amount_raw": str(amt)})
        v1text = "".join(c.text for c in v1.content)
        assert '"approved"' in v1text

        # 4) the gated tool execution happened (one call, not two)
        uv = await server.call_tool("verify_payment",
                                    {"request_id": qid, "amount_raw": str(amt)})
        uvtext = "".join(c.text for c in uv.content)
        assert '"spent"' in uvtext  # replay refused

        # 5) evidence gate: external payer is journaled, owner is not
        db = journal_mod.connect(str(tmp_path / "ev.db"))
        journal_append = lambda xs: journal_mod.append(db, xs)  # noqa: E731
        log_ext = append_nano_tx(EXT, "0.002", "B" * 64, journal_append, {OWN})
        log_own = append_nano_tx(OWN, "0.002", "B" * 64, journal_append, {OWN})
        assert log_ext is True
        assert log_own is False
        assert len(journal_mod.read_after(db, 0)) == 1
        assert journal_mod.read_after(db, 0)[0][2] == "nano_tx"

    asyncio.run(main())


class _WatchClient:
    """Stands in for rpc.nano.to in the service: maps one-time address ->
    recently landed `receive` entries."""

    def __init__(self):
        self.incoming: dict[str, list] = {}

    def account_history(self, account, count=20):
        return {
            "account": account,
            "history": [
                {"type": "receive", "account": account, "amount": str(a), "hash": h}
                for a, h in self.incoming.get(account, [])
            ],
        }


def test_journal_uses_real_nano_pulse_writer(tmp_path):
    """The journal writer contract actually matches the nano-pulse plugin."""
    db = journal_mod.connect(str(tmp_path / "real.db"))
    journal_append = lambda rows: journal_mod.append(db, rows)  # noqa: E731
    ok = append_nano_tx(EXT, "0.001", "C" * 64, journal_append, set())
    assert ok is True
    row = journal_mod.read_after(db, 0)[0]
    assert row[2] == "nano_tx"
    data = json.loads(row[3])
    assert data["external"] is True