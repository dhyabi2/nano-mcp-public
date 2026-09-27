"""Law for the declared floor of the `mcp` dependency.

`pyproject.toml` asked for `mcp>=1.0`, and `nano_mcp/server.py` and
`nano_mcp/paidtool.py` both import `mcp.server.mcpserver`, which does not exist
in mcp 1.x. `nano_mcp/__init__.py` imports `paidtool`, so the whole package is
unimportable on a floor the metadata says is enough. Measured in clean
virtualenvs:

    mcp==1.9.0  from mcp.server.mcpserver import MCPServer
                -> ModuleNotFoundError: No module named 'mcp.server.mcpserver'
    mcp==2.0.0  MCPServer OK; 116 passed, 4 skipped, 6 deselected

And the README's first quickstart command, in an environment where the resolver
was free to keep mcp 1.9.0 because `>=1.0` accepted it:

    $ python -m nano_mcp.server
    File "nano_mcp/paidtool.py", line 41, in <module>
        from mcp.server.mcpserver import MCPServer
    ModuleNotFoundError: No module named 'mcp.server.mcpserver'

So the floor is not a style preference: it is the earliest version at which the
imports in this tree resolve. This law reads the modules the source actually
imports and requires the declared floor to cover them, so adding an import from
a newer part of the mcp SDK fails here rather than at a stranger's install.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Measured in clean virtualenvs (see the module docstring): the mcp release at
# which each submodule this tree imports first exists. Not a guess and not a
# restatement of pyproject.toml - the right-hand side is the observation.
MCP_SUBMODULE_INTRODUCED_IN = {
    "mcp.server.mcpserver": (2, 0),
}


def _source_files():
    for pkg in ("nano_mcp", "nano_sdk"):
        for name in sorted(os.listdir(os.path.join(ROOT, pkg))):
            if name.endswith(".py"):
                yield os.path.join(pkg, name)


def _imported_mcp_submodules():
    """Every `mcp.*` submodule imported anywhere in the shipped packages."""
    found = {}
    for rel in _source_files():
        with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                m = re.match(r"\s*(?:from|import)\s+(mcp\.[\w.]+)", line)
                if m:
                    found.setdefault(m.group(1), []).append(f"{rel}:{lineno}")
    return found


def _declared_mcp_floor():
    """The lower bound of the `mcp` requirement in pyproject.toml, as a tuple."""
    with open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8") as fh:
        text = fh.read()
    m = re.search(r'"mcp\s*>=\s*([0-9]+(?:\.[0-9]+)*)"', text)
    assert m, "pyproject.toml no longer declares a lower bound for mcp"
    return tuple(int(p) for p in m.group(1).split("."))


def _at_least(floor, needed):
    """Compare version tuples of different lengths (2.0 >= 2 is true)."""
    width = max(len(floor), len(needed))
    pad = lambda t: tuple(list(t) + [0] * (width - len(t)))  # noqa: E731
    return pad(floor) >= pad(needed)


def test_the_declared_mcp_floor_provides_every_submodule_the_source_imports():
    floor = _declared_mcp_floor()
    for module, sites in sorted(_imported_mcp_submodules().items()):
        needed = MCP_SUBMODULE_INTRODUCED_IN.get(module)
        assert needed is not None, (
            f"{module} is imported at {sites} and this law does not know which mcp "
            "release introduced it; measure it in a clean virtualenv and record it "
            "in MCP_SUBMODULE_INTRODUCED_IN"
        )
        assert _at_least(floor, needed), (
            f"pyproject.toml declares mcp>={'.'.join(map(str, floor))}, but {module} "
            f"(imported at {sites}) first exists in mcp "
            f"{'.'.join(map(str, needed))} - an install that satisfies the declared "
            "floor cannot import this package"
        )


def test_the_package_imports_under_the_installed_mcp():
    """The floor is only honest if the tree really does import here."""
    import nano_mcp  # noqa: F401
    import nano_mcp.paidtool  # noqa: F401
    import nano_mcp.server  # noqa: F401
