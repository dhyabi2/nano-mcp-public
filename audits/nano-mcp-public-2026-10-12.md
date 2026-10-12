# nano-mcp-public — audit 2026-10-12

Same lens as the previous audits: can an agent hold XNO and pay — or get paid — per tool
call with this code today, without being hurt? This pass was run after the two facilitator
pull requests of the day landed (#29 `840299c`-equivalent payload guards, #30 the hex-case
claim key), so the tree audited is `main` with both in it.

## What I checked

- The whole offline suite on the merged tree: **461 passed, 5 skipped** (`pytest -m "not network"`),
  463 collected, which is what the quickstart line in `README.md` claims and what
  `tests/test_readme.py` enforces.
- `nano_mcp/facilitator.py` verify → settle end to end: `_check_requirements`, the proof read,
  `verify_block_on_independent_endpoints`, and the single-use claim (`ClaimStore`, the
  canonicalising migration added by #30).
- The SDK send and receive paths: `nano_sdk/wallet.py` (`send`, `receive`), `nano_sdk/buyer.py`
  (`SessionWallet.send`), and the two node reads each builds a block from
  (`RpcClient.account_info`, `work_generate`).
- The twin: every file this repository shares with the private `nano-mcp` was diffed against it
  so a fix landing on one half does not sit unported on the other.

## What I found

**Every send AND every receive silently re-delegated the account.**

`RpcClient.account_info` (`nano_sdk/client.py:63`) called the node with no
`representative="true"`. A stock Nano node, and `rpc.nano.to` with it, answers `account_info`
with `frontier`, `balance`, `open_block`, `representative_block`, `block_count`,
`confirmation_height` and `modified_timestamp` — and **no `representative` key** unless that
flag is set. Both block builders read the key off that answer:

- `nano_sdk/wallet.py:155` `rep = info.get("representative") or self.representative` — in
  `send`, and again at `wallet.py:189` in `receive` (this repository has a `receive()` the
  private twin does not).
- `nano_sdk/buyer.py:298` `rep = info.get("representative", self.account.address)`.

The read therefore never found one, and the fallback ran every time: every `Wallet` block
signed `DEFAULT_REPRESENTATIVE`, and every `SessionWallet` send signed the sub-account's own
address. A Nano send and a Nano receive both carry the representative, so an agent that had
chosen its representative lost that choice on its next payment — and on every payment after
it — without anything in the API saying so.

No funds can be lost this way: the amount, the destination and the balance are untouched.
What moves is the account's delegated voting weight, which is the one field in a block this
code was writing without being asked to.

The same defect was found in the private twin earlier today and is open there as
`dhyabi2/nano-mcp#30`, where the node behaviour above was checked against the live node.

## What I fixed

On `fix/every-send-and-receive-rewrote-the-representative`, ported from that reviewed branch
and extended to this repository's `receive()`:

- `RpcClient.account_info` asks for the representative.
- `representative_from_info()` validates the node's answer — absent, null or blank means "none,
  take your fallback"; a non-string or a non-checksum-valid address is an `RpcError`, raised
  before `work_generate` is called and before anything is signed.
- `Wallet.representative` defaults to `None`, meaning "keep the account's current one".
  An address passed or assigned is an explicit choice and every block signs it.
  `Wallet._block_representative` decides this in one place for `send` and `receive`.
- `tests/test_send_keeps_the_representative.py`: **28 of its checks fail on `main`** and all
  pass with the change; the full offline suite stays green (461 passed).
- `README.md` documents the behaviour change, and its test-count line is updated to the 463
  the suite now collects.

**It is left open, not merged.** The change decides a field a send block carries, so by the
standing rule it is not a refusal-only fix and the merge is the owner's.

## Could not verify from here

- The live node behaviour (`account_info` with and without `representative=true`) is taken from
  the private twin's branch, which measured it today; this container's proxy answers 403 for
  `rpc.nano.to`, so it could not be re-measured here. `tests/test_pricing.py`'s live median
  check fails for the same reason, on `main` as well as on the branch.
- Nothing was broadcast: no funded wallet exists in this environment, so the send and receive
  paths are exercised against the suite's fake node only.
