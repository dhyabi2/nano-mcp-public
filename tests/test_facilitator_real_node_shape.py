"""The facilitator against the shape a real Nano node actually returns.

Every other stub in this suite puts the payee somewhere a real node does not:
at the top level as `link_as_account`/`destination`, or inside `contents` as
`destination` with `contents.type: "send"`. A real node returns a STATE block -
`contents.type: "state"`, the payee in `contents.link_as_account`, no
`destination` field anywhere, and the send/receive distinction only in the
TOP-LEVEL `subtype`.

`normalize_block_info` read none of those, so `destination` came back EMPTY for
every real reply, and `verify_block_on_independent_endpoints` then defaulted the
receiver to `pay_to` and compared it with itself. The payee check - the one that
proves the seller's money arrived - was skipped on every real verification, so
any confirmed send of the right amount passed whoever it had paid.

REAL_SEND below is captured from rpc.nano.to for block 64AD5A5C...FEDF, field for
field, with only the signature truncated.
"""
import pytest

from nano_mcp.facilitator import (
    RpcEndpoint,
    normalize_block_info,
    verify_block_on_independent_endpoints,
)

BLOCK = "64AD5A5C2383E465482CFC2E7C5FDFDA4E4D089477F46F88C4336665B929FEDF"
PAYER = "nano_1uw3f4f1sgpm7sd1jptri4xphdchhaa6pw67aagygzferhfxrr69nkrmgiw4"
PAYEE = "nano_1natrium1o3z5519ifou7xii8crpxpk8y65qmkih8e8bpsjri651oza8imdd"
# A seller who was NOT paid by this block.
STRANGER = "nano_1q3hqecaw15cjt7thbtxu3pbzr1eihtzzpzxguoc37bj1wc5ffoh7w74gi6p"
AMOUNT = "50000000000000000000000000000000"


def real_send(**over):
    block = {
        "block_account": PAYER,
        "amount": AMOUNT,
        "balance": "454950477059438533690000000000000",
        "height": "224",
        "local_timestamp": "1790888011",
        "successor": "0" * 64,
        "confirmed": "true",
        "subtype": "send",
        "contents": {
            "type": "state",
            "account": PAYER,
            "previous": "D16B7D2EF1CEF3645DBC6CB2B3B34E4271F957E2267E128169A4565BA6FEDA32",
            "representative": "nano_3patrick68y5btibaujyu7zokw7ctu4onikarddphra6qt688xzrszcg4yuo",
            "balance": "454950477059438533690000000000000",
            "link": "511AC43730543F18C07836BB2F61032B16EDA46F10779CA0F330C9B663881060",
            "link_as_account": PAYEE,
            "signature": "EB09907B1BC0EC3B819C7D87CEDC26FF2F9CC45A26FC7657F210ACA05FBCAD68",
            "work": "65e645ad237b46fc",
        },
    }
    block.update(over)
    return block


def endpoints(block, count=2):
    return [
        RpcEndpoint(url=f"https://node{i}.example", call=lambda a, p, b=block: dict(b))
        for i in range(count)
    ]


def test_a_real_send_blocks_payee_is_read_from_contents_link_as_account():
    """The field a real state block actually carries."""
    nb = normalize_block_info(real_send())

    assert nb.destination == PAYEE, (
        "a real node's send block names its payee in contents.link_as_account; "
        "an empty destination here means the payee check below cannot run"
    )
    assert nb.subtype == "send"
    assert nb.confirmed is True


def test_a_real_send_to_a_stranger_is_refused():
    """The defect, in the form that costs the seller the work.

    A buyer owes the seller `AMOUNT`. It sends exactly that - to its own second
    account - and presents the confirmed block hash. Nothing was paid to the
    seller, and this must refuse.
    """
    res = verify_block_on_independent_endpoints(
        endpoints(real_send()), BLOCK, STRANGER, AMOUNT
    )

    assert res.ok is False
    assert PAYEE in (res.reason or ""), res.reason


def test_a_real_send_to_the_right_payee_still_verifies():
    """The fix must not refuse the payments that were genuinely made."""
    res = verify_block_on_independent_endpoints(
        endpoints(real_send()), BLOCK, PAYEE, AMOUNT
    )

    assert res.ok is True, res.reason
    assert res.confirmed_on == 2
    record = res.confirmed_sends[0]
    assert record["receiver"] == PAYEE
    assert record["payer"] == PAYER
    assert record["amount"] == AMOUNT


def test_the_recorded_receiver_is_the_payee_the_block_names():
    """The receipt must not assert a payee that was never read.

    `receiver` used to be written as `pay_to` unconditionally, so a verification
    that had checked nothing still produced a record naming the seller.
    """
    res = verify_block_on_independent_endpoints(
        endpoints(real_send()), BLOCK, PAYEE, AMOUNT
    )

    assert res.confirmed_sends[0]["receiver"] == normalize_block_info(
        real_send()
    ).destination


def test_a_block_whose_payee_cannot_be_read_is_refused_not_assumed():
    """No payee field at all: refuse, rather than default it to payTo."""
    stripped = real_send()
    contents = dict(stripped["contents"])
    del contents["link_as_account"]
    del contents["link"]
    stripped["contents"] = contents

    assert normalize_block_info(stripped).destination == ""

    res = verify_block_on_independent_endpoints(
        endpoints(stripped), BLOCK, PAYEE, AMOUNT
    )

    assert res.ok is False
    assert "no payee account" in (res.reason or ""), res.reason


def test_a_real_receive_block_cannot_pass_as_a_payment():
    """A confirmed receive of the right amount pays the seller nothing.

    Refused on the subtype. Were a node to omit the top-level `subtype`, the
    payee check is the second line of defence: a receive block's
    `contents.link_as_account` is the SOURCE SEND'S HASH rendered as an account,
    which is not the seller's payTo.
    """
    res = verify_block_on_independent_endpoints(
        endpoints(real_send(subtype="receive")), BLOCK, PAYEE, AMOUNT
    )
    assert res.ok is False

    no_subtype = real_send()
    del no_subtype["subtype"]
    contents = dict(no_subtype["contents"])
    # What a receive block carries there: the hash of the send, as an account.
    contents["link_as_account"] = STRANGER
    no_subtype["contents"] = contents
    res2 = verify_block_on_independent_endpoints(
        endpoints(no_subtype), BLOCK, PAYEE, AMOUNT
    )
    assert res2.ok is False


def test_the_amount_is_still_compared_exactly():
    """One raw short is not payment. Guards against a loosened comparison."""
    short = real_send(amount=str(int(AMOUNT) - 1))

    res = verify_block_on_independent_endpoints(
        endpoints(short), BLOCK, PAYEE, AMOUNT
    )
    assert res.ok is False


@pytest.mark.parametrize("confirmed", ["false", False])
def test_an_unconfirmed_real_block_is_refused(confirmed):
    res = verify_block_on_independent_endpoints(
        endpoints(real_send(confirmed=confirmed)), BLOCK, PAYEE, AMOUNT
    )
    assert res.ok is False
