"""The server must start, and list its tools, with NO configuration at all.

Every MCP directory discovers a server the same way: install the package from a
clean clone, start it with an empty environment, send `tools/list`, read the
answer. `server_from_env` used to raise

    RuntimeError: NANO_PAYMENT_MASTER_SECRET must be set (32+ bytes hex)

before building anything, so that discovery never got as far as a tool list and
the server read as broken to the directory and to any agent host trying it out.
This is the gate on the Glama listing that `awesome-mcp-servers` asks for, and
the first thing an agent hits when it installs the package to look at it.

The fix is not a generated fallback secret. A throwaway key would derive payment
addresses whose private keys die with the process: XNO sent to one is
unrecoverable, and the loss would land on whoever paid. So an unconfigured server
lists and describes every tool, serves the read-only ones, and refuses exactly
the ones that must derive an address -- naming the variable to set.
"""
from __future__ import annotations

import asyncio

import pytest

from mcp.server.mcpserver.exceptions import ToolError

from nano_mcp.server import build_server, server_from_env
from nano_mcp.service import MasterSecretNotConfigured, PaymentService

# The tools an agent needs to see before it will keep the server.
EXPECTED_TOOLS = {
    "get_address",
    "get_balance",
    "get_history",
    "quote",
    "quote_usd",
    "verify_payment",
    "pay_and_call",
}


def test_server_from_env_starts_with_no_environment(monkeypatch):
    """No NANO_PAYMENT_MASTER_SECRET: the server is still built."""
    monkeypatch.delenv("NANO_PAYMENT_MASTER_SECRET", raising=False)
    server = server_from_env()
    assert server.name == "nano-mcp"


def test_tools_list_answers_without_a_secret(monkeypatch):
    """The exact question a directory asks: tools/list on an unconfigured server."""
    monkeypatch.delenv("NANO_PAYMENT_MASTER_SECRET", raising=False)
    server = server_from_env()
    tools = asyncio.run(server.list_tools())
    names = {t.name for t in tools}
    assert EXPECTED_TOOLS <= names, f"missing from tools/list: {EXPECTED_TOOLS - names}"
    # A directory renders these; an empty description is what gets a listing rejected.
    for tool in tools:
        assert tool.description, f"{tool.name} has no description"


def test_deriving_a_payment_address_refuses_and_names_the_variable():
    """Unconfigured, the address-deriving path fails loudly, not silently."""
    pay = PaymentService(master_secret=None)
    with pytest.raises(MasterSecretNotConfigured) as excinfo:
        pay.one_time_account("req-1")
    assert "NANO_PAYMENT_MASTER_SECRET" in str(excinfo.value)


def test_no_stand_in_secret_is_invented():
    """The unconfigured service holds no key -- it does not mint one to get by.

    If this ever starts failing because a default appeared, read the module
    docstring above before "fixing" the test: a generated secret means payments
    to addresses nobody can ever spend from.
    """
    monkeypatch_free = PaymentService(master_secret=None)
    assert monkeypatch_free.master_secret is None


def test_an_unconfigured_quote_tool_call_says_what_to_set():
    """Through the real MCP tool surface, and the message must survive the trip.

    MCPServer withholds the text of any error that is not a ToolError, so without
    the translation in `server.py` the agent reads only "Error executing tool
    quote" and has no way to learn that one environment variable is missing.
    """
    server = build_server(PaymentService(master_secret=None))

    async def call():
        return await server.call_tool("quote", {"price_nano": "0.001"})

    with pytest.raises(ToolError) as excinfo:
        asyncio.run(call())
    assert "NANO_PAYMENT_MASTER_SECRET" in str(excinfo.value)


def test_a_configured_server_still_quotes():
    """The guard must not have broken the configured path."""
    pay = PaymentService(master_secret=bytes.fromhex("33" * 32))
    acct = pay.one_time_account("req-1")
    assert acct.address.startswith("nano_")
    # Deterministic for the same request_id, which is what binds a payment to a call.
    assert pay.one_time_account("req-1").address == acct.address
    assert pay.one_time_account("req-2").address != acct.address
