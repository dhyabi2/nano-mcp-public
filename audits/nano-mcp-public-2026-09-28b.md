# nano-mcp-public — code audit, 2026-09-28 (second run of the day)

Tree audited at `86e470f` (`main`). Fresh `.venv`, `pip install -e ".[dev]"`.

This is the `b` note for the day: another run audited `43d10d7` this morning and fixed a crafted
`payment-signature` header crash in `nano_mcp/httpx402.py`. Its note is left exactly as it stands.
Nothing here touches that file or that fix.

## Baseline, before any change

```
$ python -m pytest -m "not network" -q
137 passed, 4 skipped, 6 deselected
```

## Found

**`public_key` accepted a 64-byte input and echoed 32 of those bytes back as the "public key",
producing a valid-looking address that no private key can sign for.** This is the *same* defect
found in the sibling `dhyabi2/nano-mcp` this run, in this repository's own copy of `crypto.py:52`.
The function had no length check; it handed its argument straight to
`ed25519_blake2b.SigningKey`. That constructor refuses most wrong lengths — 0, 16, 31, 33 all raise
— but it also accepts the 64-byte `seed || verifying key` form, in which **bytes 32:64 are taken as
the verifying key verbatim**. Reproduced here, on this tree:

```
hex_key = "00"*31 + "01"
public_key(bytes.fromhex(hex_key)).hex()  -> c969ec348895a49e21824e10e6b829edea50ccc26a83ce8986a3b95d12576058
public_key(hex_key.encode()).hex()        -> 3030303030303030303030303030303030303030303030303030303030303031
                                             ^ ACCEPTED - and that is just the input's own tail, as ASCII
  address_from_public_key(that)           -> nano_1e3i81r51e3i81r51e3i81r51e3i81r51e3i81r51e3i81r51e3juqsa5t4t
  equals the correct key?                 -> False
```

That address is well-formed and its checksum verifies, so `validate_address` returns `True` and
nothing downstream objects. **There is no private key behind it** — those bytes were never a public
key, they were text. XNO paid to it is unspendable by anyone, permanently.

The two inputs that produce it are ordinary: `hex_key.encode()`, and `open(keyfile, "rb").read()` for
a key file holding 64 hex characters. The sibling function `derive_private_key` guards exactly this
slip already, which is what makes the gap an inconsistency rather than a policy:

```
derive_private_key(hex_key.encode())  -> ValueError: seed must be 32 bytes (64 hex chars)
public_key(hex_key.encode())          -> a wrong key, silently
```

**Why it matters more in this repository than in the sibling.** `public_key` is exported from
`nano_sdk`, and here it is the last step of the two functions that mint the address a *payer is told
to send XNO to*: `nano_mcp/oneshot.py:59` `derive_one_time_account` and `nano_sdk/buyer.py:89`
`derive_session_account`. Both are safe today — each checks `master_secret`/`master_seed` is `bytes`
of at least 16, and each derives with `hkdf_sha256(..., length=32)`, so `public_key` always receives
exactly 32 bytes from them. The exposure is a caller of the SDK, or any later call site that does not
happen to hand it a 32-byte value. A guard in `public_key` closes it for all three call sites at once
rather than relying on each to keep being careful.

**Fixed** by checking `len(private_key) != 32` after the hex-string branch and raising `ValueError`,
the same shape and message as `derive_private_key`. The change can only *refuse* more: all three
call sites (`crypto.py:120`, `buyer.py:89`, `oneshot.py:59`) pass 32 bytes, the two test call sites
pass a 64-hex *string* and a 32-byte key, and both accepted forms are pinned by the new tests.

Proved both directions. The two new tests against the unfixed `crypto.py` (stashed, same venv):

```
FAILED tests/test_crypto.py::test_the_ascii_bytes_of_a_hex_key_are_refused_not_silently_accepted
FAILED tests/test_crypto.py::test_public_key_accepts_exactly_32_bytes_and_refuses_every_other_length
```

With the fix: **139 passed, 4 skipped, 6 deselected** (137 before).

**One more change, forced by the first and not a separate finding.** `README.md:31` advertises
`# 138 offline tests collected`, and `tests/test_readme.py::test_the_offline_test_count_in_the_quickstart_is_the_count_pytest_collects`
checks that number against what `pytest --collect-only -m "not network"` actually collects. Adding two
tests made it 140, so the law failed until the README was corrected:

```
$ python -m pytest -q -m "not network" --collect-only -p no:cacheprovider
140/146 tests collected (6 deselected)
```

That law is doing precisely the job it was written for — it caught the drift in the same run that
caused it. One digit changed on line 31; no other README text was touched.

**Not merged.** `crypto.py` is the key path, which the standing instruction for these runs excludes
from self-merging, so this is a pull request left open for a person.

## Found, not fixed — for a person

**`validate_address` raises `TypeError` on any non-string input, though it is documented to return a
bool.** Present here as well as in the sibling — `crypto.py` calls `ADDRESS_RE.match(address)` before
anything narrows the type, and `validate_address` catches only `ValueError`:

```
validate_address(None)          -> TypeError: expected string or bytes-like object, got 'NoneType'
validate_address(addr.encode()) -> TypeError: cannot use a string pattern on a bytes-like object
public_key_from_address(None)   -> TypeError   (documented: "Raises ValueError if ... malformed")
```

The consequence that matters is the send path, which guards its destination through this function: a
`destination` read from a JSON body where the key was absent is `None`, and the caller gets a
`TypeError` instead of the wallet's own refusal — uncatchable by the `except ValueError` the docstring
tells them to write. Left for a person because it is a second change to the key path in one run and
the fix above should be reviewed on its own; the fix itself is one `isinstance` check.

## Checked and clean

- **The three `public_key` call sites** were read before the guard was added, to be sure it could not
  refuse anything the package itself does: `crypto.py:120` (a 32-byte derived key), `buyer.py:89` and
  `oneshot.py:59` (both HKDF with `length=32`, behind a `>= 16` bytes check on the master secret).
- **Type fuzzing of the rest of the exported surface.** `derive_private_key` refuses every seed length
  but 32. `nano_to_raw` honours its ValueError-only contract for `None`, `bytes`, `list`, `dict`,
  `bool`. Only the two address functions above break their stated contracts.
- **Packaging.** `pip install -e ".[dev]"` succeeds; the `LICENSE` and the `license` field are both
  present here (unlike the sibling).
- **No secrets in the tree.** The 64-hex-shaped lines are the published `docs.nano.org` all-but-one-
  zero seed vectors and keys derived from it. No `.env`, `.pem`, `.key` or token shape is tracked, and
  nothing of that kind is in the history a shallow clone carries.

## Not verified

- The 6 `network`-marked tests and the 4 skips. The `network` tests reach `rpc.nano.to` and the live
  price sources, which this sandbox's proxy answers `403`; the skips are the journal-plugin modules,
  not in this release. None of them imports the changed line.
- The live send and receive paths end to end: they need a funded wallet, and an audit should not move
  money.
