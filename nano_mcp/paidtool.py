"""MCP `paidTool` wrapper for the x402 `exact`-on-`nano` scheme (roadmap stage 1).

Blocks 10-14 built the HTTP half of the protocol: the spec + TS reference
implementation, the self-hostable facilitator (/supported /verify /settle,
fail-closed on >=2 independent Nano RPCs, atomic single-use claim), and the HTTP
402 Resource Server + wire client (httpx402.py): first GET returns 402 + a
`payment-required` header with a one-time nano_ payTo and exact amount; the
client retries with a `payment-signature` (PaymentPayload {paymentProof 64-hex});
the server verifies then settles and serves the protected result 200 +
`payment-response`; a spent/replayed proof is refused.

Roadmap stage 1 also requires an **MCP `paidTool` wrapper**: expose the same
handshake as MCP tools so an MCP *client agent* (not raw HTTP) can request a
protected paid tool, be told the one-time payTo and exact amount, send XNO, then
receive the protected result exactly once. This module is that wrapper. It does
NOT reimplement the protocol — it reuses `ResourceApp` + `Facilitator` from
httpx402.py/facilitator.py, so the exact same verify (>=2 RPC fail-closed) and
settle (atomic single-use claim) logic runs whether the handshake arrives over
HTTP or over MCP.

Two MCP tools (mirroring the HTTP 402 -> pay -> verify -> settle -> 200 flow):

  paid_tool_request(tool, params?) -> issues a fresh one-time requirement via
      ResourceApp.begin(); returns {tool, request_id, pay_to, amount_raw,
      scheme, network, asset}. Stateless: no resource-side state is stored;
      the one-time payTo is derived deterministically from (master, request_id).
  paid_tool_execute(tool, request_id, payment_proof) -> re-derives the exact
      requirements the server would issue for that request_id, builds the
      PaymentPayload, and runs ResourceApp.complete() (verify then settle). On
      settlement success it runs the protected tool and returns its result;
      a spent/replayed proof is refused (facilitator atomic claim).

The wrapper never moves, holds or guards funds: the agent sends XNO itself to
the one-time payTo from its own wallet; 'settlement' is binding the on-chain
proof exactly once. All money math and proof binding live in the facilitator.
"""
from __future__ import annotations

from typing import Callable

from mcp.server.mcpserver import MCPServer

from nano_mcp.facilitator import Facilitator
from nano_mcp.httpx402 import ResourceApp, build_payment_payload, derive_requirements


class PaidToolServer:
    """MCP-side paidTool wrapper: request a paid tool, then execute it once.

    Built purely from ResourceApp + Facilitator (the same components the HTTP
    resource server uses), so the 402 handshake is exercised, not reimplemented.
    """

    def __init__(
        self,
        master_secret: bytes,
        facilitator: Facilitator,
        amount_raw: str,
        tools: dict[str, Callable[[dict], dict]],
        max_timeout: int = 60,
        resource: dict | None = None,
    ):
        self._app = ResourceApp(
            master_secret=master_secret,
            facilitator=facilitator,
            amount_raw=amount_raw,
            resource=resource or {
                "url": "https://localhost/mcp-paid-tools",
                "description": "Protected paid MCP tools",
                "mimeType": "application/json",
            },
            max_timeout=max_timeout,
        )
        self._tools = tools

    # -- MCP tool 1: issue a one-time payment requirement -------------------
    def request(self, tool: str) -> dict:
        """Issue a fresh one-time nano requirement for `tool`.

        Stateless mirror of the HTTP 402: returns the one-time payTo, the exact
        raw amount, and the request_id that names them. The agent then sends
        exactly amount_raw to pay_to from its own wallet.
        """
        started = self._app.begin()
        req = started["requirements"]
        return {
            "tool": tool,
            "request_id": started["request_id"],
            "pay_to": req["payTo"],
            "amount_raw": req["amount"],
            "scheme": req["scheme"],
            "network": req["network"],
            "asset": req["asset"],
        }

    # -- MCP tool 2: verify + settle + run the protected tool ----------------
    def execute(self, tool: str, request_id: str, payment_proof: str) -> dict:
        """Verify + settle the presented proof for request_id, then run `tool`.

        Re-derives the exact requirements the server would issue for this
        request_id, builds the PaymentPayload, and runs ResourceApp.complete()
        (verify on >=2 independent RPCs fail-closed, then atomic settle). On
        settlement success the protected tool runs once and its result is
        returned; a spent/replayed proof is refused.
        """
        requirements = derive_requirements(
            self._app.master_secret, request_id, self._app.amount_raw, self._app.max_timeout
        )
        try:
            payload = build_payment_payload(requirements, payment_proof)
        except ValueError as err:  # not a 64-hex block hash
            return {
                "success": False,
                "ok": False,
                "error_reason": "invalid",
                "error_message": str(err),
            }
        result = self._app.complete(payload)
        if not result.get("ok"):
            return {
                "success": False,
                "ok": False,
                "error_reason": result.get("error_reason", "unverified"),
                "error_message": result.get("error_message", "payment could not be served"),
            }
        handler = self._tools.get(tool)
        body = handler(self._app.resource) if handler else {"result": "ok"}
        return {
            "success": True,
            "ok": True,
            "result": body,
            "settlement": result["settlement"],
        }


def build_paid_server(
    paid: PaidToolServer,
    title: str = "nano-mcp paid tools (x402 exact-on-nano)",
) -> MCPServer:
    """Build the MCPServer exposing the two paidTool wrapper tools (mcp v2)."""
    server = MCPServer(
        name="nano-mcp-paid",
        title=title,
        version="0.1.0",
        description=(
            "Paid MCP tools in Nano (XNO) via the x402 exact-on-nano scheme: "
            "paid_tool_request issues a one-time nano_ payTo and exact amount for "
            "a named tool; pay that address from your own wallet; then "
            "paid_tool_execute verifies the send on two independent RPCs, settles "
            "it exactly once, and returns the protected tool result. Feeless, "
            "sub-second finality, no issuer."
        ),
    )

    @server.tool()
    def paid_tool_request(tool: str, params: str | None = None) -> dict:  # noqa: ARG001
        """Request a paid tool: returns {tool, request_id, pay_to, amount_raw,
        scheme, network, asset}. Send exactly amount_raw raw XNO to pay_to from
        your own wallet, then call paid_tool_execute(tool, request_id, <64-hex
        block hash>) to get the protected result."""
        return paid.request(tool)

    @server.tool()
    def paid_tool_execute(tool: str, request_id: str, payment_proof: str) -> dict:
        """Verify + settle the 64-hex payment proof for request_id, then run
        `tool` and return its protected result exactly once. A spent/replayed
        proof is refused."""
        return paid.execute(tool, request_id, payment_proof)

    return server


__all__ = ["PaidToolServer", "build_paid_server"]
