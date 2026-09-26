"""Block 12 tests: self-hostable x402 `exact`-on-`nano` facilitator.

The block-10 spec (draft/x402/specs/schemes/exact/scheme_exact_nano.md)
claimed the Python reference "implements the /supported, /verify, /settle
surface", but the product had no HTTP facilitator and a single-RPC verifier.
Block 12 builds it and proves:

  L16 — the facilitator exposes /supported, /verify, /settle per the spec and
        verifies every proof on AT LEAST TWO independent RPC endpoints,
        FAILING CLOSED if any endpoint cannot confirm (strategy law L1).
  L17 — /settle re-verifies (never trusts /verify) and binds the proof to the
        request with an ATOMIC SINGLE-USE claim: one proof yields exactly one
        resource; a repeat /settle returns duplicate, never a second success.

No funds move and no live node is touched: the RPC endpoints are stub
`RpcEndpoint` objects backed by an in-memory block store.
"""
import threading

import pytest

from nano_mcp.facilitator import (
    ClaimStore,
    Facilitator,
    FacilitatorConfig,
    RpcEndpoint,
    consumption_key,
    parse_raw,
    verify_block_on_independent_endpoints,
)

BLOCK = "BA96F62D4EA651A21DA4282809F2541EA42481CA35018129F29B406EF3FE36C0"
PAYER = "nano_3p1zmep1qax1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f9"
PAY_TO = "nano_3t6k35gi95xu6tergt6p69ck76ogmitsa8mnijtpxm9fkcm736xtoncuohr3"
AMOUNT = "1000000000000000000000000000000"  # 1 XNO raw


class BlockStore:
    """In-memory block_info store: hash -> {confirmed, subtype, amount,
    account, link_as_account}. Stands in for independent RPC endpoints."""

    def __init__(self):
        self.blocks: dict[str, dict] = {}
        self.calls = 0

    def add(
        self,
        block_hash: str,
        *,
        confirmed: bool = True,
        subtype: str = "send",
        amount: str = AMOUNT,
        account: str = PAYER,
        link_as_account: str = PAY_TO,
    ):
        self.blocks[block_hash] = {
            "hash": block_hash,
            "confirmed": confirmed,
            "subtype": subtype,
            "amount": amount,
            "account": account,
            "link_as_account": link_as_account,
        }

    def add_endpoint(self, url: str, *, broken: bool = False) -> RpcEndpoint:
        def call(action: str, params: dict) -> dict:
            self.calls += 1
            if broken:
                raise RuntimeError(f"{url} down")
            if action == "block_info":
                h = params.get("hash")
                if h not in self.blocks:
                    return {}
                return dict(self.blocks[h])
            return {}

        return RpcEndpoint(url=url, call=call)


def make_verify_endpoints(*, broken: bool = False) -> list[RpcEndpoint]:
    store = BlockStore()
    store.add(BLOCK)
    eps = [store.add_endpoint("https://rpc.nano.to"), store.add_endpoint("https://proxy.nano.rpc.blvd.run")]
    if broken:
        eps[1] = store.add_endpoint("https://broken.nano.rpc", broken=True)
    return eps


def make_fac(tmp_path, eps=None) -> Facilitator:
    eps = eps or make_verify_endpoints()
    store = ClaimStore(path=str(tmp_path / "claims.db"))
    return Facilitator(FacilitatorConfig(endpoints=eps, claim_store=store))


def req() -> dict:
    return {
        "scheme": "exact",
        "network": "nano:live",
        "amount": AMOUNT,
        "asset": "XNO",
        "payTo": PAY_TO,
        "maxTimeoutSeconds": 60,
        "extra": {"requestId": "req_fac_1"},
    }


def payload(proof: str = BLOCK) -> dict:
    return {
        "x402Version": 2,
        "accepted": {
            "scheme": "exact",
            "network": "nano:live",
            "amount": AMOUNT,
            "asset": "XNO",
            "payTo": PAY_TO,
            "extra": {"requestId": "req_fac_1"},
        },
        "payload": {"paymentProof": proof},
    }


# ------------------------------------------------------------------ L16 surface


def test_l16_facilitator_exposes_supported_verify_settle(tmp_path):
    fac = make_fac(tmp_path)
    assert fac.supported()["scheme"] == "exact"
    assert fac.supported()["network"] == "nano:live"
    assert fac.supported()["asset"] == "XNO"
    # the three spec surface methods exist and are wired
    assert callable(fac.verify) and callable(fac.settle)


def test_l16_verify_confirms_on_two_independent_endpoints(tmp_path):
    fac = make_fac(tmp_path)
    res = fac.verify(req(), payload())
    assert res["isValid"] is True
    assert res["payer"] == PAYER
    assert res["extra"]["confirmedOn"] == 2
    assert res["extra"]["consulted"] == 2


def test_l16_fail_closed_when_any_endpoint_errors(tmp_path):
    # one endpoint broken -> MUST refuse even though the other confirms
    fac = make_fac(tmp_path, eps=make_verify_endpoints(broken=True))
    res = fac.verify(req(), payload())
    assert res["isValid"] is False
    assert res["invalidReason"] == "unconfirmed"
    # fail-closed: the healthy endpoint saw the block, but the broken one did
    # NOT confirm, so verification MUST NOT pass (0 out of 2 full confirms).
    assert res["extra"]["confirmedOn"] < res["extra"]["consulted"]
    assert res["extra"]["consulted"] == 2


def test_l16_refuses_less_than_two_endpoints(tmp_path):
    store = BlockStore()
    store.add(BLOCK)
    ep = store.add_endpoint("https://only.one")
    fac = Facilitator(FacilitatorConfig(endpoints=[ep], claim_store=ClaimStore(path=str(tmp_path / "c.db"))))
    res = fac.verify(req(), payload())
    assert res["isValid"] is False
    assert "fail-closed" in res["invalidMessage"]


def test_l16_refuses_unconfirmed_or_wrong_amount_or_wrong_payto(tmp_path):
    # unconfirmed
    store = BlockStore()
    store.add("C0FFEE" * 8, confirmed=False)
    evil = [store.add_endpoint("https://a"), store.add_endpoint("https://b")]
    fac = Facilitator(FacilitatorConfig(endpoints=evil, claim_store=ClaimStore(path=str(tmp_path / "c.db"))))
    assert fac.verify(req(), payload("C0FFEE" * 8))["isValid"] is False

    # wrong amount
    store2 = BlockStore()
    store2.add(BLOCK, amount="1")
    fac2 = Facilitator(FacilitatorConfig(endpoints=[store2.add_endpoint("https://a"), store2.add_endpoint("https://b")], claim_store=ClaimStore(path=str(tmp_path / "d.db"))))
    assert fac2.verify(req(), payload())["isValid"] is False

    # wrong payTo
    store3 = BlockStore()
    store3.add(BLOCK, link_as_account="nano_1q3hqecaw15cjt7thbtxu3pbzr1eihtzzpzxguoc37bj1wc5ffoh7w74gi6p")
    fac3 = Facilitator(FacilitatorConfig(endpoints=[store3.add_endpoint("https://a"), store3.add_endpoint("https://b")], claim_store=ClaimStore(path=str(tmp_path / "e.db"))))
    assert fac3.verify(req(), payload())["isValid"] is False


def test_l16_verify_refuses_bad_malformed_proof(tmp_path):
    fac = make_fac(tmp_path)
    res = fac.verify(req(), payload("not-hex"))
    assert res["isValid"] is False
    assert res["invalidReason"] == "proof missing"


def test_l16_verify_checks_requirements_consistency(tmp_path):
    fac = make_fac(tmp_path)
    bad = req()
    bad["scheme"] = "nope"
    assert fac.verify(bad, payload())["invalidReason"] == "bad-scheme"
    bad2 = req()
    bad2["network"] = "evm:mainnet"
    assert fac.verify(bad2, payload())["invalidReason"] == "bad-network"
    bad3 = req()
    bad3["asset"] = "USDC"
    assert fac.verify(bad3, payload())["invalidReason"] == "bad-asset"
    bad4 = req()
    bad4["amount"] = "2"
    assert fac.verify(bad4, payload())["invalidReason"] == "bad-amount"


# ---------------------------------------------------------------- L17 settle


def test_l17_settle_re_verifies_then_claims_atomically(tmp_path):
    fac = make_fac(tmp_path)
    s1 = fac.settle(req(), payload())
    assert s1["success"] is True
    assert s1["transaction"] == BLOCK
    assert s1["network"] == "nano:live"
    assert s1["payer"] == PAYER
    assert s1["amount"] == AMOUNT

    # a repeat /settle for the same proof+request must NOT deliver twice
    s2 = fac.settle(req(), payload())
    assert s2["success"] is False
    assert s2["errorReason"] == "duplicate"
    # the proof is now recorded on-chain so both were verifiable; exactly one won
    assert fac.claim_store.is_claimed(consumption_key(BLOCK, "req_fac_1")) is True


def test_l17_settle_never_trusts_prior_verify(tmp_path):
    # settle must re-verify on-chain: build an endpoint that is fine for the
    # first call but returns nothing on settle -> settle refuses even though a
    # hypothetical prior /verify succeeded.
    store = BlockStore()
    store.add(BLOCK)

    class Flaky:
        def __init__(self):
            self.times = 0

        def call(self, action, params):
            self.times += 1
            if action == "block_info":
                if self.times > 2:  # first two verifies ok, settle re-verify fails
                    return {}
                return dict(store.blocks[params["hash"]])
            return {}

    flaky = Flaky()
    eps = [RpcEndpoint(url="https://a", call=flaky.call), RpcEndpoint(url="https://b", call=flaky.call)]
    fac = Facilitator(FacilitatorConfig(endpoints=eps, claim_store=ClaimStore(path=str(tmp_path / "c.db"))))
    assert fac.verify(req(), payload())["isValid"] is True
    s = fac.settle(req(), payload())
    assert s["success"] is False
    assert s["errorReason"] == "unconfirmed"


def test_l17_claim_store_persists_exactly_once_across_instances(tmp_path):
    key = consumption_key(BLOCK, "req_fac_1")
    db = str(tmp_path / "p.db")
    c1 = ClaimStore(path=db)
    assert c1.claim(key) is True
    assert c1.claim(key) is False  # second claim loses
    c2 = ClaimStore(path=db)  # new handle, same sqlite file
    assert c2.is_claimed(key) is True
    assert c2.claim(key) is False  # persists across instances


def test_l17_concurrent_settles_approve_exactly_once(tmp_path):
    fac = make_fac(tmp_path)
    results = []

    def settle_many():
        for _ in range(5):
            results.append(fac.settle(req(), payload())["success"])

    threads = [threading.Thread(target=settle_many) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(True) == 1
    assert results.count(False) == 19


# ------------------------------------------------------------------ HTTP layer


@pytest.fixture()
def live_fac(tmp_path):
    """Serve a real facilitator on an ephemeral port; teardown after each test."""
    import httpx

    from nano_mcp.facilitator import serve

    fac = make_fac(tmp_path)
    server = serve(fac, host="127.0.0.1", port=0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    yield fac, httpx.Client(base_url=base)
    server.shutdown()
    thread.join(timeout=5)
    server.server_close()


def test_l16_http_get_supported(live_fac):
    _, client = live_fac
    r = client.get("/supported")
    assert r.status_code == 200
    body = r.json()
    assert body["scheme"] == "exact"
    assert body["network"] == "nano:live"
    assert body["asset"] == "XNO"


def test_l16_http_post_verify_confirms_on_two_endpoints(live_fac):
    _, client = live_fac
    r = client.post("/verify", json={"requirements": req(), "payload": payload()})
    assert r.status_code == 200
    assert r.json()["isValid"] is True
    assert r.json()["payer"] == PAYER


def test_l17_http_post_settle_exactly_once(live_fac):
    _, client = live_fac
    body = {"requirements": req(), "payload": payload()}
    s1 = client.post("/settle", json=body).json()
    assert s1["success"] is True
    assert s1["transaction"] == BLOCK
    s2 = client.post("/settle", json=body).json()
    assert s2["success"] is False
    assert s2["errorReason"] == "duplicate"


def test_l16_http_unknown_route_404(live_fac):
    _, client = live_fac
    assert client.get("/nope").status_code == 404
    assert client.post("/nope", json={}).status_code == 404


# ------------------------------------------------------------------ parse_raw


def test_parse_raw_handles_raw_and_decimal():
    assert parse_raw("1000000000000000000000000000000") == 10**30
    assert parse_raw("1") == 1
    assert parse_raw("1.5") == 15 * 10**29
    assert parse_raw("") == 0

def test_post_body_that_is_valid_json_but_not_an_object_is_400(live_fac):
    """A /verify or /settle body that is well-formed JSON but not an object must
    be answered 400, not drop the connection.

    do_POST reads `body.get("requirements")`, so a bare number, string or list
    used to raise AttributeError outside the handler's try/except and the client
    got no response at all.
    """
    _, client = live_fac
    for path in ("/verify", "/settle"):
        for raw in (b"123", b'"x"', b"[1,2]", b"null"):
            r = client.post(
                path, content=raw, headers={"Content-Type": "application/json"}
            )
            assert r.status_code == 400, f"{path} {raw!r} -> {r.status_code}"
