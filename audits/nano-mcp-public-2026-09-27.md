# nano-mcp-public — audit, 2026-09-27

A clone of `main` at `8a9030d`, a clean virtualenv, and the dependencies the package itself declares.
The offline suite was green before any change — **116 passed, 4 skipped, 6 deselected** — so this run
began from a repository whose own tests were satisfied.

The three previous audits read `nano_sdk/` (09-24, 09-25) and then the `nano_mcp/` payment surface
(09-26). This run read the half none of them had — `scorecard.py`, `journaldb.py`, `evidence.py`,
`paidtool.py`, `nano_sdk/buyer.py` — and then asked the question none of them had asked either:
**does the package install and import from the dependencies it declares?** It does not, and that is
the finding.

## Found and fixed

**`pyproject.toml` declared a floor for `mcp` that cannot import this package.** The metadata asked
for `mcp>=1.0`. `nano_mcp/server.py:19` and `nano_mcp/paidtool.py:41` both import
`mcp.server.mcpserver`, which does not exist in mcp 1.x — and `nano_mcp/__init__.py` imports
`paidtool`, so `import nano_mcp` itself fails, not merely the two modules. Measured in clean
virtualenvs:

```
mcp==1.9.0   from mcp.server.mcpserver import MCPServer
             -> ModuleNotFoundError: No module named 'mcp.server.mcpserver'
mcp==2.0.0   MCPServer OK; whole offline suite 116 passed, 4 skipped, 6 deselected
```

The floor is therefore exactly `2.0`: the earliest release at which the imports in this tree resolve,
confirmed by running the *whole* suite against `mcp==2.0.0` rather than only trying the import.

Why a declared floor that is too low is a real defect and not a tidiness point: pip only upgrades a
dependency when the metadata forces it. In an environment that already holds mcp 1.x — which is most
environments, since 1.x is what the majority of MCP projects pin — the resolver saw `>=1.0`
satisfied, left 1.9.0 in place, and the README's very first quickstart command died:

```
$ pip install -e .            # accepted: mcp 1.9.0 satisfies ">=1.0"
$ python -m nano_mcp.server
  File "nano_mcp/paidtool.py", line 41, in <module>
    from mcp.server.mcpserver import MCPServer
ModuleNotFoundError: No module named 'mcp.server.mcpserver'
```

**Fixed** by declaring `mcp>=2.0`, with the reason in a comment beside it. Proved against the same
pinned-1.9.0 environment:

```
$ pip install -e .            # with the fixed metadata
  Uninstalling mcp-1.9.0: Successfully uninstalled mcp-1.9.0
  Successfully installed mcp-2.2.0 ...
$ python -c "import nano_mcp, nano_sdk"
import nano_mcp OK
```

No behaviour, no payment logic, no key path and no amount is touched: one dependency bound, plus the
law below.

**The law** (`tests/test_dependency_floor.py`) is deliberately not a restatement of the constant it
guards. It scans `nano_mcp/` and `nano_sdk/` for every `mcp.*` submodule the source actually imports,
looks each up in a table of *measured* introduction versions, and requires the declared floor to
cover all of them — so importing from a newer part of the mcp SDK fails here rather than at a
stranger's install, and an unknown submodule fails loudly with instructions rather than passing
silently. A second case asserts the tree really does import under the installed mcp, so the floor
cannot be honest on paper and wrong in fact.

Against the unchanged `pyproject.toml`:

```
FAILED tests/test_dependency_floor.py::test_the_declared_mcp_floor_provides_every_submodule_the_source_imports
  AssertionError: pyproject.toml declares mcp>=1.0, but mcp.server.mcpserver
  (imported at ['nano_mcp/paidtool.py:41', 'nano_mcp/server.py:19']) first exists
  in mcp 2.0 - an install that satisfies the declared floor cannot import this package
1 failed, 1 passed
```

## After

```
$ python -m pytest -q -m "not network"
118 passed, 4 skipped, 6 deselected
```

116 before, in the same clean venv on the same clone.

## Checked and clean

- **The scorecard reproduces its own published figures.** `scorecard verify --raw scorecard/raw
  --published scorecard/published.json` → `verify PASS`, exit 0. Strategy law L6 holds against the
  committed data, and `build` on a scratch journal writes a manifest and figures without touching the
  network. The `--journal` JSON reader path runs (its `_Reader(JournalProto)` subclass of a
  `typing.Protocol` does instantiate on 3.11 — checked rather than assumed, since a Protocol subclass
  is a shape that can raise).
- **Every shipped module imports** under the dependency set the fixed metadata resolves: all eleven
  `nano_mcp.*` and all seven `nano_sdk.*` modules.
- **The one README fetch URL is this repository**, and the `v0.1.0` tag it pins really exists
  (`028b385`). The 403 a fetch of it returns from this sandbox is the egress proxy, not GitHub — the
  tag was confirmed through the API instead.
- **No secrets.** Every tracked file swept again; the 64-hex strings are block hashes, chain links,
  signature digests and the published `docs.nano.org` test vectors, as the 09-25 audit derived. No
  `.env`, `.pem` or `.key` is tracked or in the available history.

## Found, not fixed

- **The README's test count is wrong.** Line 31 says `# offline tests pass (123)`; the suite reports
  118 passing after this change (116 before it). A reader who runs the command and counts will think
  they broke five tests. Left for its own pull request, because it is a separate concern from the
  dependency bound and the number depends on which one lands first.
- **`nano_mcp/journaldb.py:61` drops a `payer` that is `None`, and the scorecard then counts that row
  as external.** `read_nano_tx` strips keys whose value is `None`, so a `nano_tx` journal row written
  without a `payer` arrives at `count_external_receipts` with no `payer` at all; `r.get("payer") not
  in own` is then `None not in own` → `True`, and the row is counted toward the headline share. The
  own-account gate is sound for rows that *have* a payer, and `evidence.append_nano_tx` always writes
  one — so this needs a row from another writer of the shared journal to bite, which is why it is
  reported rather than changed under a metric this audit cannot re-baseline.
- **`scorecard.py:251` documents a `--journal-db` default (`~/.hermes/nano-pulse/journal.db`) that the
  code never applies.** `default=None`, and `_receipts` only builds the DB reader `if args.journal_db
  is not None`, so the documented default is unreachable; `journaldb.default_journal_db()` exists and
  is what the help text describes, but nothing calls it from the CLI. Help text that describes a
  behaviour the code does not have, not a crash.
- **`nano_sdk/buyer.py:120-121` signs `repr(self.issued_at)` and `repr(self.expires_at)`** without
  coercing them to `float`. `Mandate` annotates both as `float` but a dataclass does not enforce it,
  so a mandate issued with an `int` timestamp signs `b"2000000000"` while the same mandate rebuilt
  with a float signs `b"2000000000.0"`, and verification fails. Nothing here serialises a mandate
  today, so it cannot be shown to break a user yet; it is a trap for whoever adds that. Not changed:
  `buyer.py` is the spend path.
- **`.github/workflows/publish.yml` still names the wrong owner** for the PyPI trusted publisher
  (`PANDeveloper001`, where the repository and the OIDC claim say `dhyabi2`), unchanged from 09-26. A
  release workflow, deliberately not edited.
- **Four pull requests from earlier audits are still open** and all touch money code or key paths, so
  they remain a person's call: `#3` units precision, `#4` legacy `xrb_` addresses, `#6` the approvals
  database inside the installed package, `#8` an address with a trailing newline.

## Not verified

- **The tag the README tells people to install is still behind `main`.** `pip install …@v0.1.0`
  resolves to `028b385`, which carries `mcp>=1.0` — so **the defect fixed here is live for anyone
  following the README right now**, and stays live until a new tag is cut. That is a release decision
  and not taken here, and it is the single most valuable thing an owner could do with these notes.
- **The 6 `network`-marked tests** and the live paid path: they reach `rpc.nano.to` and live price
  sources, which this sandbox's proxy refuses, and an audit should not be moving money.
- **The published sdist and wheel** for `v0.1.0` were not downloaded; only the tree was audited.
- **The MCP registry identity** (`io.github.PANDeveloper001/nano-mcp` in the README marker and in
  `server.json.mcpregistry`) is how the registry proves who owns the entry — a publishing decision,
  deliberately untouched.
