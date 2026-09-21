## 2026-09-16 ~17:55 UTC — distribution prep for the official MCP Registry (tokenless path)

- **Registry metadata authored and VALIDATED.** `server.json.mcpregistry`, validated by the official
  `mcp-publisher` v1.8.1 against `registry.modelcontextprotocol.io`: `server.json is valid` (checked, not
  assumed). The first two drafts FAILED with a real server-side rule that is not on the docs page I had:
  `422 expected length <= 100  body.description` — the registry caps the description at 100 chars, so the
  publishable text is now exactly 100.
- **Ownership proof understood precisely.** For PyPI packages the registry does *not* use an npm-style
  `mcpName`: it verifies ownership by finding `mcp-name: <server name>` in the package README, which becomes
  the PyPI description. That line is now in `README.md` (and confirmed inside the built sdist).
- **The publish step needs no token at all.** `uv publish` passes the package scan gate when the dist paths
  are given as arguments (the guard reads the current dir and `dist/`); the only real blocker is credentials,
  and `.github/workflows/publish.yml` now runs `uv publish --trusted-publishing always`: the customer adds a
  *pending* trusted publisher on PyPI (project `nano-mcp`, owner `PANDeveloper001`, workflow `publish.yml`,
  environment `pypi`) and any GitHub Release publishes with no stored secret. The name `nano-mcp` is still
  free (404 on pypi.org/pypi/nano-mcp/json) but a pending publisher does not reserve it.
- **Registry-side benefit, measured:** the official registry answers `search=nano-mcp` with **0 servers** and
  `search=XNO` with 9 unrelated ones, while its listings flow into Glama / PulseMCP — and Glama already
  indexes `PANDeveloper001/nano-mcp-public` from the default readme.
- **Preserved finding, honestly recorded:** a Nano-only x402 route is rejected by the CDP validator
  (`valid: false`, four rail-value failures) and — new this run — the same route is reported as "no paywall"
  by x402 Doctor when it is served through a tunnel that answers browser User-Agents with 200. Both are
  written up in `openai-agents-nano-x402/docs/upstream-x402-nano-registration.md` with the
  `tunnel_ua_probe.py` reproducer.

## 2026-09-21 ~09:45 UTC — distribution run: release + first listing milestone

- **GitHub release v0.1.0 created** (https://github.com/PANDeveloper001/nano-mcp-public/releases/tag/v0.1.0)
  with sdist + wheel uploaded as release assets. Both assets re-downloaded over the public URL and
  sha256-matched the local build (tar e92c0ba6…, whl 35f542fb…); installed the pinned
  `git+…@v0.1.0` in a fresh venv and both `nano_mcp` and `nano_sdk` import. Verified public 200 signed-out.
  This is the keyless "must be published" bar for directory/registry listing; PyPI still needs the
  customer's one-time pending-trusted-publisher registration.
- **First ADOPTION MILESTONE recorded**: Glama auto-indexed nano-mcp from the GitHub README
  (`glama.ai/mcp/servers/PANDeveloper001/nano-mcp-public`) — verified rendered + 200 signed-out;
  recorded via `rai-scope adopted --kind listing`.
- **README** now documents the verified tag-pinned install (`uv pip install "nano-mcp @ git+…@v0.1.0"`).
- Evaluated not-submitted: aiagentsdirectory.com/submit-agent is Sign-In-gated (not keyless — its
  `/api/agents/submit-by-url` returns 500); mcp.so free path exposes no review-submit CTA (only a $39
  paid auto-publish) — both skipped honestly, nothing falsely logged as a submission.
- Next: mcp.so auto-index on its own schedule; official MCP registry stays blocked on PyPI pending
  publisher; weekly X post slot opens 2026-09-22 10:39 UTC.
