"""verify_payment and pay_and_call must refuse an amount that is not plain raw digits.

Both MCP tools read `amount_raw` with `int(amount_raw)`. `int` reads "-1",
"+1", " 1", "1_000" and Arabic-Indic digits, and `verify_payment` approves an
on-chain receive of at least that many raw. For a request_id this server never
quoted, "-1" is therefore satisfied by any receive at all, of any size - the
same class as the facilitator reading "-0.5" as a positive amount.

An amount here is a string of ASCII digits: a whole number of raw.
"""
import asyncio
import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from nano_mcp.server import build_server
from nano_mcp.service import PaymentService
from nano_mcp.store import ApprovalStore

MASTER = bytes.fromhex("33" * 32)

NOT_PLAIN = ["-1", "-0", "+1", " 1", "1 ", "1_000", "١", "1.0", "1e3", ""]


class OneReceiveClient:
    """A node that shows one 500 raw receive on every account it is asked about."""

    def account_history(self, account: str, count: int = 20):
        return {
            "account": account,
            "history": [{"type": "receive", "account": account, "amount": "500", "hash": "DEADBEEF"}],
        }

    def account_balance(self, account: str) -> dict:
        return {"balance": "500"}


def _server(tmp_path):
    pay = PaymentService(MASTER, OneReceiveClient(), ApprovalStore(path=str(tmp_path / "a.db")))
    return build_server(pay)


async def _call(server, tool, args) -> dict:
    res = await server.call_tool(tool, args)
    return json.loads("".join(c.text for c in res.content))


@pytest.mark.parametrize("amount", NOT_PLAIN, ids=repr)
def test_verify_payment_refuses_an_amount_that_is_not_plain_digits(tmp_path, amount):
    async def main():
        server = _server(tmp_path)
        with pytest.raises(ToolError, match="amount_raw"):
            await _call(server, "verify_payment", {"request_id": "unquoted", "amount_raw": amount})

    asyncio.run(main())


@pytest.mark.parametrize("amount", NOT_PLAIN, ids=repr)
def test_pay_and_call_refuses_an_amount_that_is_not_plain_digits(tmp_path, amount):
    async def main():
        server = _server(tmp_path)
        with pytest.raises(ToolError, match="amount_raw"):
            await _call(server, "pay_and_call", {"request_id": "r", "amount_raw": amount, "tool": "x"})

    asyncio.run(main())


def test_the_service_refuses_a_negative_amount(tmp_path):
    pay = PaymentService(MASTER, OneReceiveClient(), ApprovalStore(path=str(tmp_path / "s.db")))
    with pytest.raises(ValueError):
        pay.verify_payment("unquoted", -1, require_onchain=True)
    assert pay.store.get("unquoted") is None


# ---- controls: a plain amount is still checked against the chain ----


def test_a_plain_amount_is_still_verified_on_chain(tmp_path):
    async def main():
        server = _server(tmp_path)
        ok = await _call(server, "verify_payment", {"request_id": "r1", "amount_raw": "500"})
        assert ok["status"] == "approved", ok
        short = await _call(server, "verify_payment", {"request_id": "r2", "amount_raw": "501"})
        assert short["status"] == "pending", short

    asyncio.run(main())
