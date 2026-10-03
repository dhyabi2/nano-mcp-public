# nano-mcp-public — audit, 2026-09-30

Audited at `25ef5e7` (head of `main`). Python 3.11.15, installed the way the README says
(`pip install -e ".[dev]"`; mcp 2.2.0, httpx 0.28.1, ed25519-blake2b-fork 1.4.2).

Baseline: **138 passed, 4 skipped, 6 deselected** under `pytest -m "not network"`. After this
change: **140 passed, 4 skipped, 6 deselected.**

## What was checked

- **The README, run rather than read.** The editable install and the offline suite both work as
  printed. The `v0.1.0` tag it pins for the public install exists. `mcp>=2.0` resolves (2.2.0), so
  the dependency floor the comment in `pyproject.toml` explains is real and satisfiable. The
  "3 journal-plugin modules skip" clause is exact: three skip for the plugin, and the fourth skip
  is an unreachable live endpoint, not a fourth module.
- **`nano_sdk/units.py`** — probed rather than read, across 19 inputs.
- `nano_mcp/httpx402.py` — `b64decode_json` (non-object JSON, URL-safe alphabet, missing padding,
  non-ASCII), `_accepted_matches`, `ResourceApp.complete`'s four refusal paths, and the handler's
  402/400/404 branches. Nothing external reaches a header value; every error string goes in the
  JSON body, so there is no header-injection surface.
- `nano_mcp/store.py` — the claiming insert, the shared-connection lock, `default_store_path`'s
  three-step precedence.
- Grepped the whole package for `subprocess`, `eval`, `exec`, `pickle`, `shell=True` and for
  `int()`/`float()` over external data: **none**, except `store.get`'s own `amount_raw`.

## Found and fixed (this pull request)

**`nano_to_raw` refuses a malformed amount with an exception its own docstring rules out.**

The docstring offers ValueError as the way to refuse a bad amount. `Decimal(str(amount))` answers
anything unconvertible with `decimal.InvalidOperation`, which is an `ArithmeticError` and no kind of
ValueError — so `except ValueError`, the only defence the docstring describes, did not catch `""`,
`"abc"`, `"0x10"`, `"not-a-price"` or `None`:

    nano_to_raw('abc')  RAISED InvalidOperation: [<class 'decimal.ConversionSyntax'>]
    nano_to_raw('')     RAISED InvalidOperation: [<class 'decimal.ConversionSyntax'>]
    nano_to_raw('1e-31') RAISED ValueError: amount has more precision than 10^-30 nano

`nano_to_raw` is exported from `nano_sdk` and its one production caller is the **`quote` MCP tool**
(`nano_mcp/server.py:76`), whose `price_nano` is whatever the calling agent typed — so the
unconvertible case is the ordinary case. Driven through the real tool surface, a bad price leaves
`call_tool` as `mcp.server.mcpserver.exceptions.UnexpectedToolError`.

The fix converts that one exception and nothing else. Two laws, because a converter for money must
be shown not to have moved an amount while an exception was narrowed:
`test_nano_to_raw_refuses_a_malformed_amount_with_value_error` (fails against the unfixed file with
the `InvalidOperation` traceback) and
`test_nano_to_raw_still_converts_every_accepted_amount_identically`, which pins eight accepted
spellings — `0`, `1`, `0.000001`, `1e-30`, a full 30-decimal amount, `1E+2`, whitespace — to the raw
they returned before. **No input that converted now converts differently; only inputs that already
raised raise a different class.**

The README's offline test count moves 139 → 141 with the two new laws, which
`test_the_offline_test_count_in_the_quickstart_is_the_count_pytest_collects` requires.

## Found, not changed

- **A bad price argument leaves `call_tool` as `UnexpectedToolError` whatever the exception class**,
  including for the ValueError the docstring always promised (`"1e-31"`). An agent that mis-types a
  price gets "Error executing tool quote" and no usable message. Returning a structured tool error
  is the right answer and is a change to the server's contract, not a fix, so it is reported.
- **`nano_to_raw` accepts spellings no other tool in this stack would read as a number**, because
  `Decimal` does: `"1_000"` becomes 1000 XNO, and the fullwidth digit `"０"` becomes 0. Both are
  correct by `Decimal`'s rules and neither is what a price field ought to accept. Narrowing this
  *would* change an amount's interpretation, which is a decision about money and not this audit's
  to take.
- **No ceiling.** `nano_to_raw("1e400")` returns a 400-digit raw, far beyond the 2**128−1 a Nano
  balance can hold, so a price can be quoted that the ledger cannot express. A supply ceiling is a
  design decision.
- **`raw_to_nano(-5)` and `nano_str(-5)` accept a negative raw** and format it. A raw balance cannot
  be negative.

## Not verified

- Anything requiring the live `rpc.nano.to` node or the three public price sources: this sandbox
  reaches neither (`tests/test_facilitator_live.py` skips on `rainstorm.city` being unreachable).
  The change above touches no network path.

## Secrets

Clean. No key, seed, token or `.env` in the tree or in this change; the only 64-hex values are
declared test vectors.
