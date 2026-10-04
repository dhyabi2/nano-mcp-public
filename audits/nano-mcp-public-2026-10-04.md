# nano-mcp-public — audit 2026-10-04

Lens: can an agent pay — or get paid — per tool call in XNO with this, today, without being
hurt? This repository is the **public** half of the `nano-mcp` / `nano-mcp-public` pair — the one
an outside agent installs — so a defect here reaches further than the same defect in the private
twin.

## Checked

- `pip install -e ".[dev]"` and `python -m pytest -q -m "not network"` — the command CI runs.
  **146 passed / 4 skipped** before the change, **155 passed / 4 skipped** after.
- `nano_mcp/facilitator.py` against its twin in `dhyabi2/nano-mcp`: lines 100-250 are
  **byte-identical** to the pre-fix version there.
- The same live `block_info` reply from `rpc.nano.to` used for the twin's audit, replayed
  against this package's own `normalize_block_info` and
  `verify_block_on_independent_endpoints`.

## Found and fixed — the same payee defect as `nano-mcp`, ported

**`verify_block_on_independent_endpoints` skipped the payee check on every reply a real Nano node
returns**, so a confirmed send of the right amount verified whoever it had actually paid. The
full analysis is in `dhyabi2/nano-mcp` PR #18 and `audits/nano-mcp-2026-10-04.md` there; the
short form:

1. `normalize_block_info` built `destination` from `contents.destination`, `link_as_account`,
   `destination` and `link`. A real node returns a **state** block, which names its payee in
   `contents.link_as_account`; `contents.destination` does not exist on a state block at all,
   and the other three were read from the **top level**, where a node puts none of them. So
   `destination` was always `""`.
2. `facilitator.py:234` then read
   `receiver = nb.destination if nb.subtype == "send" and nb.destination else pay_to`, so an
   empty destination defaulted `receiver` to `pay_to` and the following comparison was vacuous.
   `confirmed_sends` recorded `"receiver": pay_to` — a payee that had never been read.

Reproduced here, not assumed, against the reply captured live from `rpc.nano.to` (send block
`64AD5A5C...FEDF`), with the seller's payTo set to an account the block did not pay:

```
normalize_block_info -> destination = ''
verification ok: True   reason=None
*** ACCEPTED. The seller is told they were paid. ***
   ...but the XNO went to nano_1natrium1o3z5519ifou7xii8crpxpk8y65qmkih8e8bpsjri651oza8imdd
```

A buyer owing the quoted amount could send exactly that amount to its own second account,
present the confirmed block hash, and be served. Both the x402 `verify` and `settle` endpoints
go through this function.

**The fix is the twin's patch, applied unchanged** (it applied cleanly — the files were
identical): `contents.link_as_account` is read first, and a payee that cannot be read refuses
rather than being assumed. `contents.link` is deliberately not a fallback, being 64 hex
characters rather than an account. `confirmed_sends` records the payee the block names, checked
to equal `pay_to`. **Nothing previously refused is now accepted.**

`tests/test_facilitator_real_node_shape.py` is the twin's test file, pinning the captured reply
verbatim. **Reverting `nano_mcp/facilitator.py` alone turns 5 of its 9 red**, including the
send-to-a-stranger one.

## The README test-count law applied, as the 09-30 note predicted

`tests/test_readme.py::test_the_offline_test_count_in_the_quickstart_is_the_count_pytest_collects`
runs the collection and compares it with the number the README prints, so adding 9 tests turned
the suite red until `README.md:31` moved **147 → 156**. This is the law recorded after the
four-way `nano-mcp-public#17` integration: *a repository whose README states its own test count
cannot take parallel test-adding pull requests merged independently.* Noted here because it
means this branch and any other test-adding branch on this repository **cannot both be
squash-merged**; the second to land will fail its own CI on a count that was right when it was
written. If another test-adding branch appears before this one merges, integrate rather than
merge in sequence.

## Could not verify

- **No full x402 round trip against a live facilitator and a funded wallet.** The fix is proved
  against a node reply captured live, through a stub endpoint. **The first live
  `verify`/`settle` after this lands is the first time the payee comparison will actually
  execute against a real node**, because before this change it never did.
- **`NormalizedBlock.is_send` accepts `"state"` and `""` as well as `"send"`** and is left as the
  twin leaves it, for the same reason: with the payee check now running, a receive block that
  slipped past that guard is refused on its payee instead, and the new test pins both lines of
  defence. Tightening it would also refuse a legitimate payment from a node reporting only
  `contents.type`.
- The `network`-marked tests (6 deselected, 4 skipped — three of the skips are journal-plugin
  modules not in this release) were not run.
- The rest of the tree was not re-audited this run; the 2026-10-03 audit covers it, and this run
  was spent on the defect the twin's audit surfaced.
