"""MCP server exposing the nano-mcp pay-per-call tools.

Tools (all stdio MCPServer, mcp v2 API where FastMCP was renamed MCPServer):

  get_address       -> the derived one-time address for a request_id (or a fresh quote)
  get_balance       -> balance of a nano account (via rpc.nano.to)
  get_history       -> account history of a nano account
  quote             -> {request_id, address, price_raw} one-time payment address for a call
  pay_and_call      -> agent side: send price to the quoted address, then call a tool (stub)
  verify_payment    -> service side: approve the call once the matching on-chain send is seen

The server is constructed from a PaymentService; the master secret comes from
the NANO_PAYMENT_MASTER_SECRET env var (seeds/keys stay out of repos).
"""
from __future__ import annotations

import os

from mcp.server.mcpserver import MCPServer

from nano_sdk.client import RpcClient
from nano_sdk.units import nano_to_raw

from .service import PaymentService


def server_from_env(
    master_secret: bytes | None = None,
    client: RpcClient | None = None,
) -> MCPServer:
    secret = master_secret or os.environ.get("NANO_PAYMENT_MASTER_SECRET")
    if secret is None:
        raise RuntimeError("NANO_PAYMENT_MASTER_SECRET must be set (32+ bytes hex)")
    if isinstance(secret, str):
        secret = bytes.fromhex(secret)
    return build_server(PaymentService(master_secret=secret, client=client))


def build_server(pay: PaymentService, title: str = "nano-mcp pay-per-call") -> MCPServer:
    server = MCPServer(
        name="nano-mcp",
        title=title,
        version="0.1.0",
        description=(
            "Pay-per-call in Nano (XNO): a service derives a one-time payment address "
            "per request, the agent sends exact XNO there, and the call is approved "
            "once the matching on-chain send is confirmed. Feeless, instant, no issuer."
        ),
    )
    srv = pay

    @server.tool()
    def get_address(request_id: str | None = None) -> str:
        """Return the one-time Nano payment address for request_id (or mint a fresh)."""
        if request_id:
            acct = srv.one_time_account(request_id)
            return acct.address
        return srv.quote(price_raw=0).address

    @server.tool()
    def get_balance(account: str) -> str:
        """Return the raw balance of a nano_ account via rpc.nano.to."""
        b = srv.client.account_balance(account)
        return b.get("balance", "0")

    @server.tool()
    def get_history(account: str, count: int = 10) -> list:
        """Return the last `count` on-chain entries for a nano_ account."""
        return list(srv.client.account_history(account, count=count).get("history", []))

    @server.tool()
    def quote(price_nano: str, request_id: str | None = None) -> dict:
        """Price a call in Nano. Returns {request_id, address, price_raw, price_nano}
        where `address` is the ONE-TIME payment address for this request. Send the
        exact amount there, then call verify_payment(request_id, amount_raw)."""
        price_raw = nano_to_raw(price_nano)
        q = srv.quote(price_raw=price_raw, request_id=request_id)
        return q.as_dict()

    @server.tool()
    def quote_usd(price_usd: str, request_id: str | None = None) -> dict:
        """Price a call in dollars. Converts the USD price to the exact XNO amount
        via the MEDIAN of three independent public price sources and returns
        {request_id, address, price_raw, price_usd, rate_xno_usd, expires_at}.
        The quote expires in <=30s; pay the exact price_raw to `address` before
        that, then call verify_payment(request_id, price_raw). This is pure
        computation — nothing is held or converted."""
        q = srv.quote_usd(price_usd, request_id=request_id)
        return q.as_dict()

    @server.tool()
    def verify_payment(request_id: str, amount_raw: str) -> dict:
        """Service side: approve the call for request_id once an on-chain send of at
        least amount_raw is confirmed to its one-time address. Approves exactly once;
        a repeat returns status='spent'.

        `amount_raw` can only make the check STRICTER: the amount actually required
        is the higher of it and what this server quoted for request_id.

        There is deliberately no `require_onchain` argument here. The service
        method takes one so in-process tests can run without a node, but over the
        tool surface the caller is the payer, and a payer that can pass
        require_onchain=false is approved with tx_hash "simulated" without any
        payment at all."""
        return srv.verify_payment(request_id, int(amount_raw), require_onchain=True)

    @server.tool()
    def pay_and_call(request_id: str, amount_raw: str, tool: str) -> dict:
        """Agent side: given a quoted request_id, pay its one-time address and call
        `tool` after verify. NOTE: broadcast is not executed here in tests (no funded
        wallet); this returns the target so a host calling the server can send via the
        SDK wallet (Wallet.send) then verify."""
        q = srv.quote(price_raw=int(amount_raw), request_id=request_id)
        return {
            "request_id": request_id,
            "pay_to": q.address,
            "amount_raw": str(amount_raw),
            "tool": tool,
            "verify_status": "call verify_payment(request_id, amount_raw) after sending",
        }

    return server


def main() -> None:
    """Run the server over stdio (default MCP transport for agents)."""
    server = server_from_env()
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
