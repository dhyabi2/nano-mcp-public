# nano-mcp-public — audit 2026-10-14

Picked because the last pass here was 2026-10-12 and because `nano-mcp`'s pass this
morning found a defect in `nano_mcp/facilitator.py` — and **this repository's copy of
that file is byte-identical** to the one on `nano-mcp`'s `main`:

```
$ diff <(git show HEAD:nano_mcp/facilitator.py) \
       <(cd ../nano-mcp && git show origin/main:nano_mcp/facilitator.py)
$                       # no output
```

So the finding transfers in full, and this is the copy that matters more: this is the
public repository, the one an outside agent installs and points a seller at.

## What I checked

- `nano_mcp/facilitator.py`, with the attention on
  `verify_block_on_independent_endpoints`, `normalize_block_info` and
  `Facilitator.verify` / `settle`: every field of a `block_info` reply a verdict is
  built from, and which of them >=2 endpoints have to agree on.
- That the file really is identical to the sibling's (above), so the fix is a port and
  not a second diagnosis.
- Who reads the verdict's fields here: `nano_mcp/evidence.py` and
  `nano_mcp/scorecard.py`, same as the sibling.
- The README quickstart's offline test-count law (`tests/test_readme.py`), which the
  added tests move.
- The suite on `main` in a clean venv per the README: **470 passed, 5 skipped,
  7 deselected.** No pre-existing failure.

## What I found and fixed

**Two independent nodes could name different senders for one block, and the facilitator
published one of them as verified.**

`verify_block_on_independent_endpoints` refuses unless every configured endpoint
confirms the block, reads it as a send, carries the exact required amount and names the
required payee. The payer — `nb.account` — was checked against nothing. It was read off
`confirmed_sends[0]`, whichever endpoint answered first (`facilitator.py:717`, `:781`),
and the other endpoints' readings of it were discarded. Two nodes returning the same
confirmed send, of the right amount, to the right payee, but naming different senders,
both passed and the verdict carried the first one's, under `confirmedOn: 2`. A node
that omits `block_account` reads as `account=""` and produced an isValid verdict
carrying `"payer": ""`.

It matters because the payer is published as fact: `/verify` and `/settle` both answer
it, a resource server binding an entitlement or a refund to the sender reads it from
there, and `evidence.append_nano_tx` writes it into the journal
`scorecard.count_external_receipts` counts from — where `should_log` gates on
`NANO_AGENT_OWN_ACCOUNTS`, and a blank payer is in no such set, so it counts as an
outside agent having paid us.

The full reasoning, the captured verdicts and the "checked and left alone" list are in
`../nano-mcp/audits/nano-mcp-2026-10-14.md` (PR dhyabi2/nano-mcp#32); this note does
not repeat them.

**Fix:** the sibling's two added refusals, ported verbatim — a blank `nb.account`, and
an `nb.account` differing from the first endpoint's, both raised as `RpcError` which
the existing handler already turns into a fail-closed refusal. It only narrows what is
accepted: no amount, payee, destination, rounding, proof, claim key or key path is
touched, no existing refusal is removed or widened, and a block every endpoint reads
the same way verifies and settles exactly as before. A refused settle leaves the proof
unclaimed.

**One README line moves, and only because a law makes it.**
`tests/test_readme.py::test_the_offline_test_count_in_the_quickstart_is_the_count_pytest_collects`
pins the quickstart's stated offline count against what `pytest --collect-only`
actually collects. Adding 13 tests failed that law:

```
AssertionError: the README says 472 offline tests, pytest collects 485
```

README line 31 now says 485. No other prose changed — the README's claim of
verification on "at least two independent RPC endpoints (fails closed if any cannot
confirm)" was already what it says, and is now true of the payer too.

**Failing-then-passing:** with the new test file present and `facilitator.py` reverted
to `main` (the guard alone removed), **10 of its 13 fail** —

```
FAILED test_two_endpoints_naming_different_payers_refuse
FAILED test_verify_refuses_when_the_endpoints_disagree_about_the_payer
FAILED test_settle_refuses_when_the_endpoints_disagree_about_the_payer
FAILED test_a_disagreement_does_not_spend_the_claim
FAILED test_the_disagreement_is_refused_whichever_node_is_asked_first[order0]
FAILED test_the_disagreement_is_refused_whichever_node_is_asked_first[order1]
FAILED test_three_endpoints_refuse_when_only_the_last_disagrees
FAILED test_a_node_that_omits_the_payer_refuses
FAILED test_verify_refuses_a_blank_payer_rather_than_publishing_one
FAILED test_one_endpoint_omitting_the_payer_refuses_too
10 failed, 3 passed in 1.22s
```

(the 3 are the controls: the agreed-payer happy path, the `account`-spelling read, and
the `should_log("")` fact). With the refusals and the README line: **483 passed,
5 skipped, 7 deselected** — the 470 that passed on `main`, all unchanged, plus these 13.

## Could not verify

- No live RPC call was made. `rpc.nano.to` refuses this container's address
  (`429 api_key_required_for_heavy_usage`), as every audit in this repository has
  recorded. Every node reply in the new test is a stub shaped on
  `tests/test_facilitator_real_node_shape.py`'s verbatim capture.
- Whether the two repositories' copies of `facilitator.py` are meant to stay
  byte-identical, or have simply not diverged yet. They are kept in step by hand today
  — this run had to `diff` them to find that out, and a sibling fix landing in one and
  not the other is the obvious way for a money-path refusal to go missing from the
  public copy. Raised for the owner rather than acted on: a shared-source or
  vendoring-check decision is not a correction.
