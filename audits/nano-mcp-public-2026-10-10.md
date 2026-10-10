# nano-mcp-public — audit 2026-10-10

Through the lens: *can an agent pay, or get paid, in XNO with this today without being hurt?*

This is the repository people `pip install`, which raises the cost of everything below.

Base: `c4d8e42` (`The handshake reported a server's stderr while still reading it (#25)`).
Installed from the declared dependencies (`pip install -e ".[dev]"`); suite before any change:
**204 passed, 9 skipped, 1 failed** — the failure is
`tests/test_pricing.py::test_live_median_of_three_sources_returns_numeric`, which reaches the
three live price APIs and gets `httpx.ProxyError: 403 Forbidden` from this environment's network
policy. It fails identically on the base commit and is not a defect in the repository.

Reached by pair comparison with `dhyabi2/nano-mcp`, where the same defect was found first today.
The two servers share `facilitator.py` almost verbatim, so a defect in one is worth looking for
in the other — and this is the twin a stranger installs.

## Found and fixed (this PR)

**A malformed `requirements` crashed `/verify` and `/settle` instead of refusing them** —
`nano_mcp/facilitator.py:518` (`_check_requirements`), reached from `verify()` and `settle()`.

`REQUIRED_SECTIONS` (line 396) listed the mandatory fields and **nothing read it**.
`_check_requirements` compared `requirements` against the payload's `accepted` block instead, so
a field missing from *both* halves is `None == None` and passed every check. `verify` and
`settle` then indexed `requirements["payTo"]` and `requirements["amount"]` directly, and
`verify_block_on_independent_endpoints` called `parse_raw(amount)` outside any `try`.

Driven against the real `Facilitator` in this tree, with two stub endpoints that fail the test
if consulted:

```
verify -> RAISED KeyError: 'payTo'
settle -> RAISED KeyError: 'payTo'
```

and the same for `amount: "abc"` / `"1e30"` (`ValueError: invalid literal for int()`), for
`requirements: []` (`AttributeError: 'list' object has no attribute 'get'`), while `amount: ""`
parsed to **0** and `amount: "-1"` to **-1** and both were carried to the nodes as the amount to
match. None of these exceptions is caught above `make_handler`'s `do_POST`, so over HTTP the
caller gets a closed connection and no body where the scheme requires
`{"isValid": false, "invalidReason": ...}` — which a paying agent cannot tell apart from the
facilitator being down.

This twin's `_read_json` **already** refuses a body that is not a JSON object and answers 400
(a guard `nano-mcp` still lacks, now being ported there). That guard is upstream of this one and
does not help: the crashing body above *is* a well-formed JSON object; the fault was in the code
reading fields off it.

**The fix adds refusals only**, and is the same patch landing as `nano-mcp#24` — lifted verbatim,
so the twins do not drift. After every check `_check_requirements` already made, in that
unchanged order (so no existing `invalidReason` string moves), it now refuses:

* a `requirements`, `payload` or `accepted` that is not a JSON object → `bad-request`;
* any of `REQUIRED_SECTIONS` absent or blank → `bad-requirements`;
* an `amount` that `parse_raw` cannot read, or that is not positive → `bad-amount`.

No amount, destination, rounding or key path is touched. `parse_raw` still reads the decimal-XNO
form it always did, so nothing that could be quoted before is refused now.

**Evidence.** 21 laws in `tests/test_facilitator_refuses_malformed_requirements.py`. With
`tests/` kept and `nano_mcp/` alone reverted to `main`: **18 failed, 3 passed** — the 3 are the
controls (`0.000001`, `1`, and one whole XNO in raw) that must still reach the nodes and be
refused as `unconfirmed`, and they hold either way. With the fix: **21 passed**, full suite
**225 passed, 9 skipped, 1 failed** (the same live-price-API failure as the baseline).

**One README claim moved, because a test made it.** `tests/test_readme.py::test_the_offline_test_count_in_the_quickstart_is_the_count_pytest_collects`
failed at `the README says 205 offline tests, pytest collects 226`. That law exists so a reader
following the quickstart cannot mistake a stale README for tests they broke, and it did its job:
`README.md:31` is updated from 205 to 226. Nothing else in the README changed.

## Checked and clean

* **The quote is exact and host-independent.** `usd_to_xno_raw` computes
  `ceil(price * 10**30 / rate)` through `Fraction`, consulting no `Decimal` context. Cross-checked
  this run against `nano-mcp`'s independent implementation (which uses
  `Decimal.as_integer_ratio()`) and against an exact `Fraction` ceiling, over seven
  price/rate pairs including the three that the 2026-10-08 audits tabulated as wrong under the old
  code: **both twins agree with each other and with the exact ceiling on all seven, 0 mismatches.**
  The twins cannot quote a buyer and a seller different numbers.
* **`verify_payment`'s quote floor holds.** `store.record_quote_price` takes
  `max(existing, new)` and `service.verify_payment` checks the higher of the caller's amount and
  the recorded quote, so `pay_and_call`'s re-quote cannot walk the floor down. Byte-identical to
  `nano-mcp`'s `server.py` and `service.py` (`diff` over both: 0 differing lines).
* **Integer raw end to end.** `nano_sdk/units.py` rescales the Decimal exponent rather than
  multiplying by `10**30`, and `nano_to_raw` normalises `InvalidOperation` to `ValueError` so the
  one documented exception covers every unusable amount. No float on an amount anywhere in
  `nano_mcp/` or `nano_sdk/`.
* **`settle` re-runs the full verification** rather than trusting a prior `/verify`, and the
  single-use claim is an atomic `INSERT … PRIMARY KEY` keyed by
  `nano:live <hash> <request_id>`, with a rollback on conflict.
* **The payee is read, not assumed.** `normalize_block_info` reads `contents.link_as_account`,
  and an unreadable payee is a refusal rather than a default to `pay_to`.
* **No secret in the tree or history.** `git log --all -p` grepped for `nano_`/`xrb_` seeds,
  64-hex blobs, `sk-`, `ghp_`, `AKIA`, JWT and PEM headers — nothing. The master secret is read
  from `NANO_PAYMENT_MASTER_SECRET`; `Wallet.seed` is `field(repr=False)` and `Account.__repr__`
  withholds the private key.
* **Install URLs and registry identity** are held by `tests/test_readme.py` and
  `tests/test_registry_identity.py`, both green: every fetch URL names
  `dhyabi2/nano-mcp-public`, and no URL points at the deleted account.
* **The twins have not drifted on the money path.** `server.py`, `service.py`, `oneshot.py`,
  `paidtool.py`, `nano_sdk/crypto.py`, `client.py` and `buyer.py` are byte-identical to
  `nano-mcp`'s `main`; the differences in `store.py`, `pricing.py`, `httpx402.py`, `units.py`,
  `wallet.py` and `block.py` were read line by line and are comments, docstrings and the
  `Fraction`-vs-`as_integer_ratio` spelling of the same exact arithmetic. `wallet.check_send` is
  identical in both.

## Found, NOT fixed here

**`build_fail_closed_config` will accept the same node twice as "two independent endpoints"** —
`nano_mcp/facilitator.py:323-329`. `if custom and len(custom) >= 2: return list(custom)` counts
entries, never distinct URLs, and the result then reports `consulted: 2`. A self-hoster passing
the same URL twice gets a verdict decided by one node while the answer says two agreed — the
single-RPC outage, or single-RPC lie, that strategy law L1 exists to refuse. Present in both
twins, left out of this PR under one-concern-per-branch; the fix is a refusal (or a dedupe of the
URL set) and is in scope for a routine to land on its own branch.

**`NormalizedBlock.is_send` returns True for an unknown subtype** — line 99,
`self.subtype in ("send", "state", "")`. The `""` arm fails *open*. What stops it being
exploitable today is the payee check immediately below: a receive block's `link_as_account` is
derived from the send block's hash and a change or epoch block's is the burn address, so neither
can equal a `payTo`. I could not construct a block that passes the payee check and should not be
a send, so this is recorded as a loose guard rather than a proven defect, and nothing is changed
on that basis.

## Could not verify

* **No live node and no live payment.** This environment's network policy reaches neither
  `rpc.nano.to` nor the three price APIs, so every result above is measured against the
  repository's stubs and its recorded real-node shapes. No XNO moved.
* **Whether a published facilitator has crashed this way in practice** is not knowable from here.
  What is shown is that the crash is reachable from the documented HTTP surface with a well-formed
  JSON body.
* **The installed-from-PyPI artifact.** Everything above was run against this checkout
  (`pip install -e .`), not against a wheel fetched from an index.
