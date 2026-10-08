"""verify_payment must not believe the payer's own numbers.

`verify_payment` is an MCP tool, and in the pay-per-call flow the agent calling
it is the one that owes the money -- the `quote` and `quote_usd` docstrings tell
a buyer to pay `price_raw` and "then call verify_payment(request_id, price_raw)".
This is the published package, the one people `pip install`. Two of its
arguments were taken on trust:

  * `require_onchain` -- a payer passing false was approved with tx_hash
    "simulated" and the node was never asked anything at all;
  * `amount_raw` -- the amount its own payment is checked against, so a payer
    could be quoted 1 XNO, send 1 raw, and ask to be verified against 1 raw.

Both are driven here through the real MCPServer tool surface, against a node
that shows exactly what the payer really sent and nothing more.
"""
import asyncio
import json

import pytest

from nano_mcp.server import build_server
from nano_mcp.service import PaymentService
from nano_mcp.store import ApprovalStore

MASTER = bytes.fromhex("22" * 32)

ONE_XNO_RAW = 10**30


class RecordingClient:
    """A node that reports only the receives it is told about, and records every
    question it is asked, so a verification that never looked is visible."""

    def __init__(self, incoming: dict[str, str] | None = None):
        self.incoming = incoming or {}
        self.asked: list[str] = []

    def account_history(self, account: str, count: int = 20):
        self.asked.append("account_history")
        amount = self.incoming.get(account)
        history = (
            [{"type": "receive", "account": account, "amount": amount, "hash": "DEADBEEF"}]
            if amount is not None
            else []
        )
        return {"account": account, "history": history}

    def account_balance(self, account: str) -> dict:
        return {"balance": "0"}


def _server(tmp_path, client, name="m.db"):
    pay = PaymentService(MASTER, client, ApprovalStore(path=str(tmp_path / name)))
    return build_server(pay)


async def _call(server, tool, args) -> dict:
    res = await server.call_tool(tool, args)
    return json.loads("".join(c.text for c in res.content))


def test_the_tool_surface_offers_no_way_to_skip_the_onchain_check(tmp_path):
    """A payer must not be able to turn the on-chain check off over the wire."""

    async def main():
        client = RecordingClient()  # no payment to anyone, ever
        server = _server(tmp_path, client)

        q = await _call(server, "quote", {"price_nano": "1.0", "request_id": "r1"})
        assert int(q["price_raw"]) == ONE_XNO_RAW

        tool = next(t for t in await server.list_tools() if t.name == "verify_payment")
        params = (tool.input_schema or {}).get("properties", {})
        assert "require_onchain" not in params, (
            "verify_payment still accepts require_onchain from its caller; a payer "
            f"passing false is approved without paying. parameters: {sorted(params)}"
        )

        # And belt-and-braces: passing it anyway must not produce an approval.
        try:
            out = await _call(
                server,
                "verify_payment",
                {"request_id": "r1", "amount_raw": str(ONE_XNO_RAW), "require_onchain": False},
            )
        except Exception:
            return  # rejected outright: also fine, nothing was approved
        assert out["status"] != "approved", (
            f"approved with no payment at all: {out}; node was asked {client.asked}"
        )

    asyncio.run(main())


def test_a_payer_cannot_name_the_amount_its_own_payment_is_checked_against(tmp_path):
    """Quoted 1 XNO, sent 1 raw, asked to be verified against 1 raw."""

    async def main():
        server = _server(tmp_path, RecordingClient(), "pending.db")
        q = await _call(server, "quote", {"price_nano": "1.0", "request_id": "r2"})
        address = q["address"]
        assert int(q["price_raw"]) == ONE_XNO_RAW

        # The payer really did send 1 raw -- and only 1 raw -- to that address.
        underpaid = _server(tmp_path, RecordingClient({address: "1"}), "pending.db")
        out = await _call(underpaid, "verify_payment", {"request_id": "r2", "amount_raw": "1"})

        assert out["status"] != "approved", (
            "a 1 XNO call was approved for 1 raw -- 10**-30 of the quote -- because "
            f"verify_payment checked the amount the payer named: {out}"
        )
        assert out["status"] == "pending", out

    asyncio.run(main())


def test_pay_and_call_cannot_quote_the_payer_back_down(tmp_path):
    """`pay_and_call` re-quotes at an amount its caller names, so the recorded
    floor must never fall -- otherwise the payer lowers its own price and is then
    verified against the lower one."""

    async def main():
        server = _server(tmp_path, RecordingClient(), "floor.db")
        q = await _call(server, "quote", {"price_nano": "1.0", "request_id": "r3"})
        address = q["address"]

        # The payer re-quotes itself down to 1 raw through pay_and_call...
        await _call(server, "pay_and_call", {"request_id": "r3", "amount_raw": "1", "tool": "x"})
        # ...sends exactly that...
        paid = _server(tmp_path, RecordingClient({address: "1"}), "floor.db")
        out = await _call(paid, "verify_payment", {"request_id": "r3", "amount_raw": "1"})

        assert out["status"] != "approved", (
            f"the payer re-quoted itself down to 1 raw and was approved for it: {out}"
        )

    asyncio.run(main())


# ---- controls: the refusal must not cost an honest payment ----


def test_the_quoted_amount_paid_in_full_is_still_approved(tmp_path):
    async def main():
        server = _server(tmp_path, RecordingClient(), "ok.db")
        q = await _call(server, "quote", {"price_nano": "1.0", "request_id": "r4"})
        address = q["address"]

        honest = _server(tmp_path, RecordingClient({address: str(ONE_XNO_RAW)}), "ok.db")
        out = await _call(
            honest, "verify_payment", {"request_id": "r4", "amount_raw": q["price_raw"]}
        )
        assert out["status"] == "approved", out
        assert out["tx_hash"] == "DEADBEEF"

        # still exactly once
        again = await _call(
            honest, "verify_payment", {"request_id": "r4", "amount_raw": q["price_raw"]}
        )
        assert again["status"] == "spent", again

    asyncio.run(main())


def test_a_caller_asking_for_more_than_was_quoted_still_gets_the_stricter_check(tmp_path):
    """The floor only rises. A seller demanding more than it quoted is unchanged."""

    async def main():
        server = _server(tmp_path, RecordingClient(), "strict.db")
        q = await _call(server, "quote", {"price_nano": "1.0", "request_id": "r5"})
        address = q["address"]

        # exactly the quote arrived, but this caller demands double
        paid = _server(tmp_path, RecordingClient({address: str(ONE_XNO_RAW)}), "strict.db")
        out = await _call(
            paid, "verify_payment", {"request_id": "r5", "amount_raw": str(2 * ONE_XNO_RAW)}
        )
        assert out["status"] == "pending", out

    asyncio.run(main())


def test_a_request_id_this_server_never_quoted_for_is_unchanged(tmp_path):
    """get_address hands out an address without quoting a price; that path keeps
    checking the amount the caller names, which is all there is to check."""

    async def main():
        server = _server(tmp_path, RecordingClient(), "noquote.db")
        res = await server.call_tool("get_address", {"request_id": "r6"})
        address = "".join(c.text for c in res.content).strip().strip('"')

        paid = _server(tmp_path, RecordingClient({address: "500"}), "noquote.db")
        out = await _call(paid, "verify_payment", {"request_id": "r6", "amount_raw": "500"})
        assert out["status"] == "approved", out

    asyncio.run(main())


def test_a_usd_quote_is_recorded_as_a_floor_too(tmp_path):
    from decimal import Decimal

    async def main():
        client = RecordingClient()
        pay = PaymentService(
            MASTER,
            client,
            ApprovalStore(path=str(tmp_path / "usd.db")),
            rate_source=lambda: Decimal("0.34"),
        )
        server = build_server(pay)
        q = await _call(server, "quote_usd", {"price_usd": "1.00", "request_id": "r7"})
        quoted = int(q["price_raw"])
        assert quoted > 0

        underpaid = RecordingClient({q["address"]: "1"})
        pay2 = PaymentService(
            MASTER,
            underpaid,
            ApprovalStore(path=str(tmp_path / "usd.db")),
            rate_source=lambda: Decimal("0.34"),
        )
        out = await _call(build_server(pay2), "verify_payment", {"request_id": "r7", "amount_raw": "1"})
        assert out["status"] != "approved", (
            f"a dollar-priced call was approved for 1 raw: {out}"
        )

    asyncio.run(main())
