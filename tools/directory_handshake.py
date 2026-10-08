#!/usr/bin/env python3
"""Speak MCP to a server command the way a directory does, and report.

A directory -- Glama, which `punkpeye/awesome-mcp-servers` gates its listing on,
or the official registry -- never imports this package. It runs a command, writes
newline-delimited JSON-RPC to that process's stdin, and reads the answer to
`tools/list` off its stdout. This script is that client and nothing else: no
network, no configuration, and `NANO_PAYMENT_MASTER_SECRET` scrubbed from the
environment the server is started in, because a directory has no secret to give.

    python tools/directory_handshake.py -- nano-mcp
    python tools/directory_handshake.py -- python -m nano_mcp.server

Exit codes are kept apart on purpose, because "the gate would fail" and "this
run could not measure it" are different answers and reporting the second as the
first is how a broken probe reads as a broken server:

    0  the exchange completed and every listed tool carries a description
    1  the exchange ran and the server would fail a directory's check -- which
       includes hanging, because a directory gives up the same way
    2  the probe could not look at all (no such command, usage)

`tests/test_directory_handshake.py` imports `handshake` from here, so the suite
and the workflow check one implementation rather than two spellings of it.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

# The protocol version the probe offers. A server that negotiates a different
# one is fine; one that refuses outright is the failure this script reports.
PROTOCOL_VERSION = "2025-06-18"

# The three frames a directory sends: `initialize`, the notification the
# protocol requires before any further request, and the one question the
# listing gate actually asks.
FRAMES = (
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "mcp-directory-probe", "version": "1.0"},
        },
    },
    {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
)

# Scrubbed from the child's environment: a directory starts this server with
# nothing configured, so a secret that happens to sit in the parent shell must
# not be what makes the exchange pass here.
SCRUBBED = ("NANO_PAYMENT_MASTER_SECRET",)

# Put on the stdout queue by the reader thread when the stream ends.
_EOF = object()


@dataclass
class Handshake:
    """What the exchange produced, and why it stopped if it did."""

    returncode: int | None = None
    tools: dict[str, str] = field(default_factory=dict)
    stderr: str = ""
    replies: list[dict] = field(default_factory=list)
    problem: str | None = None
    # False when the probe itself could not look. Kept separate from `problem`
    # so a missing command never gets reported as a server that failed.
    measurable: bool = True

    @property
    def ok(self) -> bool:
        return self.problem is None


def handshake(command, timeout: float = 120.0, env: dict | None = None) -> Handshake:
    """Run `command` as an MCP server over stdio and ask it for its tools.

    The frames are sent one exchange at a time, each reply read before the next
    request goes out. Writing all three at once and closing stdin looks simpler
    and is WRONG: the server may answer `initialize` and then shut down on the
    EOF before its reader ever gets to the buffered `tools/list`, which it did
    here about half the time -- a test that passes in a full suite run and fails
    when run alone. A directory holds the pipe open and waits, so this does too.
    """
    child_env = dict(os.environ if env is None else env)
    for name in SCRUBBED:
        child_env.pop(name, None)

    try:
        proc = subprocess.Popen(
            list(command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=child_env,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError:
        return Handshake(
            problem=f"no such command: {list(command)[0]!r}", measurable=False
        )

    out: queue.Queue = queue.Queue()
    errors: list[str] = []

    def drain_stdout() -> None:
        try:
            for line in proc.stdout:
                out.put(line)
        finally:
            out.put(_EOF)

    def drain_stderr() -> None:
        try:
            for line in proc.stderr:
                errors.append(line)
        except (ValueError, OSError):  # pragma: no cover - closed under us
            pass

    pumps = [threading.Thread(target=pump, daemon=True)
             for pump in (drain_stdout, drain_stderr)]
    for pump in pumps:
        pump.start()

    result = Handshake()
    deadline = time.monotonic() + timeout

    def stop() -> Handshake:
        """Close the server down and attach whatever it said on the way out.

        The pumps are JOINED before `errors` is read, and that is not tidiness.
        `proc.wait()` returning says the child has exited; it says nothing about
        the daemon thread that is still draining the child's stderr pipe into
        `errors`. Reading the list at that moment loses whatever has not been
        appended yet -- measured at **10 runs in 400** against a child that
        writes one line and exits immediately, and more often than that under
        load.

        Every assertion anyone makes about this field is of the form "the bad
        thing is NOT in stderr": no `Traceback`, no leaked secret. An empty
        stderr satisfies all of them. So the race does not make a check flaky in
        the honest direction -- it makes a crashed server and a leaked key read
        as clean, which is the one direction a probe must never fail in.
        """
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=15)
        # The child is gone, so both pipes are at EOF and the pumps return
        # promptly. The timeout is a backstop against a pipe held open by a
        # grandchild the server left behind, not an expected path: a pump that
        # does not finish leaves `stderr` short, so say so rather than imply the
        # server was quiet.
        for pump in pumps:
            pump.join(timeout=10)
        result.returncode = proc.returncode
        result.stderr = "".join(errors)
        if any(pump.is_alive() for pump in pumps):
            result.stderr += (
                "\n[directory_handshake: a reader thread was still draining this "
                "server's output after 10s, so the text above may be incomplete]"
            )
        return result

    def send(frame: dict) -> str | None:
        try:
            proc.stdin.write(json.dumps(frame) + "\n")
            proc.stdin.flush()
            return None
        except (BrokenPipeError, OSError):
            return f"the server closed its input before `{frame.get('method')}`"

    def reply_to(request_id: int, method: str) -> str | None:
        """Read until the answer to `request_id` arrives, or say why it did not."""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return f"the server did not answer `{method}` within {timeout:g}s"
            try:
                line = out.get(timeout=remaining)
            except queue.Empty:
                return f"the server did not answer `{method}` within {timeout:g}s"
            if line is _EOF:
                return f"the server exited before answering `{method}`"
            line = line.strip()
            if not line:
                continue
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                # An MCP host reads this stream as JSON-RPC and nothing else, so
                # a banner or a log line on stdout breaks a directory even though
                # the server is otherwise healthy. Naming it beats skipping it.
                return f"a line on stdout is not JSON-RPC: {line[:200]!r}"
            result.replies.append(frame)
            if frame.get("error"):
                return f"the server answered with a JSON-RPC error: {frame['error']}"
            if frame.get("id") == request_id:
                if "result" not in frame:
                    return f"the answer to `{method}` carries no result: {frame}"
                return None

    initialize, initialized, list_tools = FRAMES
    for step in (
        lambda: send(initialize),
        lambda: reply_to(initialize["id"], "initialize"),
        lambda: send(initialized),
        lambda: send(list_tools),
        lambda: reply_to(list_tools["id"], "tools/list"),
    ):
        problem = step()
        if problem is not None:
            result.problem = problem
            return stop()

    listed = next(r for r in result.replies if r.get("id") == list_tools["id"])
    result.tools = {
        tool["name"]: (tool.get("description") or "")
        for tool in listed["result"].get("tools", [])
    }
    if not result.tools:
        result.problem = "`tools/list` came back empty, so a directory sees nothing"
    return stop()


def main(argv: list[str]) -> int:
    command = argv[1:]
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print(
            "usage: directory_handshake.py -- COMMAND [ARG...]\n"
            "       e.g. directory_handshake.py -- python -m nano_mcp.server",
            file=sys.stderr,
        )
        return 2

    result = handshake(command)
    if result.problem is not None:
        print(f"FAILED: {result.problem}", file=sys.stderr)
        if result.stderr.strip():
            print("--- the server's stderr ---", file=sys.stderr)
            print(result.stderr.strip()[:4000], file=sys.stderr)
        return 2 if not result.measurable else 1

    undescribed = sorted(name for name, text in result.tools.items() if not text.strip())
    print(f"{len(result.tools)} tools: {', '.join(sorted(result.tools))}")
    if undescribed:
        # A directory renders the description. An empty one is what gets a
        # listing rejected on sight.
        print(f"FAILED: no description for {undescribed}", file=sys.stderr)
        return 1

    print("the handshake completed: a directory can start this server and list its tools")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a CLI by the workflow
    raise SystemExit(main(sys.argv))
