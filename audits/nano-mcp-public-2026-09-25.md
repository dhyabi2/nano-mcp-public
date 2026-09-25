# nano-mcp-public — audit, 2026-09-25

A clone of `main` at `e46315f`, a clean virtualenv, and the dependencies the package itself declares
(`httpx`, `ed25519-blake2b-fork`, `mcp>=1.0`, plus `pytest` from `[dev]`). The suite was green before
any change — **113 passed, 4 skipped, 6 deselected** — so this audit started from a repository whose
own tests were satisfied. Both findings below are defects those tests never asked about.

The method that found them was comparing this repository against its sibling `dhyabi2/nano-mcp`,
which ships the same `nano_sdk` and had received two fixes on 2026-09-25 at 02:05 UTC that never
reached here. **This is the repository people install from**, and it was the one running the older
code.

## What was checked

- `import nano_mcp` and `import nano_sdk` on a clean install. Both import cleanly.
- The offline suite before and after each change, and each ported law against the unchanged file it
  accuses, so the difference is the fix and not the test.
- `nano_sdk/units.py`, `crypto.py`, `block.py` read against the sibling line by line.
- The README's single link: `pip install …@v0.1.0`. The tag **exists** — see *Not verified* for what
  is wrong with it anyway.
- A secret sweep of every tracked file, with the tightened scanner from today's `agent-runtime`
  audit. **39 lines are 64-hex shaped, and every one was checked by hand: none is a secret.** They
  are block hashes, `prev`/`receipt` chain links and signature digests in `.ledger/` and
  `scorecard/`, and the `DOCS_SEED` / `DOCS_PRIV` / `DOCS_PRIV_EXPAND` vectors in the tests. The last
  of those are the only ones that could matter, and they were verified rather than taken on trust:
  both `DOCS_PRIV` values derive to
  `nano_1e5aqegc1jb7qe964u4adzmcezyo6o146zb8hm6dft8tkp79za3sxwjym5rx`, exactly the address the files
  document as the canonical `docs.nano.org` `key_expand` example, and not any account this swarm
  holds.

## Found

1. **Raw/nano conversion rounded every balance over 28 significant digits.** `raw_to_nano` divided by
   `10**30` and `nano_to_raw` multiplied by it; Decimal arithmetic rounds to the context precision,
   28 digits, and a whole-XNO raw balance carries 31. Measured on `main`:

   ```
   raw in   1000000000000000000000000000001   ->  raw out 1000000000000000000000000000000   (1 raw short)
   raw in   3999999999999999999999999999999   ->  nano_str 4.000000000000000000000000000     (reports MORE than is held)
   raw in   12345678901234567890123456789012  ->  raw out 12345678901234567890123456790000   (988 raw MORE)
   ```

   The inflating direction is the dangerous one: a balance one raw short of 4 XNO is reported to an
   agent as 4 XNO, and converting that back asks for more than the account holds.
   `nano_to_raw("1.000000000000000000000000000001")` also rejected an exactly representable amount,
   because the precision guard compared it against its own rounded result. **Fixed** by rescaling the
   exponent; raised separately and **left open** — money code, see *Open*.

2. **A legacy `xrb_` address was accepted by the validator's regex, then decoded one character
   short.** `public_key_from_address` sliced the body at `address[5:]`, and `xrb_` is four characters,
   so every `xrb_` address raised `AssertionError: public key field wider than 256 bits`. Node
   tooling still returns `account` with the legacy prefix, which is why the regex accepts it. Second
   defect on the same line: `validate_address` is documented to return a bool and catches only
   `ValueError`, so that `AssertionError` escaped it — `if validate_address(addr):` raised rather
   than returning `False`. **Fixed**; raised separately and **left open** — key path, see *Open*.

## Not verified

- **The tag the README tells people to install is behind `main`.** `pip install …@v0.1.0` resolves to
  `028b385`; `main` is `e46315f`. So neither yesterday's fixes nor today's reach an installer until a
  new tag is cut. Cutting one is a release decision and is deliberately not taken here — it is the
  single most valuable thing an owner could do with this note.
- **The 6 `network`-marked tests**, unchanged from the 2026-09-24 position: they reach `rpc.nano.to`
  and live price sources, which this sandbox's proxy refuses. Nothing in either change touches a
  network path.
- **The live paid path end to end**, which needs `NANO_RPC_URL`/`NANO_RPC_KEY` and a funded wallet.
  An audit should not be moving money.
- **The published sdist and wheel** for `v0.1.0` were not downloaded or installed; only the tree was
  audited. Given the point above, they carry both defects.
- **The MCP registry identity**, deliberately untouched and still as the 2026-09-24 note left it:
  `server.json.mcpregistry` and the `<!-- mcp-name: io.github.PANDeveloper001/nano-mcp -->` marker
  still name the `PANDeveloper001` account, which is how the registry proves who owns the entry and
  is now inconsistent with where the repository lives. That is a publishing decision for a person.

## After

`python -m pytest -q -m "not network"` → **120 passed** on the units branch, **117 passed** on the
crypto branch, from 113 before, in a clean venv on the same clone.

## Open

Both code fixes are open pull requests, neither merged, each with its failing-then-passing evidence
in the body:

- *units: raw/nano conversion rounded every balance over 28 significant digits* — money code.
- *crypto: a legacy xrb_ address was accepted, then decoded one character short* — key path.

Both are ports of changes already merged in `dhyabi2/nano-mcp`, so the code in them is not new; what
is left to a person is the decision to run it here, and whether to cut a release afterwards.
