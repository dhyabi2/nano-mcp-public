# nano-mcp

<!-- MCP Registry ownership proof: the official registry verifies a PyPI package by
     finding this exact string in the package README (which becomes the PyPI
     description). Keep it identical to the server name in server.json.mcpregistry. -->
<!-- mcp-name: io.github.PANDeveloper001/nano-mcp -->

An **MCP server + SDK** so any AI agent can hold XNO (Nano) and pay per API call, transacting
through the public node **rpc.nano.to** (no local node, no issuer, no bridge).

Built by an **AI agent** (Rai). Tests are run against the live `rpc.nano.to` node.

> **Public release** — this is the clean, gate-2 release of the nano-mcp project. It is a
> single-commit, secret-scanned snapshot (no git history, no withdrawn `draft/x402` P1
> duplicate), published so any agent or API consumer can use the `nano_mcp` and `nano_sdk`
> packages. The working copy with the full law-ledger history stays private.

## 5-minute quickstart

The MCP server is stdio-only and needs one secret: a master secret it uses to derive a **one-time
payment address per request**. Nothing else is required (the default RPC is the public
`rpc.nano.to` node).

```bash
# 1. run it straight from a checkout (uv resolves the deps for you)
NANO_PAYMENT_MASTER_SECRET=$(python3 -c "import os;print(os.urandom(32).hex())") uv run python -m nano_mcp.server

# 2. or install the packages and register the server with an MCP client
uv venv .venv && source .venv/bin/activate
uv pip install -e ".[dev]"
python -m pytest -m "not network"        # offline tests pass (123)
```

Verified public install (pinned to the v0.1.0 release; sdist + wheel served as release assets):

```bash
uv pip install "nano-mcp @ git+https://github.com/PANDeveloper001/nano-mcp-public.git@v0.1.0"
python -c "import nano_mcp, nano_sdk; print('nano_mcp OK')"
```


Use the SDK to derive a wallet and read a balance:

```python
from nano_sdk import RpcClient, Wallet
from nano_sdk.crypto import derive_account

# seed from your env only — never commit it
account = derive_account(SEED, 0)
print(account.address)

wallet = Wallet(seed=SEED, client=RpcClient())   # reads rpc.nano.to
print(wallet.balance())
```

To stand up the x402 facilitator and paid tool surface, see `nano_mcp/facilitator.py` and
`nano_mcp/paidtool.py`; live tests need `NANO_RPC_URL` + `NANO_RPC_KEY` and a funded test
wallet (the on-chain send is written but — honestly — not yet faked, see Status).

## Why Nano

- **Feeless** — no per-transaction fee, so truly *per-call* (even micro) billing is economic; no batching.
- **Instant** — >99.9% of transactions settle in under a second.
- **Green** — no mining.
- **No issuer** — self-custody; nothing to freeze, no trusted third party to verify the payment.

## Repo layout

- `nano_sdk/` — pure-Python SDK: derive a wallet from a seed, read balance/history, and (later
  blocks) sign + publish sends via `rpc.nano.to`. Crypto is in `nano_sdk/crypto.py`.
- `nano_mcp/` — MCP server exposing wallet and pay-per-call tools, plus a self-hostable
  facilitator (`nano_mcp/facilitator.py`) exposing the x402 `exact`-on-`nano` `/supported`,
  `/verify`, `/settle` surface (verifies on ≥2 independent RPCs, fails closed).
- `tests/` — pytest; `nano_sdk/crypto.py` vectors are validated against the live node.

## Install / test

```bash
uv venv .venv && source .venv/bin/activate
uv pip install -e ".[dev]"
# NANO_RPC_URL + NANO_RPC_KEY must be set for live read/send tests
python -m pytest -m "not network"   # offline tests
python -m pytest                    # includes live rpc.nano.to reads
```

## Address encoding (verified against the node)

`PrivK[i] = blake2b-256(seed || uint32be(i))`, public key via Ed25519-Blake2b, address =
`nano_` + fixed-width-52 big-endian base32 of the public key + fixed-width-8 base32 of the
little-endian `blake2b-40(public_key)` checksum.

## Status

Built and verified under the Law Ledger (`.ledger/`): blocks 2–15 done.

- Block 2: SDK derive + live read (L0, L1).
- Block 3: SDK send (sign + PoW + publish) with balance + 0.01 XNO/day cap guards (L3).
- Block 4: MCP pay-per-call server — one-time address per request (L4), exactly-once
  `verify_payment` (L5).
- Block 5: end-to-end probe (L6) + evidence gate that journals a payment as `nano_tx`
  only if it comes from an account we do NOT control (L7). `ledger probe` → 88/100.
- Block 6: dollar-priced quotes — the exact XNO for a USD price from the **median of
  three** independent sources, expiring in ≤30s, pure computation (L8, L9).
- Block 7: **Buyer SDK** — owner-signed Ed25519 `Mandate` + capped per-session
  sub-accounts. `SessionWallet` lets an owner delegate *limited, expiring* spending
  authority to an autonomous agent: it verifies the owner signature, the session
  binding, the per-session cap, the 0.01 XNO/day cap and the balance guard before
  any block is broadcast, so a compromised agent cannot drain the wallet (L10, L11).
- Block 12: **self-hostable x402 `exact`-on-`nano` facilitator** — `/supported`,
  `/verify`, `/settle` HTTP surface that verifies every payment proof on **at least
  two independent RPC endpoints** (fails closed if any cannot confirm) and settles
  it with an atomic single-use claim, exactly once (L16, L17). This is the real
  facilitator the x402 spec's "Reference implementations" section describes.
- Block 13: the multi-RPC verifier parses the **real Nano block_info shape**
  (`block_account` / `contents.type` / `contents.destination` / `confirmed:"true"`)
  and confirms a real on-chain send on two independent public RPCs (L18).
- Block 14: the **HTTP 402 Resource Server** — returns `402 Payment Required` with a
  `payment-required` header (one-time nano_ payTo + exact amount) and serves the
  protected result only after the client presents a verified-and-settled
  `payment-signature` (L19, L20). The missing HTTP half of the x402 protocol.
- Block 15: the **MCP paidTool wrapper** (`nano_mcp/paidtool.py`) — `paid_tool_request`
  issues a one-time nano payTo + exact amount, and `paid_tool_execute` verifies the
  proof on two independent RPCs, settles it exactly once, and returns the protected
  tool result — the same handshake the HTTP server runs, exposed to MCP agents (L21,
  L22). Closes the last roadmap-stage-1 deliverable.
- Block 16: the scorecard reads **real evidence**. The open rail scorecard's
  `--journal` path previously accepted only a hand-written JSON array, so its
  "measured share from nano receipts" could not be computed from the actual evidence
  store. `nano_mcp/journaldb.py` is a stdlib, read-only, network-free adapter that
  reads the real nano-pulse journal DB (kind=`nano_tx`, written by `append_nano_tx`),
  and `scorecard build/verify --journal-db <path>` feeds it straight into the share
  computation. `scorecard/published.json` now reproduces exactly from the real DB
  (strategy law L5/L6): with no external receipts yet it honestly reads 0% share
  (L23).

L2 (a live funded on-chain send confirmed via rpc.nano.to) is recorded STUCK: no funded
test wallet exists, and the money rules forbid seeking funds. Every real component is
exercised end-to-end through a chain stub; the live-funded confirmation leg is written
but not faked.