# nano-mcp-public — audit, 2026-09-24

Audited as published: a clone of `main`, a clean virtualenv, and the dependencies the package itself
declares (`httpx`, `ed25519-blake2b-fork`, `mcp>=1.0`, plus `pytest` from `[dev]`). The question
throughout was what happens to someone who follows the README, because this is a package other
people are invited to install.

## What was checked

- `import nano_mcp` and `import nano_sdk` on a clean install. **Both import cleanly.**
- The README's own documented commands, run verbatim, including
  `python -m pytest -m "not network"` and the pinned `uv pip install` line.
- `nano_mcp/paidtool.py:41`, `from mcp.server.mcpserver import MCPServer` — checked against the real
  dependency rather than assumed. `mcp.server.mcpserver.MCPServer` **exists** in `mcp` 2.2.0; this
  import is correct and was a false alarm.
- `nano_sdk/wallet.py` — the daily cap, the balance guard, `DEFAULT_DAILY_CAP_RAW` derived from
  `nano_to_raw("0.01")` rather than restated as a literal, and the two independent reads before a
  send. Read only; **nothing changed here**, money code is out of this audit's remit.
- `nano_sdk/crypto.py`, `units.py`, `block.py` via the offline suite.
- A secret sweep of the tree: seed-shaped hex, `nano_` addresses, token shapes, private key blocks.
  **Nothing found.** Every `nano_` address in the tree is a public one — the burn address, a faucet,
  and test payers — and the 64-character strings in `.ledger/` are block hashes and signature
  digests, not keys.

## Found

1. **The test suite could not run at all on a clean install.** `tests/test_evidence.py:14`,
   `tests/test_probe.py:23` and `tests/test_journaldb.py:23` each do
   `sys.path.insert(0, "/root/.hermes/plugins/nano-pulse")` and then `import journal`. That plugin
   lives on the swarm's own boxes; it is in neither this repository nor its dependencies. pytest
   treats a collection error as **fatal to the whole run**, so this did not fail three modules — it
   aborted everything:

   ```
   $ python -m pytest -m "not network"
   ERROR tests/test_evidence.py
   ERROR tests/test_journaldb.py
   ERROR tests/test_probe.py
   !!!!!! Interrupted: 3 errors during collection !!!!!!
   6 deselected, 3 errors in 0.90s
   ```

   Zero tests ran, against a README that says *"offline tests pass (123)"*. **Fixed** — the import
   is guarded and the three modules skip themselves when the plugin is absent, which is the
   convention this repository already uses twice: `pyproject.toml`'s `addopts` ignores two modules
   that are "intentionally not part of this public release", and the `network` marker skips the live
   ones. On a box that has the plugin, all three run exactly as before.

2. **The README's "Verified public install" command answers 403.** Raised separately; see the pull
   request titled *README: the pinned install command answers 403*.

## After

```
$ python -m pytest -m "not network"
111 passed, 4 skipped, 6 deselected in 11.77s
```

The three guarded modules hold 11 test functions between them, so the README's figure of 123 offline
tests is consistent with a box that has the plugin. The number was left alone rather than restated
downward, because the count on such a box could not be observed from here.

## Not verified

- **The 6 `network`-marked tests.** They reach `rpc.nano.to` and live price sources, which this
  sandbox's proxy refuses (`httpx.ProxyError: 403 Forbidden`). `python -m pytest` with network tests
  included therefore reports `1 failed, 111 passed, 9 skipped`, and that one failure,
  `test_pricing.py::test_live_median_of_three_sources_returns_numeric`, is the sandbox and not the
  code — it fails identically on `main`. Nothing in this change touches a network path.
- **The live paid path end to end.** `NANO_RPC_URL`/`NANO_RPC_KEY` and a funded wallet are needed,
  and an audit should not be moving money.
- **The published artefacts.** The sdist and wheel served as release assets for `v0.1.0` were not
  downloaded or installed; only the tree was audited.
- **The MCP registry identity**, deliberately untouched: `server.json.mcpregistry` and the
  `<!-- mcp-name: io.github.PANDeveloper001/nano-mcp -->` marker in the README still name the
  `PANDeveloper001` account, and that marker is how the registry proves who owns the entry. It is
  now inconsistent with where the repository actually lives. Changing it is a publishing decision,
  so it is reported here for a person rather than edited.
