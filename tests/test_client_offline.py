"""Laws for what `RpcClient.call` does with an answer that is not a usable node reply.

No network: `httpx.post` is replaced, so these run in the offline suite. The point of each law is
that `RpcError` is the ONE exception a caller has to handle, which is what its docstring promises.
"""
import httpx
import pytest

from nano_sdk.client import RpcClient, RpcError


@pytest.fixture
def answering(monkeypatch):
    """Make the next call() receive exactly this response."""
    def _answer(response):
        monkeypatch.setattr(httpx, "post", lambda *a, **k: response)
        return RpcClient(url="https://rpc.invalid")
    return _answer


@pytest.mark.parametrize("label,response", [
    # A 200 whose body is not JSON is not the node talking; it is whatever sat in front of it.
    ("an HTML proxy page", httpx.Response(200, text="<html>error code: 1010</html>")),
    ("an empty body", httpx.Response(200, text="")),
    ("a truncated object", httpx.Response(200, text='{"balance": "1')),
    # Every documented action answers with an object, and call() is annotated -> dict.
    ("a JSON list", httpx.Response(200, json=[1, 2])),
    ("a JSON string", httpx.Response(200, json="ok")),
    ("a JSON null", httpx.Response(200, json=None)),
])
def test_an_answer_that_is_not_a_node_reply_raises_rpcerror(answering, label, response):
    with pytest.raises(RpcError):
        answering(response).call(action="version")


def test_the_body_is_quoted_in_the_error_so_the_cause_is_visible(answering):
    """A proxy page that says why is worth more than 'could not parse'."""
    client = answering(httpx.Response(200, text="<html>error code: 1010</html>"))
    with pytest.raises(RpcError, match="1010"):
        client.call(action="version")


def test_what_already_worked_still_works(answering):
    """The guard must not refuse an answer the node really gives."""
    ok = answering(httpx.Response(200, json={"balance": "1000", "pending": "0"}))
    assert ok.call(action="account_balance", account="x") == {"balance": "1000", "pending": "0"}

    empty = answering(httpx.Response(200, json={}))
    assert empty.call(action="version") == {}


@pytest.mark.parametrize("label,response", [
    ("a node error payload", httpx.Response(200, json={"error": "Bad account number"})),
    ("a non-2xx response", httpx.Response(502, text="bad gateway")),
])
def test_the_two_cases_that_already_raised_still_raise(answering, label, response):
    with pytest.raises(RpcError):
        answering(response).call(action="version")
