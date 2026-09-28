# nano-mcp-public — code audit, 2026-09-28 (second run)

Scope: the tree at `86e470f` (`main`) — the head the earlier run of the same day left behind, with its
`httpx402` payment-signature fix merged. This is the `b` file for the same reason
`nano-mcp-public-2026-09-25b.md` was: two audit runs of one repository on one day collide on the dated
path. Nothing in the earlier note was changed.

## Baseline, before any change

```
$ pip install -e ".[dev]"                 # succeeds
$ python -m pytest -m "not network" -q
137 passed, 4 skipped, 6 deselected
$ python -m build                         # sdist + wheel
Successfully built nano_mcp-0.1.0.tar.gz and nano_mcp-0.1.0-py3-none-any.whl
  wheel top level: ['nano_mcp', 'nano_sdk', 'nano_mcp-0.1.0.dist-info']
```

## Why this repository, and in this order

This run started from a defect proved in the sibling `dhyabi2/nano-mcp` an hour earlier. The two trees
share `nano_sdk/crypto.py` almost line for line, and the last three notes on both sides record fixes
landing on one and being raised separately on the other. **The first thing to do with a confirmed defect
is to look for it where the same code is published** — and this is the sibling that matters more: it
carries the `LICENSE`, the `nano_mcp/` server, a README with an install command, and
`.github/workflows/publish.yml`, so it is the one an outsider would actually install.

The defect is here. So is a second, separate problem in that workflow, below.

## Found and fixed

**`address_from_public_key` accepted a key of any length and silently returned a broken address.**
`crypto.py:64` documents "a 32-byte public key" and checked nothing. `_b32_fixedwidth(value, 52)` writes
exactly 52 characters whatever the integer is, so a longer key loses its high bytes and a shorter one is
zero-extended — while `checksum()` is taken over the bytes *as given*. Measured on this tree:

```
address_from_public_key(b"\x11"*33) -> 'nano_46aj46aj…ooceg87x'   accepted    validate_address False
address_from_public_key(b"\x11"*31) -> 'nano_111j46aj…w3i1mqas'   accepted    validate_address False
address_from_public_key(b"\x11"*16) -> 'nano_11111111…krxr6h9f'   accepted    validate_address False
address_from_public_key(b"")         -> 'nano_11111111…7rmwcs5x'   accepted    validate_address False
```

65 characters, the `nano_` prefix, nothing but alphabet characters. **The library's own validator rejects
its own encoder's output** — and because the result is a value rather than an exception, it reads as an
address everywhere a string is displayed, pasted into an invoice, written into a manifest or logged. The
error surfaces at the far end, at the node or at whoever was asked to pay, with nothing pointing back at
the call that made it.

Neither input is exotic. **64 bytes is what `open(keyfile, "rb").read()` returns for a file holding 64 hex
characters**; **31 bytes is the ordinary result of trimming leading zero bytes off an integer**.
`derive_private_key` guards exactly this shape, one screen above:

```
derive_private_key(hexkey.encode())      -> ValueError: seed must be 32 bytes (64 hex chars)
address_from_public_key(hexkey.encode()) -> an address-shaped string, silently
```

**Fixed** with one length check at the top of the function, raising `ValueError` with the same message
shape as `derive_private_key`. Nothing else is touched. It can only *refuse* more: the one internal call
site is `derive_account`, which passes a 32-byte key, and the second new test pins that no address the
library produces changes — `DOCS_PUB_EXPAND` still encodes to the documented example address and 25
derived accounts still encode to the address they were built with, as `bytes`, `bytearray` **and**
`memoryview`, so the check does not narrow the accepted types.

### Failing, then passing

Against the unfixed `crypto.py`:

```
FAILED tests/test_crypto.py::test_address_from_public_key_refuses_a_key_that_is_not_32_bytes
E           Failed: DID NOT RAISE ValueError
1 failed, 29 passed
```

With the fix:

```
$ python -m pytest -m "not network" -q
139 passed, 4 skipped, 6 deselected

$ python -m build
Successfully built nano_mcp-0.1.0.tar.gz and nano_mcp-0.1.0-py3-none-any.whl
```

The repository's own README-counting law failed in between, as it is designed to — adding two tests moved
the collected count. `README.md:31` is corrected here, **138 → 140 offline tests collected**.

**Not merged.** `crypto.py` is the key/address path, which the standing rule for these runs excludes from
self-merging, and the 6 `network`-deselected tests were not run here. Left open for a person, beside the
still-open **#12** (the same family, on `public_key`). *Note for whoever merges:* this branch and #12 both
append tests to the end of `tests/test_crypto.py` and will conflict there textually; neither touches the
other's function, so the resolution is keep-both in either order. The identical fix is raised on the
sibling as `dhyabi2/nano-mcp` **#11**.

## Found, not fixed — and the first one is the owner's to act on

**`publish.yml` documents a trusted publisher for a repository this is no longer.** The header block
tells the owner to register a pending PyPI publisher with

```
  Owner:           PANDeveloper001
  Repository name: nano-mcp-public
```

but this repository is now **`dhyabi2/nano-mcp-public`**. PyPI's trusted publishing matches the GitHub
OIDC claim on *owner*, *repository*, *workflow filename* and *environment*; the token this workflow
presents will carry `dhyabi2/nano-mcp-public`, which cannot match a publisher registered under the other
account. So a release published from here would build both artefacts and then fail the upload, and the
setup instructions the owner would follow are the reason. Two things follow from that and only a person
can decide them:

- the publisher must be registered for **`dhyabi2/nano-mcp-public`** (workflow `publish.yml`,
  environment `pypi`), and the `environment: pypi` must exist on this repository;
- **`PANDeveloper001` is the account under GitHub review**, so the instruction points at the wrong place
  twice over.

Nothing was changed: this is the publishing and release path, which these runs do not touch. It is
recorded in full so the first release attempt is not the thing that discovers it.
Also still true, and now decided by this file's existence: `nano-mcp` on PyPI is **404** (free, and a
pending publisher does not reserve it), while **both** this repository and `dhyabi2/nano-mcp` declare
`name = "nano-mcp"`, `version = "0.1.0"` with different code. This repository is the one with the publish
workflow, the MIT `LICENSE` and the `nano_mcp/` server, so it is the plausible distribution — but that is
a release decision, and the sibling's note carries the same open question.

**`project.license` as a TOML table is deprecated and will stop building.** setuptools 84 warns on every
build here:

```
WARNING `project.license` as a TOML table is deprecated
  Please use a simple string containing a SPDX expression for `project.license`.
  By 2027-Feb-18, you need to update your project and remove deprecated calls
  or your builds will no longer be supported.
```

The build succeeds today. `license = {text = "MIT"}` → `license = "MIT"` is the whole change, but it is a
licence declaration inside release metadata, so it is reported rather than edited.

## Checked and clean

- **No secrets, in the tree or in the history a shallow clone carries.** The 64-hex literals are the
  published `docs.nano.org` vectors and the keys and block hashes derived from them, in
  `tests/test_crypto.py` and `tests/test_block.py`; `.ledger/` holds public block hashes. No `.env`,
  `.pem` or `.key` is tracked, and `publish.yml` deliberately holds **no** credential — the build job's
  own name says so and only the publish job has `id-token: write`, which is the right shape.
- **Packaging.** `pip install -e ".[dev]"` succeeds; `python -m build` produces sdist and wheel; the
  wheel's top level is `nano_mcp`, `nano_sdk` and the `dist-info`, so `tests/`, `tools/` and
  `scorecard/` stay out as `packages.find` intends, with a sixth audit note added.
- **The dependency floor from 09-27 holds.** `mcp>=2.0` is still declared with its reason beside it, and
  `tests/test_dependency_floor.py` still passes.
- **The address decoder.** `\A…\Z` is still the anchor, so whitespace on either side is refused; `xrb_`
  still decodes; `derive_private_key` still refuses a seed that is not 32 bytes.
- **The whole offline suite**, twice: 137 before the change and 139 after, with nothing else moving.

## Not verified

- The 6 `network`-deselected tests and `tests/test_facilitator_live.py`'s live paths: they need the live
  `rpc.nano.to` node and `NANO_RPC_KEY`, which this sandbox has neither of. Nothing in this change is
  imported by them.
- The live send path end to end. It needs a funded wallet, and an audit should not be moving money.
- The trusted-publisher claim above is read from PyPI's documented matching rules and this repository's
  own URL, not from an attempted release. Nothing was published and no version was bumped.
