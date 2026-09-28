# nano-mcp-public — audit 2026-09-28

Tree audited at `43d10d7` (branch `main`). Environment: Python 3.11.15, a fresh
`.venv` with `pip install -e '.[dev]'`.

## Baseline

`pytest -q -m "not network"` — **136 passed, 4 skipped, 6 deselected**, before any
change. The 6 deselected and 1 further failure under a plain `pytest -q` are the
`network`-marked tests, which reach `rpc.nano.to` and the live price sources; this
sandbox's proxy answers them `403`, so they are not run here and nothing this audit
says rests on them.

## What was checked

- `nano_sdk/crypto.py`, `nano_sdk/units.py` — address encode/decode, the `\A…\Z`
  anchoring, the `split("_", 1)` body extraction, and the exponent-rescaling raw
  conversions. Re-read against the two defects earlier audits fixed there; both
  fixes are present and the reasoning holds.
- `nano_mcp/httpx402.py` — the full x402 resource-server wire, byte by byte:
  header decode, `derive_requirements`, `_accepted_matches`, `complete()` and the
  `BaseHTTPRequestHandler` adapter. **One defect found and fixed — see below.**
- `nano_mcp/oneshot.py` — HKDF-SHA256 expansion against RFC 5869, and the
  `master_secret` length gate.
- `nano_mcp/evidence.py`, `nano_mcp/journaldb.py` — the own-account gate and the
  read-only (`mode=ro`) journal reader.
- Whole tree grepped for `subprocess` / `os.system` / `shell=True` / `eval` / `exec`
  reached from library code: none. The only `subprocess` calls are in `tools/`
  evidence scripts and all pass an argument list, never a shell string.
- Whole tree grepped for path construction from caller input: `journaldb`,
  `facilitator` and `scorecard` all take an explicit path from their own CLI or
  constructor; nothing joins an untrusted component onto a filesystem path.

## Found and fixed

**`nano_mcp/httpx402.py:199` — a crafted `payment-signature` header crashed the
resource server's request handler, and the client got no response at all.**

`b64decode_json` already refuses a header that decodes to JSON which is not an
object, with a docstring explaining exactly why: every caller goes straight to
`.get()`, so a non-object would raise `AttributeError` inside `do_GET` and drop
the connection instead of answering. That check stopped at the *top level*.
`complete()` then did:

    accepted = payload.get("accepted") or {}
    request_id = str((accepted.get("extra") or {}).get("requestId") or "")

`payload["accepted"]` and `accepted["extra"]` are attacker-supplied too, and
neither was checked for being an object. A header carrying `{"accepted": "zzz"}`,
`{"accepted": 1}`, `{"accepted": ["a"]}`, `{"accepted": {"extra": "zzz"}}` or
`{"accepted": {"extra": ["a"]}}` raised `AttributeError` — which
`BaseHTTPRequestHandler` does not handle, so `socketserver` printed a traceback
and closed the socket.

Shown against a live in-process server on `127.0.0.1`, before the fix:

    well-formed-but-empty: HTTP 402
    accepted is a string : NO RESPONSE -> RemoteDisconnected: Remote end closed
                                          connection without response
    extra is a string    : NO RESPONSE -> RemoteDisconnected: ...

with this traceback on the server for each:

    File "nano_mcp/httpx402.py", line 200, in complete
      request_id = str((accepted.get("extra") or {}).get("requestId") or "")
    AttributeError: 'str' object has no attribute 'get'

Any unauthenticated client can send that header; the server is the self-hostable
one the README tells a seller to run.

**The change** adds the same object-shape check the top level already has, to
`accepted` and to `accepted.extra`, each returning the module's existing
`invalid` refusal shape. It can only make the server refuse *more*: a payload
that reached verification before still reaches it, because the guard fires only
where the old code raised. No amount, address, verification or settlement logic
is touched.

**The law**: `tests/test_httpx402.py::test_payment_signature_whose_accepted_is_not_an_object_is_refused`
drives all five shapes over the real HTTP wire and requires a `402` for each. It
fails on the tree without the fix (`AttributeError` in `do_GET`, no response) and
passes with it.

`README.md:31` advertises the offline collection count and
`tests/test_readme.py::test_the_offline_test_count_in_the_quickstart_is_the_count_pytest_collects`
enforces it, so the number moves 137 → 138 with the new test. That is the only
other line changed.

After: `pytest -q -m "not network"` — **137 passed, 4 skipped, 6 deselected**.

## Found, not fixed

Everything carried forward from 09-27 still stands and is unchanged here — the
`journaldb.py:61` `payer`-stripping and its effect on `count_external_receipts`,
the unreachable `--journal-db` default at `scorecard.py:251`, the `repr()` of a
possibly-`int` timestamp in `buyer.py:120`, and the wrong owner in the PyPI
trusted-publisher block of `.github/workflows/publish.yml`. Three of those are in
money or release paths and the fourth needs a metric re-baseline, so they remain a
person's call rather than this audit's.

The four pull requests opened by earlier audits (`#3`, `#4`, `#6`, `#8`) are still
open for the same reason.

## Not verified

- The 6 `network`-marked tests and the live paid path: the sandbox proxy refuses
  `rpc.nano.to` and the price sources, and an audit should not be moving money.
- **The tag the README tells people to install is still behind `main`.** This is
  unchanged from 09-27 and remains the single most valuable thing an owner could do
  with these notes: `pip install …@v0.1.0` does not carry the fixes merged since.
- The published sdist and wheel for `v0.1.0` were not downloaded; only the tree was
  audited.
