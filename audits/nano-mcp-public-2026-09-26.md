# nano-mcp-public — audit, 2026-09-26

A clone of `main` at `46ed4f4`, a clean virtualenv, and the dependencies the package itself declares.
The suite was green before any change — **113 passed, 4 skipped, 6 deselected** — so this run began
from a repository whose own tests were satisfied.

The two previous audits (09-24, 09-25) read `nano_sdk/` closely and compared it against the sibling
`dhyabi2/nano-mcp`. This run went the other way and read the half they had not: the `nano_mcp/`
payment surface — `service.py`, `store.py`, `oneshot.py`, `pricing.py`, `facilitator.py`,
`httpx402.py`, `server.py` — with the question *what can a stranger who can reach these endpoints
make them do?*, because `facilitator.py` and `httpx402.py` are HTTP servers this package invites
people to self-host.

## What was checked

- The offline suite before and after the change, and the new tests against the unchanged files they
  accuse, so the difference is the fix and not the test.
- Both HTTP surfaces driven over a real in-process socket with hostile bodies, not through the test
  client only: `/verify`, `/settle`, and the resource server's `payment-signature` header.
- `service.verify_payment` and `store.claim` — the exactly-once path. The claim is a single
  `INSERT ... PRIMARY KEY` under one lock, every read serialises on the same lock, and a lost race
  returns `spent` rather than a second approval. **Sound; nothing changed.**
- `oneshot.derive_one_time_account` — HKDF-SHA256 per RFC 5869, `master_secret` length-checked, the
  request_id as `info`. Read only; key path, out of this audit's remit to change.
- `pricing.usd_to_xno_raw` — checked for the same Decimal-context rounding the 09-25 `units.py`
  finding was about. It **is** present (the division rounds at 28 significant digits before the
  `10**30` scaling) but the error is bounded at about one raw, i.e. 10^-30 XNO, and the `ROUND_CEILING`
  absorbs it. Not reported as a defect: it is unmeasurable to any user, and a pull request for it
  would be noise.
- `_onchain_paid`'s `type == "send"` branch (`service.py:146`) requires `entry["account"]` to equal
  the one-time address, which in an `account_history` response is the counterparty — so that branch
  can only fire on a self-send and is effectively dead. Harmless, and left alone: it refuses rather
  than over-approves.
- A secret sweep of every tracked file. **Clean.** 52 lines are 64-hex shaped; all are block hashes,
  ledger chain links, signature digests, or the published `docs.nano.org` test vectors the 09-25 audit
  already derived and confirmed belong to the documented example account and to no account this swarm
  holds. `tools/receive_funding.py` reads its seed from `NANO_AGENT_SEED`; nothing is hardcoded. No
  `.env`, `.pem` or `.key` is tracked, and none appears in the available history.

## Found and fixed

**Both x402 HTTP surfaces drop the connection on well-formed JSON that is not an object.** Every
handler reads its input with `.get()` immediately, and neither validated the shape first, so a bare
number, string, list or `null` raised `AttributeError` *inside* the request handler.
`BaseHTTPRequestHandler` has no answer for that: the thread dies, `socketserver` prints a traceback,
and the caller gets no response at all rather than a 400. Measured against `main`, over a real socket:

```
facilitator /verify  body=123      -> NO RESPONSE: RemoteDisconnected
facilitator /settle  body="x"      -> NO RESPONSE: RemoteDisconnected
facilitator /verify  body=[1,2]    -> NO RESPONSE: RemoteDisconnected
facilitator /verify  body={}       -> 200 {"isValid": false, "invalidReason": "bad-scheme", ...}
resource GET sig=b64("123")        -> NO RESPONSE: RemoteDisconnected
resource GET sig=b64("null")       -> NO RESPONSE: RemoteDisconnected
resource GET sig=b64("{}")         -> 402 {"error": "payment required", ...}
```

Five of five. The inputs are unauthenticated: `payment-signature` is a request header on the
protected route, and `/verify` and `/settle` are the facilitator's public POST surface. Nothing is
mis-approved — the failure is before any verification runs — but a self-hoster gets an endpoint a
stranger can make unresponsive one request at a time, with stack traces in the log, and an x402 client
that sends a slightly wrong payload sees a dropped connection instead of the 400 the wire expects.

**Fixed** at the two boundaries where the input first arrives, so the error paths that already exist
do their job:

- `httpx402.b64decode_json` raises `ValueError` when a `PAYMENT-*` header decodes to a non-object.
  `do_GET` already answers 400 on a `ValueError` there.
- `facilitator._read_json` raises `ValueError` when the POST body is not an object. `do_POST` already
  answers 400 on a `ValueError` there.

No verification logic, no amount, no address derivation and no approval decision is touched, and the
change can only turn a crash into a refusal.

Against the unfixed files:

```
FAILED tests/test_httpx402.py::test_payment_signature_that_is_valid_json_but_not_an_object_is_400
FAILED tests/test_httpx402.py::test_b64decode_json_refuses_a_non_object
FAILED tests/test_facilitator.py::test_post_body_that_is_valid_json_but_not_an_object_is_400
3 failed
```

## After

```
$ python -m pytest -q -m "not network"
116 passed, 4 skipped, 6 deselected
```

113 before, in the same clean venv on the same clone.

## Found, not fixed — for a person

**`.github/workflows/publish.yml` tells the owner to register the PyPI trusted publisher under the
wrong account.** Its setup comment says `Owner: PANDeveloper001`, `Repository name:
nano-mcp-public`. The repository lives at `dhyabi2/nano-mcp-public`, and the OIDC claim GitHub
presents at publish time carries the *actual* owner — so a pending publisher registered as the file
instructs will not match, and the first release upload will be rejected. This is a release workflow
and is deliberately not edited here; it needs one word changed by whoever owns the PyPI project.

**Three pull requests from earlier audits are still open**, and until they land the code in `main` is
not what an installer gets either way (see below): `#3` units precision (money code), `#4` legacy
`xrb_` addresses (key path), `#6` the approvals database inside the installed package.

## Not verified

- **The tag the README tells people to install is still behind `main`.** `pip install …@v0.1.0`
  resolves to `028b385`. Neither this change nor the three open pull requests reach an installer
  until a new tag is cut, which is a release decision and not taken here. It remains the single most
  valuable thing an owner could do with these notes.
- **The 6 `network`-marked tests**, unchanged from the two previous positions: they reach `rpc.nano.to`
  and live price sources, which this sandbox's proxy refuses. Nothing in this change touches a
  network path.
- **The live paid path end to end**, which needs `NANO_RPC_URL`/`NANO_RPC_KEY` and a funded wallet.
  An audit should not be moving money.
- **The published sdist and wheel** for `v0.1.0` were not downloaded; only the tree was audited.
- **The MCP registry identity**, deliberately untouched and unchanged from 09-24: `server.json.mcpregistry`
  and the `<!-- mcp-name: io.github.PANDeveloper001/nano-mcp -->` marker still name `PANDeveloper001`,
  which is how the registry proves who owns the entry. A publishing decision for a person.

## Addendum, same day — a second defect, raised separately and left open

**An address with a trailing newline crashes the decoder with `KeyError` instead of being refused.**
`crypto.py:24` anchors `ADDRESS_RE` with `$`, and in Python `$` also matches immediately before a
trailing newline, so `"nano_<60 chars>\n"` passes the regex and `_b32_decode` then reaches the `"\n"`,
which is not in the Nano alphabet. Measured on `main`:

```
validate_address(addr)        -> True
validate_address(addr + "\n") -> RAISED KeyError('\n')
```

`public_key_from_address` is documented to raise `ValueError` on a malformed address and
`validate_address` to return a bool catching only `ValueError`, so the `KeyError` escapes both — the
same contract violation the 09-24 sibling audit fixed on this function when an `AssertionError` was
escaping it. A trailing newline is the ordinary shape of an address read from a file or a config, and
`Wallet.send` validates its destination through this path.

Found while auditing `dhyabi2/nano-mcp`, whose `crypto.py` carries the identical line; the fix is
raised in both repositories and **left open in both**, because `crypto.py` is the address/key path
that these runs do not self-merge. The change is one anchor (`$` → `\Z`) and accepts nothing it did
not accept before, pinned by a second test over 25 derived accounts. Offline suite here: 116 → 118.

The `xrb_` half of that path is deliberately untouched by it — the fixed-offset slice is pull request
`#4`, still open — so the new test exercises `nano_` addresses only. `#3`, `#4` and this one all want
reviewing together, and a release cut afterwards.
