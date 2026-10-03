# nano-mcp-public — audit, 2026-09-29

Audited at `86e470f` (head of `main`). Python 3.11.15, `pip install -e ".[dev]"`,
`pytest -m "not network"`: **137 passed, 4 skipped, 6 deselected** before any change.
The skips are the three journal-plugin modules the release does not carry; the
deselected are the live `rpc.nano.to` tests, which need `NANO_RPC_URL`/`NANO_RPC_KEY`.

## What was checked

- **The README, as a reader runs it.** Every fetch URL, the pinned install line, the
  advertised offline-test count, and — new this run — the one Python example.
- `nano_mcp/server.py` — the stdio entry point and its six tools; `nano_mcp/service.py`
  (quote, quote_usd, verify_payment); `nano_mcp/oneshot.py` (HKDF-SHA256 one-time
  address derivation); `nano_mcp/store.py`, `pricing.py`.
- `nano_sdk/` — crypto, units, block, wallet, buyer.
- **Injection sinks:** grepped the whole of `nano_mcp/` and `nano_sdk/` for
  `subprocess`, `os.system`, `shell=True`, `eval`, `exec`, `pickle`, `yaml.load` and
  request-derived `open()`/path joins. **None.** The only `open()` calls are
  `scorecard.py:67` and `:79`, on operator-supplied paths.
- `derive_one_time_account` refuses a master secret shorter than 16 bytes
  (`oneshot.py:56`), so a weak `NANO_PAYMENT_MASTER_SECRET` cannot silently make the
  one-time payment addresses guessable. The HKDF is RFC 5869 and correct.
- Secret scan of the tree: nothing found.

## Found and fixed (this pull request)

**The README's only Python example crashes on its last line.** It read:

```python
wallet = Wallet(seed=SEED, client=RpcClient())
print(wallet.balance())
```

`Wallet` has no `balance`. It has `balance_raw(index=0)` (`nano_sdk/wallet.py:104`).
So the first thing a reader runs after installing the package ends in

    AttributeError: 'Wallet' object has no attribute 'balance'

with nothing to tell them whether they mistyped it, installed the wrong version, or
found a bug. The existing README laws covered the fetch URLs, the release-assets claim
and the test count — not whether the code in the README runs.

Fixed by calling the method that exists, and pinned by a law that **extracts the
example from README.md and executes it**, with the seed supplied and `RpcClient`
replaced by a stub, so it is offline and touches no funds. Against the unfixed README
the law fails at `README.md#python:9` with the AttributeError above; with the fix the
suite is **138 passed, 4 skipped, 6 deselected**.

## Found, fix proposed separately — branch `fix/quote-precision-context`

**`usd_to_xno_raw` returns a different amount depending on a process-global that
nothing in this codebase sets.** `pricing.py:102-103` computes the quote with Decimal
division and multiplication, both of which round to `decimal.getcontext().prec`
(28 significant digits by default) — while a raw XNO amount carries 31. This is the
same fault `nano_sdk/units.py` documents having fixed for balances, not carried across
to the quote. Measured on the repository's own law case, $1.00 at 0.34 USD/XNO:

    exact ceiling                 2941176470588235294117647058824
    as computed, prec=28          2941176470588235294117647059000   (+176)
    as computed, prec=50          2941176470588235294117647058824

For an `exact`-scheme payment the amount is the contract: two processes that agree on
the price and the rate can still disagree on what must be paid, and the facilitator
refuses the difference. The module also promises "no precision is ever lost" and
"rounded UP … so the seller never under-receives"; at the default precision it rounds
**down** in some cases ($1 at 3 USD/XNO: 34 raw short of the ceiling).
`test_usd_to_xno_raw_rounds_up_never_underpays` does not catch it because it builds its
expected value from the implementation's own rounded expression.

That change is in the amount a buyer must pay, so it is **not** self-merged — it is a
separate pull request for a person to read.

## Not changed

`fetch_median_xno_usd` requires 2 of 3 sources and, with exactly two, the "median" is
their mean — so one bad source moves the quote by half its error, which is not what
"one API outage cannot skew the median" says. Making that safe means outlier rejection
or a 3-source floor: a design decision, not a fix, so it is reported and left alone.
