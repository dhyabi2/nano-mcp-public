"""Law for the README's install command.

The pinned install line is the one instruction a stranger runs before anything else, and it pointed
at an account that answers 403:

    $ pip install "nano-mcp @ git+https://github.com/PANDeveloper001/nano-mcp-public.git@v0.1.0"
    error: subprocess-exited-with-error
    fatal: could not read Username for 'https://github.com': terminal prompts disabled

A reader has no way to tell that from a network problem or a mistake of their own.

Scope: this law is about the *fetch* URLs only - what a reader clones or pip-installs. It said
nothing about the MCP registry identity in the README marker and in server.json.mcpregistry, on
the grounds that choosing which of the owner's accounts publishes an entry is a publishing
decision and not a broken link. That premise has since failed - the account it named is deleted,
so there is no longer a decision to make - and the identity is now held by
`tests/test_registry_identity.py`, whose docstring quotes this note and says why it changed.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = "dhyabi2/nano-mcp-public"


def _readme():
    with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as f:
        return f.read()


def test_every_url_a_reader_fetches_from_names_this_repository():
    """`git clone <url>` and `pip install git+<url>` must reach a repository that exists."""
    fetched = re.findall(
        r"(?:git\+)?https://github\.com/([\w.-]+/[\w.-]+?)(?:\.git)(?=[@\s\"')]|$)", _readme())
    assert fetched, "the README no longer tells a reader where to install from"
    wrong = sorted({u for u in fetched if u != HERE})
    assert not wrong, f"the README installs from {wrong}, which is not this repository ({HERE})"


def test_the_readme_does_not_promise_release_assets_that_are_not_published():
    """It advertised 'sdist + wheel served as release assets'. This repository has no releases, so a
    reader looking for those assets finds an empty page. The `git+...@v0.1.0` install works - the
    tag is real - so the command stayed and only the unsupported clause went."""
    text = _readme().lower()
    assert "release assets" not in text, (
        "the README promises release assets; publish them or drop the claim")


def test_the_offline_test_count_in_the_quickstart_is_the_count_pytest_collects():
    """The quickstart advertised `# offline tests pass (123)`. The suite collects 120 offline tests,
    of which 119 pass and one skips without a live node.

    A reader follows the quickstart, sees a different number, and has no way to tell a stale
    README from five tests they broke. The number is checked against what pytest actually
    collects under the very selection the README prints, so it cannot drift again:

        $ python -m pytest -q -m "not network" --collect-only
        120/126 tests collected (6 deselected)

    `--collect-only` is used rather than a real run so this law stays cheap and cannot recurse
    into itself.
    """
    import subprocess
    import sys

    m = re.search(r"#\s*(\d+)\s+offline tests collected", _readme())
    assert m, "the quickstart no longer states how many offline tests there are"
    claimed = int(m.group(1))

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-m", "not network", "--collect-only",
         "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True,
    )
    got = re.search(r"(\d+)/\d+ tests collected", proc.stdout)
    assert got, f"could not read a collection count from pytest:\n{proc.stdout[-2000:]}"
    collected = int(got.group(1))
    assert claimed == collected, (
        f"the README says {claimed} offline tests, pytest collects {collected}"
    )


def test_the_readme_sdk_example_runs():
    """The quickstart's one Python example must work.

    It read:

        wallet = Wallet(seed=SEED, client=RpcClient())
        print(wallet.balance())

    `Wallet` has no `balance`. It has `balance_raw(index=0)`. So the first thing
    a reader runs after installing the package ends in

        AttributeError: 'Wallet' object has no attribute 'balance'

    and there is nothing in the README to tell them whether they mistyped it,
    installed the wrong version, or hit a bug.

    This law extracts the example from README.md and executes it, so the block
    cannot drift from the API again. The seed is supplied (the README says to
    take it from the environment) and `RpcClient` is replaced by a stub, so the
    law is offline and touches no funds.
    """
    import re

    block = re.search(r"```python\n(.*?)```", _readme(), re.S)
    assert block, "the README no longer shows how to use the SDK"
    source = block.group(1)

    seen = []

    class StubClient:
        """Stands in for RpcClient: answers account_info, reaches no network."""

        def account_info(self, account):
            seen.append(account)
            return {
                "balance": "1234000000000000000000000000",
                "frontier": "AB" * 32,
                "representative": account,
            }

        def block_info(self, block_hash):
            return {}

        def call(self, **payload):
            raise AssertionError(f"the example must not reach the node: {payload}")

    import nano_sdk

    real = nano_sdk.RpcClient
    nano_sdk.RpcClient = StubClient
    try:
        ns = {"SEED": "00" * 32, "__name__": "readme_example"}
        exec(compile(source, "README.md#python", "exec"), ns)
    finally:
        nano_sdk.RpcClient = real

    # the example really did read a balance through the client, not a stub value
    assert seen, "the example no longer reads a balance"
