"""Pay-per-call service: quote + verify_payment using one-time addresses.

Flow (the invention):
  1. `quote(price_raw)` derives a fresh one-time address from the server master
     key + a new request_id and returns {request_id, address, price_raw}.
  2. The agent sends exactly `price_raw` to that address via its SDK wallet.
  3. `verify_payment(request_id, amount_raw)` watches the *on-chain* state of
     that one-time address (account_history) and approves the call only once a
     matching send is confirmed there, and never twice (exactly-once).

No memo, no trusted verifier, no off-chain settlement: the chain itself is the
verifier, and the one-time address binds exactly one payment to exactly one call.

The client is injectable (an RpcClient by default; a stub in unit tests) so the
on-chain lookup and the exactly-once approval can be tested without moving funds.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Protocol, runtime_checkable

from nano_sdk.client import RpcClient
from nano_sdk.units import raw_to_nano

from .oneshot import derive_one_time_account, new_request_id
from .pricing import QUOTE_TTL_SECONDS, default_rate, exact_xno_amount
from .store import ApprovalStore


@dataclass
class Quote:
    request_id: str
    address: str
    price_raw: int
    price_usd: str | None = None
    rate_xno_usd: str | None = None
    expires_at: float | None = None

    def as_dict(self) -> dict:
        out = {
            "request_id": self.request_id,
            "address": self.address,
            "price_raw": str(self.price_raw),
            "price_nano": str(raw_to_nano(self.price_raw)),
        }
        if self.price_usd is not None:
            out["price_usd"] = self.price_usd
        if self.rate_xno_usd is not None:
            out["rate_xno_usd"] = self.rate_xno_usd
        if self.expires_at is not None:
            out["expires_at"] = f"{self.expires_at:.3f}"
        return out

    def expired(self, now: float | None = None) -> bool:
        """True if this quote's honour window has passed (no window = never)."""
        if self.expires_at is None:
            return False
        now = now if now is not None else time.time()
        return now > self.expires_at


class HistoryClient(Protocol):
    """Minimal RPC surface the payment service needs (enables stub clients)."""

    def account_history(self, account: str, count: int = 20) -> dict: ...


class PaymentService:
    def __init__(
        self,
        master_secret: bytes,
        client: HistoryClient | None = None,
        store: ApprovalStore | None = None,
        rate_source: Callable[[], Decimal] | None = None,
        clock: Callable[[], float] | None = None,
    ):
        self.master_secret = master_secret
        self.client = client if client is not None else RpcClient()
        self.store = store if store is not None else ApprovalStore()
        # dollar-quote rate source (default: live median of 3 sources);
        # an injectable fixed-rate source lets tests compute offline.
        self.rate_source = rate_source if rate_source is not None else default_rate
        self.clock = clock if clock is not None else time.time

    def one_time_account(self, request_id: str):
        return derive_one_time_account(self.master_secret, request_id)

    def quote(self, price_raw: int, request_id: str | None = None) -> Quote:
        rid = request_id or new_request_id()
        acct = self.one_time_account(rid)
        self.store.record_quote_price(rid, int(price_raw))
        return Quote(request_id=rid, address=acct.address, price_raw=int(price_raw))

    def quote_usd(
        self,
        price_usd: str | Decimal,
        request_id: str | None = None,
    ) -> Quote:
        """Price a call in USD: convert to the exact XNO raw amount via the live
        median of three price sources and return a quote that expires in <=30s.

        Pure computation — no balance is held, converted or sent here; the buyer
        pays the returned price_raw directly to the one-time address on-chain.
        """
        rate = Decimal(str(self.rate_source()))
        price = Decimal(str(price_usd))
        exact_raw, _ = exact_xno_amount(price, rate_xno_usd=rate)
        rid = request_id or new_request_id()
        acct = self.one_time_account(rid)
        expires_at = self.clock() + QUOTE_TTL_SECONDS
        self.store.record_quote_expiry(rid, expires_at)
        self.store.record_quote_price(rid, exact_raw)
        return Quote(
            request_id=rid,
            address=acct.address,
            price_raw=exact_raw,
            price_usd=format(price, "f"),
            rate_xno_usd=format(rate, "f"),
            expires_at=expires_at,
        )

    def _onchain_paid(self, account, amount_raw: int) -> str | None:
        """Return the tx hash of a confirmed on-chain send *to* `account` of at
        least `amount_raw`, from the account's history; None if not yet seen.

        account_history on the (one-time) account lists sends where it is the
        source and receives where it is the destination. A payment to the
        one-time address appears as a `receive` entry (or, pre-receive, we also
        accept the matching `send` observed via `pending`). For correctness we
        require the destination to be the one-time address.
        """
        try:
            hist = self.client.account_history(account.address, count=20)
        except Exception:
            return None
        for entry in hist.get("history", []):
            etype = entry.get("type")
            amt = int(entry.get("amount", "0") or 0)
            if amt >= amount_raw:
                # receive: funds landed on this one-time address
                if etype == "receive":
                    return entry.get("hash")
                # send/receive shape varies by node; accept type send only when
                # this address matches the block's account (it is the receiver's
                # history), which it is by construction of the query.
                if etype == "send" and entry.get("account") == account.address:
                    return entry.get("hash")
        return None

    def verify_payment(
        self,
        request_id: str,
        amount_raw: int,
        require_onchain: bool = True,
    ) -> dict:
        """Verify that `amount_raw` was paid to the one-time address for
        request_id. Returns:

          {"status": "approved", "request_id", "address", "tx_hash"}
              on the FIRST sighting of the matching on-chain send.
          {"status": "spent", ...}
              if this request_id was already approved (replay refused).
          {"status": "pending", ...}
              if no matching on-chain send is seen yet.
        """
        acct = self.one_time_account(request_id)
        # exactly-once: previous approval wins, always.
        existing = self.store.get(request_id)
        if existing and existing["status"] == "approved":
            return {
                "status": "spent",
                "request_id": request_id,
                "address": acct.address,
                "tx_hash": existing["tx_hash"],
            }

        # block-6: a dollar quote paid after its 30s honour window is refused.
        expiry = self.store.quote_expiry(request_id)
        if expiry is not None and self.clock() > expiry:
            return {
                "status": "expired",
                "request_id": request_id,
                "address": acct.address,
            }

        # The caller names `amount_raw`, and in the pay-per-call flow the caller IS
        # the payer: the MCP tool docstrings tell a buyer to "pay the exact
        # price_raw to `address` ... then call verify_payment(request_id,
        # price_raw)". Believing that number let a payer quote 1 XNO, send 1 raw
        # and ask to be verified against 1 raw. So the amount checked on-chain is
        # the HIGHER of what the caller names and what this server actually
        # quoted. It only ever rises: a caller asking for more than was quoted
        # still gets the stricter check it asked for, and a request_id this
        # server never quoted for is unchanged.
        quoted = self.store.quote_price(request_id)
        if quoted is not None and quoted > amount_raw:
            amount_raw = quoted

        if require_onchain:
            tx_hash = self._onchain_paid(acct, amount_raw)
            if tx_hash is None:
                return {
                    "status": "pending",
                    "request_id": request_id,
                    "address": acct.address,
                }
        else:
            tx_hash = "simulated"

        # claim exactly-once (atomic sqlite insert)
        claimed = self.store.claim(request_id, tx_hash, acct.address, amount_raw)
        if not claimed:
            # raced with a concurrent approval -> replay refused
            existing = self.store.get(request_id)
            return {
                "status": "spent",
                "request_id": request_id,
                "address": acct.address,
                "tx_hash": existing["tx_hash"] if existing else tx_hash,
            }
        return {
            "status": "approved",
            "request_id": request_id,
            "address": acct.address,
            "tx_hash": tx_hash,
        }
