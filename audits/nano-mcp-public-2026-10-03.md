# nano-mcp-public - audit 2026-10-03

Review of this branch before merging it, and of the XNO path it touches. The question
asked throughout: can an agent pay, or get paid, in XNO through this package today
without being hurt?

## Checked

- `pip install -e ".[dev]"` from a clean virtualenv, then `pytest -q -m "not network"`
  on `main` (138 passed, 4 skipped) and on this branch merged with `main`
  (**146 passed, 4 skipped, 6 deselected**, 0 failed). `git merge origin/main` into this
  branch reports "Already up to date", so the tested tree is the tree that lands.
- The four production changes this branch integrates, read line by line against `main`:
  `nano_sdk/crypto.py` (`public_key`, `address_from_public_key`), `nano_sdk/units.py`
  (`nano_to_raw`), `nano_mcp/pricing.py` (`usd_to_xno_raw`).
- Whether each change can widen what is accepted. Three of the four only add a
  `ValueError` on input that previously produced a wrong value; none changes a
  destination, a key path or a signing input.
- `usd_to_xno_raw`'s callers, to establish whether the arithmetic change sits on a
  send path: the only production caller is `exact_xno_amount`, called from
  `NanoPaidService.quote_usd` (`nano_mcp/service.py:108`), which the code itself
  documents as "Pure computation - no balance is held, converted or sent here; the
  buyer pays the returned price_raw directly to the one-time address on-chain." No
  block is built or signed from this value.
- The new arithmetic against the formula the docstring promises,
  `ceil(price_usd / rate_xno_usd * 10**30)`, over 20,000 randomised price/rate pairs:
  the `Fraction` result equals the exact integer ceiling in every case. The old
  `Decimal` path differed from it in 13,499 of the 20,000 - **7,562 of them low**, i.e.
  the seller under-received, which is the half that matters. It also answered
  differently at `decimal.getcontext().prec` 20 than at 28 (`0.001` USD at `1.37`
  USD/XNO: `...072990000000000` vs `...072992700730`), on a process-global this module
  never sets.
- Reverting each production change alone on the merged tree, to confirm the tests
  bind: 8 tests fail on `main` with this branch's `tests/` in place
  (`test_the_ascii_bytes_of_a_hex_key_are_refused_not_silently_accepted`,
  `test_public_key_accepts_exactly_32_bytes_and_refuses_every_other_length`,
  `test_address_from_public_key_refuses_a_key_that_is_not_32_bytes`,
  `test_nano_to_raw_refuses_a_malformed_amount_with_value_error`, three in
  `tests/test_pricing.py`, and the README collection-count test), and all pass with it.

## Found

Nothing new beyond what this branch already fixes. The worst of the four is worth
restating because it is a silent loss of funds rather than an error: before #12,
`public_key()` passed a 64-byte input straight to `ed25519_blake2b.SigningKey`, which
accepts the `seed || verifying key` form and returns bytes 32:64 **verbatim**. The 64
ASCII bytes of a hex-text key - exactly what `key.encode()` and
`open(path, "rb").read()` give - therefore came back as a "public key" and encoded to a
checksum-valid `nano_` address that passes `validate_address` and that **no private key
can sign for**. XNO sent to it is unspendable by anyone, and nothing warned.

## Fixed

Nothing added by this audit. The branch's own four fixes are the change.

## Could not verify

- Anything behind the `network` marker (6 deselected tests against live
  `rpc.nano.to`): no node is reachable from this run, so the on-chain halves of
  `verify_payment` and `_onchain_paid` are exercised only against the fake node in
  `tests/`.
- The real-money consequence of the pricing correction, only its arithmetic. The
  deviations found are in the last few raw digits (sub-attonano at these prices), so
  the defect is one of contract and determinism rather than of material loss.
