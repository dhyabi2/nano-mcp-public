"""A directory must be able to START this server and introspect it, over stdio.

This is the public release -- the distribution `server.json.mcpregistry` points a
registry at -- and until this file was written nothing here measured the one
thing a registry does with that entry. Two breaks were found by writing it, and
either alone was enough to make the server unusable to every directory:

  * `server_from_env()` raised `RuntimeError: NANO_PAYMENT_MASTER_SECRET must be
    set` before building anything, so discovery never reached a tool list; and
  * `pyproject.toml` declared no console script at all, so the `stdio` transport
    the manifest promises had no command to run.

`tests/test_server_unconfigured.py` asks `server_from_env().list_tools()` in
process, which covers the first break and not the second. A directory -- Glama,
which `punkpeye/awesome-mcp-servers` gates its listing on, or the official
registry -- does not import this package. It runs the command the distribution
installs, writes JSON-RPC to that process's stdin and reads `tools/list` off its
stdout.

Everything between `main()` and the wire is therefore untested by an in-process
call: the console script `pyproject.toml` declares, `main()` itself,
`server.run(transport="stdio")`, the framing of the replies, and whether
anything else gets printed on the stream a host parses as JSON-RPC. A break
anywhere in that span looks like a healthy server to the suite and like a dead
one to every directory.

So these laws run the real exchange, through `tools/directory_handshake.py`,
which is also runnable by hand against any server command. The server is started
as a subprocess with `NANO_PAYMENT_MASTER_SECRET` scrubbed from its environment.
Nothing is mocked and nothing touches the network.
"""
from __future__ import annotations

import asyncio
import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

from nano_mcp.server import server_from_env

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "tools" / "directory_handshake.py"


def _load_probe():
    """Load the probe by path: `tools/` is not part of the distribution.

    The module has to be registered in `sys.modules` BEFORE it is executed.
    `@dataclass` looks its own class's module up there to resolve annotations, so
    an unregistered module makes the decorator raise `AttributeError: 'NoneType'
    object has no attribute '__dict__'` at import time -- a collection error
    whose text says nothing about the real cause.
    """
    name = "directory_handshake"
    spec = importlib.util.spec_from_file_location(name, PROBE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


probe = _load_probe()

# The command an MCP host puts in its config, and the one the `stdio` transport
# in server.json.mcpregistry promises exists. Declared once here and asserted
# against pyproject.toml below.
CONSOLE_SCRIPT = "nano-mcp"


def test_the_module_entrypoint_completes_a_directorys_handshake():
    """`python -m nano_mcp.server`: the path that needs no console script."""
    result = probe.handshake([sys.executable, "-m", "nano_mcp.server"], timeout=120)
    assert result.ok, f"{result.problem}\n--- stderr ---\n{result.stderr}"
    assert result.returncode == 0, (
        "the server left a non-zero exit status behind after a clean exchange: "
        f"{result.returncode}\n--- stderr ---\n{result.stderr}"
    )
    assert "Traceback" not in result.stderr, (
        "the exchange completed but the server crashed on the way out:\n" + result.stderr
    )


def test_the_installed_console_script_completes_a_directorys_handshake():
    """The exact command the Dockerfile starts, as installed by pip."""
    found = shutil.which(CONSOLE_SCRIPT)
    if found is None:
        pytest.skip(
            f"{CONSOLE_SCRIPT!r} is not on PATH: this environment has the package "
            "importable but not installed, so the console script cannot be run here"
        )
    result = probe.handshake([found], timeout=120)
    assert result.ok, f"{result.problem}\n--- stderr ---\n{result.stderr}"
    assert result.returncode == 0, result.stderr


def test_the_wire_shows_exactly_what_the_code_exposes():
    """No hard-coded tool list: the subprocess answer must equal the server's own.

    A list written down here would have to be edited every time a tool is added,
    and the edit is what gets forgotten. Comparing the two sides instead pins the
    thing that actually matters -- that a tool the code exposes reaches the wire.
    """
    in_process = asyncio.run(server_from_env().list_tools())
    expected = {tool.name: (tool.description or "") for tool in in_process}

    result = probe.handshake([sys.executable, "-m", "nano_mcp.server"], timeout=120)
    assert result.ok, result.problem
    assert set(result.tools) == set(expected), (
        "the tools a directory sees are not the tools the server exposes; "
        f"only in process: {sorted(set(expected) - set(result.tools))}; "
        f"only on the wire: {sorted(set(result.tools) - set(expected))}"
    )
    for name, description in result.tools.items():
        assert description.strip(), f"{name} reaches a directory with no description"


def test_the_handshake_needs_no_secret():
    """The whole point of the gate: nothing is configured and it still answers."""
    result = probe.handshake(
        [sys.executable, "-m", "nano_mcp.server"],
        timeout=120,
        env={"PATH": "/usr/bin:/bin", "HOME": "/tmp"},
    )
    assert result.ok, f"an unconfigured server failed a directory: {result.problem}"
    assert result.tools, "unconfigured, the server lists nothing"


def test_a_secret_in_the_parent_environment_is_not_passed_to_the_server(tmp_path):
    """The scrub is real, so a developer's own shell cannot make this pass.

    Without it the suite would be green on a machine that exports the variable
    and red on the runner, and the gate being measured -- "starts with no
    configuration" -- would never actually be under test.
    """
    sentinel = "ab" * 32
    stub = tmp_path / "echo_env.py"
    stub.write_text(
        "import os, sys\n"
        "sys.stderr.write(repr(os.environ.get('NANO_PAYMENT_MASTER_SECRET')))\n"
    )
    result = probe.handshake(
        [sys.executable, str(stub)],
        timeout=60,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "NANO_PAYMENT_MASTER_SECRET": sentinel,
        },
    )
    # The stub is not a server, so the exchange cannot complete -- what is under
    # test is what reached its environment.
    assert not result.ok
    assert sentinel not in result.stderr, "the parent's secret reached the server"
    assert result.stderr.strip() == "None", (
        "the server should have seen no secret at all, it saw: " + result.stderr
    )


def test_the_distribution_declares_the_command_the_manifest_promises():
    """`pyproject.toml` must install the command, or the manifest is a dead end.

    `server.json.mcpregistry` declares `transport: {"type": "stdio"}` against a
    pypi package. A host reads that as "install this and run its command", and
    this distribution declared no command at all -- so the entry named a
    transport with nothing behind it. The laws above prove the command works when
    it is installed; this one proves it is still declared, which is the half that
    a packaging edit can silently undo.
    """
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert "[project.scripts]" in pyproject, (
        "pyproject.toml declares no console scripts, so the `stdio` transport in "
        "server.json.mcpregistry has no command to run"
    )
    assert f'{CONSOLE_SCRIPT} = "nano_mcp.server:main"' in pyproject, (
        f"pyproject.toml no longer declares the {CONSOLE_SCRIPT!r} console script, "
        "so an MCP host following the registry entry has nothing to start"
    )
