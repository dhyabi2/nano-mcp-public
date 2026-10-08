"""The registry identity must name an account that exists.

`tests/test_readme.py` fixed the URLs a reader *fetches* from and said, in as many
words, that it deliberately left the MCP registry identity alone:

    Scope: this law is about the *fetch* URLs only [...] It deliberately says
    nothing about the MCP registry identity (`io.github.PANDeveloper001/nano-mcp`
    in the README marker and in server.json.mcpregistry), which is how the
    registry proves who owns the entry and is a publishing decision, not a
    broken link.

That reading was right while both accounts existed: picking which one publishes
is nobody's call but the owner's. It is not right any more. `PANDeveloper001` is
a **deleted account** (recorded in the swarm's TIER0.md, which also puts the
whole namespace out of bounds for any routine), so there is no decision left to
make -- one of the two options is gone. What was a publishing decision is now
exactly the broken link the file above refused to call it, and these laws hold it
fixed.

Three separate things have to agree, and the registry checks all three:

  * `server.json.mcpregistry`'s `name` is the namespace the registry
    authenticates, by the GitHub login inside it. A deleted login can never be
    authenticated, so the entry can never be published under it.
  * the `<!-- mcp-name: ... -->` marker in the README is the ownership proof the
    registry looks for in the package description. It must be byte-identical to
    that name or the proof fails.
  * `repository.url` is where a reader and a directory go to read the source.

Three things are left naming the deleted account on purpose, and none is in the
scanned set below:

  * `.github/workflows/publish.yml` -- its setup comment tells a person which
    PyPI *pending trusted publisher* to register, and `Owner: PANDeveloper001`
    can never match the OIDC claim a workflow in `dhyabi2/nano-mcp-public`
    presents, so the upload cannot succeed as written. Correcting it is part of
    registering the publisher, which needs the PyPI account and is the owner's.
  * `LICENSE` -- a copyright attribution, not a link. Who holds the copyright is
    not a routine's call.
  * `audits/` and `.ledger/` -- dated records of what was true when they were
    written, this defect included.
"""
import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The account this repository actually lives under, and the only one a GitHub
# OIDC claim or a registry namespace check can come back true for.
OWNER = "dhyabi2"

# Namespaces that must never reappear in a shipped file. `PANDeveloper001` is
# deleted; anything naming it is unreachable rather than merely wrong.
DEAD_NAMESPACES = ("PANDeveloper001",)

# Files that are shipped or read by a registry. `audits/` and `.ledger/` are
# deliberately excluded: they are dated records of what was true when they were
# written, and rewriting history to hide a defect is worse than the defect.
SHIPPED = (
    "README.md",
    "server.json.mcpregistry",
    "pyproject.toml",
    "nano_mcp",
    "nano_sdk",
    "tools",
)


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def _manifest():
    return json.loads(_read("server.json.mcpregistry"))


def test_the_registry_namespace_is_an_account_that_exists():
    """The name the registry authenticates must carry a live GitHub login."""
    name = _manifest()["name"]
    assert name == f"io.github.{OWNER}/nano-mcp", (
        f"server.json.mcpregistry claims the namespace {name!r}; the registry "
        f"proves that by GitHub login, so it has to be {OWNER!r}"
    )


def test_the_readme_ownership_marker_matches_the_manifest_name():
    """Byte-identical, or the registry's ownership proof does not match."""
    marker = re.search(r"<!--\s*mcp-name:\s*(\S+)\s*-->", _read("README.md"))
    assert marker, "the README no longer carries the mcp-name ownership marker"
    assert marker.group(1) == _manifest()["name"], (
        f"the README marker says {marker.group(1)!r} and the manifest says "
        f"{_manifest()['name']!r}; the registry compares them literally"
    )


def test_the_manifest_repository_url_is_this_repository():
    """A directory follows this URL to read the source; it must resolve."""
    url = _manifest()["repository"]["url"]
    assert url == f"https://github.com/{OWNER}/nano-mcp-public", (
        f"the manifest sends a reader to {url}, which is not this repository"
    )


def test_the_manifest_declares_a_transport_this_distribution_can_serve():
    """Declaring `stdio` is a promise that an installed command exists.

    `tests/test_directory_handshake.py` proves the command works. This checks the
    two halves still describe each other: a manifest that declared, say, an http
    transport would need a server this package does not ship.
    """
    packages = _manifest()["packages"]
    assert packages, "the manifest lists no package, so there is nothing to install"
    for package in packages:
        assert package["transport"]["type"] == "stdio", (
            "this distribution serves stdio only (nano_mcp.server:main runs "
            f"server.run(transport='stdio')), but the manifest declares "
            f"{package['transport']['type']!r}"
        )


def test_no_shipped_file_names_the_deleted_account():
    """Nothing a reader or a registry parses may point at the dead namespace.

    Scoped to shipped files on purpose: `audits/` and `.ledger/` record what was
    true on the day they were written, including this defect, and are left alone.
    """
    offenders = []
    for entry in SHIPPED:
        path = os.path.join(ROOT, entry)
        files = [path]
        if os.path.isdir(path):
            files = [
                os.path.join(parent, name)
                for parent, _, names in os.walk(path)
                for name in names
                if name.endswith((".py", ".md", ".toml", ".json", ".mcpregistry"))
            ]
        for filename in files:
            try:
                with open(filename, encoding="utf-8") as f:
                    text = f.read()
            except (OSError, UnicodeDecodeError):
                continue
            for dead in DEAD_NAMESPACES:
                if dead in text:
                    offenders.append(f"{os.path.relpath(filename, ROOT)} names {dead}")
    assert not offenders, "shipped files point at a deleted account: " + "; ".join(offenders)
