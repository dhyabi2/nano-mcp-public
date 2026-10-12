"""A send must not re-delegate the account it pays from.

Both send paths carry the account's existing representative into the block they
sign:

    nano_sdk/wallet.py   rep = self._block_representative(info) # explicit rep, else node's, else default
    nano_sdk/buyer.py    rep = representative_from_info(info) or self.account.address

A fallback is right for an unopened account, which has no representative yet.
But `account_info` was called without `representative="true"`, and a Nano node
-- rpc.nano.to included -- does not send that key unless asked. So the read
NEVER found a representative and the fallback was taken on every send: a
`Wallet` re-pointed the account at `DEFAULT_REPRESENTATIVE` (NanoFusion), and a
`SessionBuyer` re-pointed the sub-account at itself. Signed, published, and
invisible: the representative is a hash-covered field of the block, so this is a
real change to the ledger every single time money moves.

Nothing noticed because every stub in the suite answers `account_info` with a
`representative` key the real node never sends. `FaithfulNode` below answers the
way the node actually does -- the field appears only when the request asks for
it -- so these tests fail unless the request asks.
"""
import os

import httpx
import pytest

from nano_sdk import Wallet, nano_to_raw
from nano_sdk.client import RpcClient

SEED = "00" * 32
DEST = "nano_1q3hqecaw15cjt7thbtxu3pbzr1eihtzzpzxguoc37bj1wc5ffoh7w74gi6p"
# The representative the account actually has on chain: not NanoFusion, and not
# the account's own address, so whichever fallback is taken is visible.
CHOSEN_REP = "nano_3patrick68y5btibaujyu7zokw7ctu4onikarddphra6qt688xzrszcg4yuo"
FRONTIER = "AB" * 32


class FaithfulNode:
    """`httpx.post` replacement that answers like rpc.nano.to.

    Checked against the live node on 2026-10-12: `account_info` returns
    `account_version, balance, balance_nano, block_count, confirmation_height,
    confirmation_height_frontier, frontier, modified_timestamp, open_block,
    representative_block` -- and `representative` ONLY when the request carries
    `representative: "true"`.
    """

    def __init__(self, balance_raw: int, rep: str | None = None):
        self.balance_raw = balance_raw
        self.rep = rep
        self.payloads: list[dict] = []

    def __call__(self, url, **kwargs):
        payload = kwargs["json"]
        self.payloads.append(payload)
        action = payload.get("action")
        if action == "account_info":
            body = {
                "frontier": FRONTIER,
                "open_block": FRONTIER,
                "representative_block": FRONTIER,
                "balance": str(self.balance_raw),
                "modified_timestamp": "1700000000",
                "block_count": "3",
                "account_version": "2",
                "confirmation_height": "3",
                "confirmation_height_frontier": FRONTIER,
            }
            if self.rep is not None and str(payload.get("representative", "")).lower() == "true":
                body["representative"] = self.rep
            return httpx.Response(200, json=body)
        if action == "work_generate":
            return httpx.Response(200, json={"work": "0000000000000000"})
        if action == "process":
            return httpx.Response(200, json={"hash": "CD" * 32})
        raise AssertionError(f"unexpected action {payload}")

    def sent(self, action: str) -> dict:
        return [p for p in self.payloads if p.get("action") == action][0]


@pytest.fixture
def node(monkeypatch):
    def _node(balance_raw: int, rep: str | None = CHOSEN_REP) -> FaithfulNode:
        """`rep=None` answers with no representative key at all (an unopened
        account); `rep=""` answers with a blank one."""
        n = FaithfulNode(balance_raw, rep)
        monkeypatch.setattr(httpx, "post", n)
        return n
    return _node


def test_account_info_asks_the_node_for_the_representative(node):
    n = node(int(nano_to_raw("0.005")))
    RpcClient(url="https://rpc.invalid").account_info(DEST)
    assert str(n.sent("account_info").get("representative", "")).lower() == "true", (
        "without representative=true the node sends no representative, so every "
        "caller that reads one finds nothing"
    )


def test_account_info_actually_returns_a_representative(node):
    node(int(nano_to_raw("0.005")))
    info = RpcClient(url="https://rpc.invalid").account_info(DEST)
    assert info.get("representative") == CHOSEN_REP


def test_a_send_keeps_the_accounts_own_representative(node):
    """The whole point: the published block must not re-delegate the account."""
    n = node(int(nano_to_raw("0.005")))
    w = Wallet(seed=SEED, client=RpcClient(url="https://rpc.invalid"))
    _, blk = w.send(DEST, int(nano_to_raw("0.001")))
    assert blk["representative"] == CHOSEN_REP, (
        f"the send re-delegated the account to {blk['representative']}"
    )
    assert n.sent("process")["block"]["representative"] == CHOSEN_REP


@pytest.mark.parametrize("rep,label", [(None, "no representative key"), ("", "a blank one")])
def test_an_answer_with_no_usable_representative_still_falls_back(node, rep, label):
    """The case the fallback is for, and the reason it is `or` and not a default.

    An unopened account has no representative, and `get(k, d)` would have handed
    a blank string straight to `public_key_from_address`. Both answers must take
    the wallet's own default instead, and the block must still be signable.
    """
    node(int(nano_to_raw("0.005")), rep=rep)
    w = Wallet(seed=SEED, client=RpcClient(url="https://rpc.invalid"))
    _, blk = w.send(DEST, int(nano_to_raw("0.001")))
    from nano_sdk.wallet import DEFAULT_REPRESENTATIVE

    assert w.representative is None
    assert blk["representative"] == DEFAULT_REPRESENTATIVE, label
    assert len(blk["signature"]) == 128


def test_a_session_send_keeps_the_sub_accounts_representative(node):
    """The buyer SDK has the same read, with a worse fallback.

    `nano_sdk/buyer.py`'s default was the sub-account's OWN address, so every
    session payment re-delegated the sub-account to itself -- an account with no
    voting weight behind it.
    """
    import time as _time

    from nano_sdk.buyer import SessionWallet, derive_session_account, issue_mandate
    from nano_sdk.crypto import derive_account

    master = bytes.fromhex("22" * 32)
    owner = derive_account(master, 0)
    now = _time.time()
    acct = derive_session_account(master, "sess-rep")
    mandate = issue_mandate(
        owner.private_key, "sess-rep", acct.address, int(nano_to_raw("0.003")),
        expires_at=now + 3600, issued_at=now,
    )

    node(int(nano_to_raw("0.005")))
    wallet = SessionWallet(
        acct, owner.public_key, mandate,
        client=RpcClient(url="https://rpc.invalid"), clock=lambda: now,
    )
    _, blk = wallet.send(DEST, int(nano_to_raw("0.001")))
    assert blk["representative"] == CHOSEN_REP, (
        f"the session send re-delegated the sub-account to {blk['representative']}"
    )
    assert blk["representative"] != acct.address


@pytest.mark.network
@pytest.mark.skipif(not os.environ.get("NANO_RPC_URL"), reason="NANO_RPC_URL not set")
def test_the_live_node_sends_no_representative_unless_asked():
    """The fact the whole fix rests on, read from the node itself."""
    client = RpcClient()
    bare = client.call(action="account_info", account=CHOSEN_REP)
    assert "representative" not in bare, (
        "rpc.nano.to now sends a representative unasked; re-read this fix"
    )
    assert client.account_info(CHOSEN_REP).get("representative")


# -- an explicitly chosen representative still wins ---------------------------

def test_an_explicit_representative_is_used_over_the_nodes(node):
    """`Wallet(representative=X)` is the SDK's only way to choose a
    representative; the node-reported one must not silently override it."""
    explicit = DEST  # any valid address that is neither CHOSEN_REP nor the default
    n = node(int(nano_to_raw("0.005")))
    w = Wallet(seed=SEED, client=RpcClient(url="https://rpc.invalid"), representative=explicit)
    _, blk = w.send(DEST, int(nano_to_raw("0.001")))
    assert blk["representative"] == explicit
    assert n.sent("process")["block"]["representative"] == explicit


def test_an_explicit_default_representative_is_still_explicit(node):
    """Passing DEFAULT_REPRESENTATIVE by name is a choice, not "no choice"."""
    from nano_sdk.wallet import DEFAULT_REPRESENTATIVE

    node(int(nano_to_raw("0.005")))
    w = Wallet(seed=SEED, client=RpcClient(url="https://rpc.invalid"),
               representative=DEFAULT_REPRESENTATIVE)
    _, blk = w.send(DEST, int(nano_to_raw("0.001")))
    assert blk["representative"] == DEFAULT_REPRESENTATIVE


def test_assigning_a_representative_after_construction_is_used(node):
    node(int(nano_to_raw("0.005")))
    w = Wallet(seed=SEED, client=RpcClient(url="https://rpc.invalid"))
    w.representative = DEST
    _, blk = w.send(DEST, int(nano_to_raw("0.001")))
    assert blk["representative"] == DEST


def test_the_attribute_is_kept_exactly_as_given():
    assert Wallet(seed=SEED).representative is None
    assert Wallet(seed=SEED, representative=DEST).representative == DEST


def test_an_invalid_explicit_representative_fails_before_work(node):
    n = node(int(nano_to_raw("0.005")))
    w = Wallet(seed=SEED, client=RpcClient(url="https://rpc.invalid"), representative="nano_bogus")
    with pytest.raises(ValueError):
        w.send(DEST, int(nano_to_raw("0.001")))
    actions = [p.get("action") for p in n.payloads]
    assert "work_generate" not in actions and "process" not in actions


# -- the node-supplied representative is validated before work ----------------

BAD_NODE_REPS = [
    pytest.param(123, id="int"),
    pytest.param(["nano_x"], id="list"),
    pytest.param({"a": 1}, id="object"),
    pytest.param(True, id="bool"),
    pytest.param("nano_bogus", id="malformed"),
    # Valid shape, last checksum character changed.
    pytest.param(CHOSEN_REP[:-1] + ("a" if CHOSEN_REP[-1] != "a" else "b"), id="bad-checksum"),
]


@pytest.mark.parametrize("bad", BAD_NODE_REPS)
def test_a_malformed_node_representative_raises_rpcerror_before_work(node, bad):
    from nano_sdk.client import RpcError

    n = node(int(nano_to_raw("0.005")), rep=bad)
    w = Wallet(seed=SEED, client=RpcClient(url="https://rpc.invalid"))
    with pytest.raises(RpcError):
        w.send(DEST, int(nano_to_raw("0.001")))
    actions = [p.get("action") for p in n.payloads]
    assert actions == ["account_info"], f"requested after a bad reply: {actions}"


@pytest.mark.parametrize("bad", BAD_NODE_REPS)
def test_a_malformed_node_representative_fails_a_session_send_before_work(node, bad):
    import time as _time

    from nano_sdk.buyer import SessionWallet, derive_session_account, issue_mandate
    from nano_sdk.client import RpcError
    from nano_sdk.crypto import derive_account

    master = bytes.fromhex("22" * 32)
    owner = derive_account(master, 0)
    now = _time.time()
    acct = derive_session_account(master, "sess-bad-rep")
    mandate = issue_mandate(
        owner.private_key, "sess-bad-rep", acct.address, int(nano_to_raw("0.003")),
        expires_at=now + 3600, issued_at=now,
    )
    n = node(int(nano_to_raw("0.005")), rep=bad)
    wallet = SessionWallet(
        acct, owner.public_key, mandate,
        client=RpcClient(url="https://rpc.invalid"), clock=lambda: now,
    )
    with pytest.raises(RpcError):
        wallet.send(DEST, int(nano_to_raw("0.001")))
    assert [p.get("action") for p in n.payloads] == ["account_info"]
    assert wallet.available_session() == int(nano_to_raw("0.003"))


@pytest.mark.parametrize("rep", [None, "", "   "])
def test_representative_from_info_treats_missing_or_blank_as_none(rep):
    from nano_sdk.client import representative_from_info

    info = {} if rep is None else {"representative": rep}
    assert representative_from_info(info) is None
    assert representative_from_info({"representative": None}) is None


def test_an_explicit_representative_does_not_depend_on_the_nodes_answer(node):
    """The node's representative is not signed when the caller chose one, so a
    garbage value there does not block the caller's send."""
    node(int(nano_to_raw("0.005")), rep=123)
    w = Wallet(seed=SEED, client=RpcClient(url="https://rpc.invalid"), representative=DEST)
    _, blk = w.send(DEST, int(nano_to_raw("0.001")))
    assert blk["representative"] == DEST


# -- which representative wins: None keeps the account's, any address is explicit --
#
# `Wallet.representative` is a plain dataclass field kept exactly as given, so
# copying, comparing and rebuilding a wallet treat it like any other field.

import dataclasses

DEFAULT_REP = "nano_1stofnrxuz3cai7ze75o174bpm7scwj9jn3nxsn8ntzg784jf1gzn1jjdkou"
THIRD_REP = "nano_1q3hqecaw15cjt7thbtxu3pbzr1eihtzzpzxguoc37bj1wc5ffoh7w74gi6p"


def _send_with(node, wallet) -> str:
    _, blk = wallet.send(DEST, int(nano_to_raw("0.001")))
    return blk["representative"]


def _client():
    return RpcClient(url="https://rpc.invalid")


def test_no_representative_passed_keeps_the_accounts_own(node):
    node(int(nano_to_raw("0.005")))
    w = Wallet(seed=SEED, client=_client())
    assert w.representative is None
    assert _send_with(node, w) == CHOSEN_REP


def test_an_explicit_representative_wins_over_the_nodes(node):
    node(int(nano_to_raw("0.005")))
    w = Wallet(seed=SEED, client=_client(), representative=THIRD_REP)
    assert _send_with(node, w) == THIRD_REP


def test_passing_the_default_explicitly_still_re_delegates(node):
    """The upgrade note tells callers who relied on the old behaviour to do this."""
    node(int(nano_to_raw("0.005")))
    w = Wallet(seed=SEED, client=_client(), representative=DEFAULT_REP)
    assert _send_with(node, w) == DEFAULT_REP


def test_assigning_a_representative_after_construction_wins(node):
    node(int(nano_to_raw("0.005")))
    w = Wallet(seed=SEED, client=_client())
    w.representative = THIRD_REP
    assert _send_with(node, w) == THIRD_REP


def test_assigning_the_default_after_construction_is_explicit(node):
    node(int(nano_to_raw("0.005")))
    w = Wallet(seed=SEED, client=_client())
    w.representative = DEFAULT_REP
    assert _send_with(node, w) == DEFAULT_REP


def test_assigning_none_after_construction_keeps_the_accounts_own(node):
    node(int(nano_to_raw("0.005")))
    w = Wallet(seed=SEED, client=_client(), representative=THIRD_REP)
    w.representative = None
    assert _send_with(node, w) == CHOSEN_REP


def test_a_replaced_copy_keeps_the_accounts_own(node):
    """dataclasses.replace must not turn "no choice" into a re-delegation."""
    node(int(nano_to_raw("0.005")))
    w = dataclasses.replace(Wallet(seed=SEED, client=_client()), daily_cap_raw=int(nano_to_raw("0.002")))
    assert w.representative is None
    assert _send_with(node, w) == CHOSEN_REP


def test_a_replaced_copy_keeps_an_explicit_choice(node):
    node(int(nano_to_raw("0.005")))
    w = dataclasses.replace(Wallet(seed=SEED, client=_client(), representative=THIRD_REP))
    assert _send_with(node, w) == THIRD_REP


def test_asdict_round_trips(node):
    node(int(nano_to_raw("0.005")))
    for rep in (None, THIRD_REP):
        w = Wallet(seed=SEED, client=_client(), representative=rep)
        d = dataclasses.asdict(w)
        assert d["representative"] == rep
        copy = Wallet(**d)
        assert copy.representative == rep
        assert _send_with(node, copy) == (rep or CHOSEN_REP)


def test_equality_treats_representative_as_a_plain_field():
    c = _client()
    assert Wallet(seed=SEED, client=c) == Wallet(seed=SEED, client=c, representative=None)
    assert Wallet(seed=SEED, client=c, representative=DEFAULT_REP) == Wallet(
        seed=SEED, client=c, representative=DEFAULT_REP)
    # "keep the account's" and "re-delegate to the default" are different wallets.
    assert Wallet(seed=SEED, client=c) != Wallet(seed=SEED, client=c, representative=DEFAULT_REP)
