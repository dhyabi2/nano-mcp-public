# nano-mcp-public — audit, 2026-09-25 (second pass)

A clone of `main` at `46ed4f4`, a clean virtualenv, the package's own declared dependencies plus
`pytest`. **113 passed, 4 skipped, 6 deselected** before any change, matching this morning's note.

That note covered `nano_sdk/` (units, crypto, block) by comparing against the sibling
`dhyabi2/nano-mcp`, and left two fixes as open pull requests. This pass took what it did not: the
**published artifact** — actually installed and run, which the earlier note lists under *Not
verified* — and `nano_mcp/`, the server side.

## What was checked

- The offline suite before and after the change.
- **The README's pinned install, run for real** in a fresh virtualenv:
  `pip install "nano-mcp @ git+https://github.com/dhyabi2/nano-mcp-public.git@v0.1.0"` →
  **succeeds**, and `import nano_mcp, nano_sdk` succeeds. The earlier note could not confirm this.
- Every command and import the README gives. `from nano_sdk import RpcClient, Wallet` and
  `from nano_sdk.crypto import derive_account` both resolve; `nano_mcp/server.py` has the `main()`
  and `__main__` guard that `python -m nano_mcp.server` needs.
- `nano_mcp/store.py` in full, and how `service.py` and `server.py` reach it.
- The injection surface across `nano_mcp/` and `nano_sdk/`: **no `subprocess`, `os.system`, `eval`,
  `exec`, `shell=True`, `pickle` or `yaml.load` anywhere.** The `open()`/`Path()` call sites are all
  in `scorecard.py`, on operator-supplied CLI arguments.
- A secret sweep of the tracked tree: **clean**, same result as this morning's hand-checked pass.

## Found and fixed

**The approvals database defaulted to a path inside the installed package, and a non-root install
cannot start the server at all.** `ApprovalStore.__init__` fell back to
`os.path.dirname(__file__)/approvals.db`. Measured on the real pinned install:

```
>>> ApprovalStore().path
'/tmp/pinned/lib/python3.11/site-packages/nano_mcp/approvals.db'

# the same install, run as an ordinary user — how a hardened container runs it
$ su probe -c '... -c "from nano_mcp.store import ApprovalStore; ApprovalStore()"'
sqlite3.OperationalError: unable to open database file
```

`server_from_env()` → `PaymentService()` → `ApprovalStore()` is the production path, and it takes
that branch. The suite never did: **every one of the nine test sites passes an explicit `path`**, so
the default was the one line in this file no test had executed. A stranger following the README's
own pinned install, on a system-wide or container install, gets a message naming nothing they can
act on.

Fixed with `default_store_path()`: `NANO_MCP_STORE` first, so an operator can always say; then an
**existing** package-directory database, so a deployment already running keeps its approvals —
forgetting them would make every already-approved `request_id` claimable a second time, which is the
one property this table exists to provide; then `$XDG_STATE_HOME/nano-mcp`, writable by the user the
server runs as and surviving a reinstall.

## After

`python -m pytest -q -m "not network"` → **117 passed, 4 skipped, 6 deselected** (113 + 4). Against
the unchanged `store.py` the headline law fails on its assertion, not on collection:

```
AssertionError: the approvals database defaults to
  /home/user/nano-mcp-public/nano_mcp/approvals.db, inside the installed package
```

## Urgent — the published release carries two known-wrong behaviours

`v0.1.0` resolves to commit `4adc719e`; `main` is `46ed4f48`. The README calls that pinned command
the *"Verified public install"*. It installs cleanly — and then behaves as follows, measured on the
installed package, not on the tree:

```
raw in  1000000000000000000000000000001   nano_str -> 1.000000000000000000000000000   (1 raw short)
raw in  3999999999999999999999999999999   nano_str -> 4.000000000000000000000000000   (reports MORE than is held)
validate_address("xrb_1e5aqeg…")          AssertionError: public key field wider than 256 bits
```

The inflating direction is the dangerous one: a balance one raw short of 4 XNO is reported to an
agent as 4 XNO. And `validate_address` is documented to return a bool, so the legacy-prefix case
raises out of a caller's `if`. Both already have open pull requests here (#3 units, #4 crypto), both
correctly left for a person — money code and a key path. **Until those merge and a new tag is cut,
every install anyone makes from the README carries both.** Cutting the release afterwards remains
the single most valuable thing an owner could do with these notes.

## Urgent — and it blocks the remedy above: the publish workflow names the wrong owner

`.github/workflows/publish.yml` uses PyPI trusted publishing (OIDC, no stored token), and its header
tells the owner to register the pending publisher as:

```
Owner:            PANDeveloper001
Repository name:  nano-mcp-public
```

This repository is **`dhyabi2/nano-mcp-public`**. PyPI matches a trusted publisher on the
`repository_owner` and `repository` claims in the OIDC token GitHub mints, so a publisher registered
as written cannot match a release published from here: the `publish` job would build the sdist and
wheel and then fail at the upload. There are **0 releases** on this repository, so the workflow has
never run and nothing has proved otherwise either way.

That matters because cutting a release is the remedy for everything above, and this is the step it
would fail on. Checked while here: `pypi.org/pypi/nano-mcp/json` still answers **404**, so the name
is free and unsquatted — the header's own warning that a pending publisher does not reserve it still
holds. Deliberately **not changed**: this is the release path, which an audit does not edit. Whether
the answer is to re-register the publisher under `dhyabi2` or to move the project back is the same
open question as the `io.github.PANDeveloper001/nano-mcp` registry identity below, now with a
consequence attached.

## Found, not fixed

- **The README states a test count that is wrong.** Line 31 says `# offline tests pass (123)`; the
  suite reports **113 passed, 4 skipped, 6 deselected** on `main` — 123 is the number *collected*,
  not the number that pass. A reader who runs it and counts 113 has no way to tell whether they
  broke something. Left alone because a bare count in prose rots by design and the honest repair is
  to drop it rather than re-state it, which is an editorial call; this repository already has the
  right precedent in `test_the_readme_does_not_promise_release_assets_that_are_not_published`.
- **`pyproject.toml` declares no `[build-system]`.** PEP 517 consumers fall back to the legacy
  setuptools backend, which is why the install above works, so this is not breaking anything today;
  declaring it is what stops it becoming a build failure on a future toolchain. The sibling
  `dhyabi2/nano-mcp` does not declare one either.
- **The MCP registry identity** is unchanged and still as both earlier notes left it:
  `server.json.mcpregistry` and the `<!-- mcp-name: io.github.PANDeveloper001/nano-mcp -->` marker
  name the `PANDeveloper001` account. A publishing decision for a person, not a broken link.

## Not verified

- **The live paid path end to end** — needs `NANO_RPC_URL`/`NANO_RPC_KEY` and a funded wallet. An
  audit should not be moving money.
- **The 6 `network`-marked tests**, which reach `rpc.nano.to` and live price sources; this sandbox's
  proxy refuses them. Nothing in this change touches a network path.
- **`facilitator.py`, `httpx402.py` and `paidtool.py` were read only in outline.** Their central
  claims — verification on ≥2 independent RPCs, failing closed, and settling exactly once — are the
  right target for the next pass and were not attacked here; the store's exactly-once claim, which
  they rest on, was.
- The read-only-install failure was reproduced by running as a second, unprivileged user. It was not
  reproduced on a genuinely read-only filesystem mount.
