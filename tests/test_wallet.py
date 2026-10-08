"""Wallet send-guard tests: never overdraw, never exceed the daily cap.

Uses a stub client so no network/funds are needed. The send pipeline reads
account_info (for balance + frontier + representative) then enforces both guards
before signing or broadcasting.
"""
import pytest

from nano_sdk import DailyCapExceeded, InsufficientBalance, Wallet, nano_to_raw

SEED = "00" * 32
DEST = "nano_1q3hqecaw15cjt7thbtxu3pbzr1eihtzzpzxguoc37bj1wc5ffoh7w74gi6p"
REP = "nano_1stofnrxuz3cai7ze75o174bpm7scwj9jn3nxsn8ntzg784jf1gzn1jjdkou"


class StubClient:
    """Returns a fixed account_info and records calls. No network access."""

    def __init__(self, balance_raw: int, frontier: str = "AB" * 32):
        self.balance_raw = balance_raw
        self.frontier = frontier
        self.pending_amount = 0
        self.calls: list[dict] = []
        self.process_result = {"hash": "CD" * 32}

    def account_info(self, account: str) -> dict:
        return {
            "balance": str(self.balance_raw),
            "frontier": self.frontier,
            "representative": REP,
        }

    def block_info(self, block_hash: str) -> dict:
        return {"amount": str(self.pending_amount)}

    def call(self, **payload) -> dict:
        self.calls.append(payload)
        if payload["action"] == "process":
            return self.process_result
        if payload["action"] == "work_generate":
            return {"work": "0000000000000000"}
        raise AssertionError(f"unexpected action {payload}")


def test_send_within_balance_and_cap_publishes():
    client = StubClient(balance_raw=int(nano_to_raw("0.005")))
    w = Wallet(seed=SEED, client=client)
    amount = int(nano_to_raw("0.001"))
    blob = client.process_result["hash"]
    h, blk = w.send(DEST, amount)
    assert h == blob
    # guard recorded the spend against the daily cap
    assert w._sent_today() == amount
    # process payload carried subtype=send and a json block
    proc = [c for c in client.calls if c["action"] == "process"][0]
    assert proc["subtype"] == "send"
    assert proc["block"]["balance"] == str(client.balance_raw - amount)


def test_send_over_balance_raises_without_publishing():
    client = StubClient(balance_raw=int(nano_to_raw("0.001")))
    w = Wallet(seed=SEED, client=client)
    with pytest.raises(InsufficientBalance):
        w.send(DEST, int(nano_to_raw("0.002")))
    # nothing was published and nothing recorded
    assert not [c for c in client.calls if c["action"] == "process"]
    assert w._sent_today() == 0


def test_send_over_daily_cap_raises_without_publishing():
    cap = int(nano_to_raw("0.01"))
    client = StubClient(balance_raw=int(nano_to_raw("0.1")))
    w = Wallet(seed=SEED, client=client, daily_cap_raw=cap)
    # filling the cap first: exact amount equals the cap -> allowed
    w.send(DEST, cap)
    assert w.available_today() == 0
    # any further send must exceed the cap
    with pytest.raises(DailyCapExceeded):
        w.send(DEST, int(nano_to_raw("0.000001")))
    # still only the first send was published
    assert len([c for c in client.calls if c["action"] == "process"]) == 1


def test_daily_cap_is_per_day_not_total_over_lifetime():
    """The cap is a rolling-day limit; a fresh day resets the spend tracker."""
    cap = int(nano_to_raw("0.01"))
    client = StubClient(balance_raw=int(nano_to_raw("0.5")))
    w = Wallet(seed=SEED, client=client, daily_cap_raw=cap)
    w.send(DEST, cap)
    assert w.available_today() == 0
    # simulate a new day
    w._day -= 1
    w._spend = {}
    assert w.available_today() == cap


def test_check_send_rejects_negative_and_zero():
    client = StubClient(balance_raw=int(nano_to_raw("0.1")))
    w = Wallet(seed=SEED, client=client)
    with pytest.raises(ValueError):
        w.check_send(0, int(nano_to_raw("0.1")))
    with pytest.raises(ValueError):
        w.check_send(-5, int(nano_to_raw("0.1")))


def test_invalid_destination_raises():
    client = StubClient(balance_raw=int(nano_to_raw("0.1")))
    w = Wallet(seed=SEED, client=client)
    with pytest.raises(ValueError):
        w.send("not_a_nano_address", int(nano_to_raw("0.001")))


# -- receive ---------------------------------------------------------------

def test_receive_publishes_receive_block_and_increases_balance():
    """Receiving a pending send builds a receive block (link = source hash),
    publishes with subtype=receive, and is not counted against the daily cap."""
    client = StubClient(balance_raw=int(nano_to_raw("0.001")))
    client.pending_amount = int(nano_to_raw("0.5"))
    w = Wallet(seed=SEED, client=client)
    src = "AB" * 32
    h, blk = w.receive(src)
    assert h == client.process_result["hash"]
    # process payload carried subtype=receive and the source hash as link
    proc = [c for c in client.calls if c["action"] == "process"][0]
    assert proc["subtype"] == "receive"
    assert proc["block"]["link"] == src.upper()
    # new balance = old + received amount
    assert proc["block"]["balance"] == str(
        int(nano_to_raw("0.001")) + int(nano_to_raw("0.5"))
    )
    # receiving is not an outgoing spend -> daily cap untouched
    assert w._sent_today() == 0


def test_receive_rejects_bad_source_hash():
    client = StubClient(balance_raw=int(nano_to_raw("0.001")))
    w = Wallet(seed=SEED, client=client)
    with pytest.raises(ValueError):
        w.receive("not-a-64-hex-hash")
    assert not [c for c in client.calls if c["action"] == "process"]


def test_receive_open_account_uses_account_public_key_as_work_root():
    """For an open (first) block, the PoW work root must be the account public
    key, not the (all-zero) previous frontier and not a block hash."""
    from nano_sdk import derive_account

    acct = derive_account(SEED, 0)
    client = StubClient(balance_raw=0, frontier="0" * 64)  # open account
    client.pending_amount = int(nano_to_raw("0.5"))
    w = Wallet(seed=SEED, client=client)
    src = "AB" * 32
    h, blk = w.receive(src)
    assert h == client.process_result["hash"]
    # work was generated over the account public key for the open block
    wg = [c for c in client.calls if c["action"] == "work_generate"][0]
    assert wg["hash"] == acct.public_key.hex()
    proc = [c for c in client.calls if c["action"] == "process"][0]
    assert proc["subtype"] == "receive"
    assert proc["block"]["previous"] == "0" * 64
    # open-block receive is still not an outgoing spend
    assert w._sent_today() == 0


def test_wallet_repr_does_not_leak_the_seed():
    """`repr(Wallet)` must never carry the seed.

    `Account.__repr__` is deliberately overridden so a private key cannot reach a
    log. `Wallet` holds the *seed*, from which every account's private key
    derives, so it needs the same guard: a dataclass repr prints every field, and
    anything that reprs locals (a traceback with locals, `pytest -l`,
    `logging.exception`, a debugger, an agent's own run log) carried the seed with
    it. This is the published package, so the leak shipped to whoever installed it.
    """
    seed = "9f" + "0e" * 31
    w = Wallet(seed=seed, client=StubClient(balance_raw=1))
    for rendered in (repr(w), str(w), f"{w}", "%r" % (w,)):
        assert seed not in rendered
        assert "seed" not in rendered
    # bytes seeds must be hidden too, and not merely re-encoded
    wb = Wallet(seed=bytes.fromhex(seed), client=StubClient(balance_raw=1))
    assert seed not in repr(wb)
    assert bytes.fromhex(seed).hex() not in repr(wb)
    # the seed is still held and usable -- only its printing changed
    assert w.seed == seed
    assert w.account(0).address.startswith("nano_")


def test_a_bool_amount_does_not_publish_a_send_of_one_raw():
    """`True` is an `int` in Python and `True > 0`, so it passed every guard and
    `raw_balance - True` signed and published a real send block of 1 raw."""
    cap = int(nano_to_raw("0.01"))
    client = StubClient(balance_raw=cap)
    w = Wallet(seed=SEED, client=client)
    with pytest.raises(ValueError):
        w.send(DEST, True)
    assert [c["action"] for c in client.calls] == [], (
        "a bool amount reached work_generate/process"
    )


def test_check_send_rejects_an_amount_that_is_not_an_integer_count_of_raw():
    """`check_send` is the guard that refuses *before* anything is signed or
    published, so a non-integer amount has to be refused here, not later.

    A float cannot hold a raw amount: at 10**30 raw to the XNO, binary floating
    point has no exact representation (`int(1e26)` is 100000000000000004764729344,
    4764729344 raw out). `block.py` catches it eventually, inside
    `balance_raw.to_bytes(16, "big")`, but only as an `AttributeError` and only
    after a `work_generate` round-trip has already been spent.
    """
    from decimal import Decimal

    cap = int(nano_to_raw("0.01"))
    for amount in (1e27, 0.001, Decimal(10 ** 27), "1000", b"1000", None, 1 + 0j):
        client = StubClient(balance_raw=cap)
        w = Wallet(seed=SEED, client=client)
        with pytest.raises(ValueError):
            w.check_send(amount, cap)


def test_a_non_integer_amount_is_refused_before_work_or_process():
    """The refusal must come before the node is touched at all, so no proof of
    work is spent and no block is built for an amount that cannot be one."""
    cap = int(nano_to_raw("0.01"))
    client = StubClient(balance_raw=cap)
    w = Wallet(seed=SEED, client=client)
    with pytest.raises(ValueError):
        w.send(DEST, 1e24)
    assert [c["action"] for c in client.calls] == [], (
        "a float amount reached work_generate/process"
    )
