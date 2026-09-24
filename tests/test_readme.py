"""Law for the README's install command.

The pinned install line is the one instruction a stranger runs before anything else, and it pointed
at an account that answers 403:

    $ pip install "nano-mcp @ git+https://github.com/PANDeveloper001/nano-mcp-public.git@v0.1.0"
    error: subprocess-exited-with-error
    fatal: could not read Username for 'https://github.com': terminal prompts disabled

A reader has no way to tell that from a network problem or a mistake of their own.

Scope: this law is about the *fetch* URLs only - what a reader clones or pip-installs. It
deliberately says nothing about the MCP registry identity (`io.github.PANDeveloper001/nano-mcp` in
the README marker and in server.json.mcpregistry), which is how the registry proves who owns the
entry and is a publishing decision, not a broken link.
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
