# nano-mcp-public — audit 2026-10-08

Through the lens: *can an agent pay, or get paid, in XNO with this today without being hurt?*

This is the repository people `pip install`, which raises the cost of everything below.

Base: `c2d7877` (`ci: run the published package's suite on a runner (#22)`). Installed from the
declared dependencies (`pip install -e ".[dev]"`), suite green before any change:
**176 passed, 4 skipped, 6 deselected**.

Reached by pair comparison with `dhyabi2/nano-mcp`, where the same defect was found first today
and fixed as that repository's #20. The two servers expose the same seven tools over the same
`PaymentService`/`ApprovalStore` shape, so a defect in one is worth looking for in the other.

## Found and fixed (this PR)

**`verify_payment` believed the payer's own numbers** — `nano_mcp/server.py:92`,
`nano_mcp/service.py:186`. `verify_payment` is an MCP *tool*, and in the pay-per-call flow the
agent calling it is the one that owes the money: the `quote` and `quote_usd` docstrings tell a
buyer to pay `price_raw` and "then call verify_payment(request_id, price_raw)". Two of its
arguments were taken on trust, and the quoted price was never recorded anywhere — `store.py`'s
schema held `approvals` and `quote_expiry` only, and `amount_raw` was written at claim time from
whatever the caller passed, so there was nothing to check that caller's number against.

Driven through the real `MCPServer` tool surface against a node that reports only what the payer
really sent:

| | what the payer did | what it got |
|---|---|---|
| A | `verify_payment(rid, amount, require_onchain=False)` | `status: approved`, `tx_hash: "simulated"` — the node was asked **nothing at all** |
| B | quoted 1 XNO (`10**30` raw), sent **1 raw**, then `verify_payment(rid, "1")` | `status: approved` with a real tx hash — a 1 XNO call for `10**-30` XNO |

`pay_and_call` (`server.py:101`) made B worse: it re-quotes at an amount **its own caller names**,
so a payer could quote itself down and then be verified against the lower number.

The fix is the same one that landed in `nano-mcp#20`, and adds refusals only:

* `store.record_quote_price()` / `quote_price()` record what *this server* quoted, as `TEXT` —
  a raw amount reaches 39 digits and SQLite's `INTEGER` is 64-bit, overflowing above ~1.8e19 raw
  (under 0.00002 XNO). The record **never lowers**, so `pay_and_call` cannot walk the floor down.
  New table under `CREATE TABLE IF NOT EXISTS`, so an existing store file needs no migration.
* `service.verify_payment()` checks the **higher** of the caller's amount and the recorded quote.
  The bar only ever rises: a caller demanding more than was quoted still gets the stricter check,
  and a `request_id` this server never quoted for (the `get_address` path) is unchanged.
* The `verify_payment` **tool** no longer takes `require_onchain`. The service method keeps it as
  an in-process test seam; it is simply not reachable from the surface a payer talks to.

No amount, destination, rounding or key path is touched, and nothing refused before is accepted now.

**Evidence.** 7 new tests. With `tests/` kept and `nano_mcp/` alone reverted to `main`:
**4 failed, 3 passed** — the 3 are controls that must hold either way. With the fix the full suite
is **183 passed, 4 skipped, 6 deselected**. Both guards mutation-checked: `if False:` on the floor
fails 3 tests; a plain overwrite instead of the non-lowering `max` fails
`test_pay_and_call_cannot_quote_the_payer_back_down`. The tree was restored byte-for-byte after
each and the full suite re-run.

`README.md:31` states the offline test count and `tests/test_readme.py` holds it against what
pytest actually collects — a good guard, and it caught this change. Updated 177 → 184.

## Checked and clean

* **The two defects fixed this morning are fixed.** `Wallet.seed` no longer reaches `repr()` and a
  `bool` amount no longer publishes a send of 1 raw (#20); `RpcError` is the one exception a caller
  has to handle (#21). Both verified present on this tree, not assumed from the merge.
* **`usd_to_xno_raw` is exact here.** This repository's copy already runs the conversion outside
  the process-global `Decimal` context, so the rounding-at-28-digits defect found today in
  `nano-mcp`'s `pricing.py` (left open there as that repository's #21, because fixing it moves an
  amount) is **not** present in the published package.
* **Exactly-once approval.** `ApprovalStore.claim` is an atomic `INSERT … PRIMARY KEY`; a replay
  returns `spent` across restarts and under concurrency.
* **Unconfigured, the server refuses** rather than generating a master secret on the fly — the
  stricter contract of the two servers, and now pinned by `test.yml`'s first-contact step (#22).
* **Dollar-quote TTL** is enforced on the verify side, not merely advertised.
* **No secret in the tree.** The master secret comes from `NANO_PAYMENT_MASTER_SECRET`; grep over
  the tree and `git log -p` found no seed, key or token committed.
* **The quickstart runs.** `tests/test_readme.py` executes the README's SDK example and holds its
  stated test count, so the first thing a new agent copies is checked by CI rather than by hope.

## Could not verify

* **No live node and no live payment.** This environment's network policy reaches neither
  `rpc.nano.to` nor the three price APIs, so the 6 network tests are deselected and every result
  above is measured against the repository's stubs and recorded shapes. **No XNO moved.**
* **Whether any seller has actually been underpaid** through either hole, and whether any installed
  copy of this package was ever reached by a payer that tried. Both holes are reachable from the
  tool surface by construction, which is what is shown; the published versions that carry them are
  not something this environment can inspect.
* The released artifacts on PyPI were not re-examined; this audit speaks to the tree at `c2d7877`.
  Whether a release carrying the fix should be cut is the owner's call — publishing a package is
  outside a routine's authority.
