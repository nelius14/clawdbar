# Contributing to Clawdbar

Thanks for your interest. Clawdbar is one stdlib file that formats a JSON blob — contributions that keep it that small are very welcome.

## Dev setup

Requires **Python 3**, **uv** (`brew install uv`) for the test runner, and Claude Code with a Claude.ai subscription to see it live.

```bash
git clone https://github.com/nealyee/clawdbar.git
cd clawdbar
chmod +x clawdbar_statusline.py
```

There is no build and nothing to install. To see it render without wiring it into Claude Code:

```bash
echo '{"model":{"display_name":"Opus 5"},"rate_limits":{"five_hour":{"used_percentage":62}}}' \
  | ./clawdbar_statusline.py
```

To use it for real, point `statusLine` in `~/.claude/settings.json` at the script — see README.

## Running tests

```bash
uv run --with pytest pytest -q
```

Tests live in `tests/test_statusline.py` and cover rendering plus the hook as Claude Code invokes it. No network, no credentials, no host state.

## Submitting a PR

1. Fork the repo and create a branch from `main`.
2. Make your change, with a test.
3. Run `uv run --with pytest pytest -q` — all tests must pass.
4. Open a pull request describing what the change does and why.

## Out of scope

These are explicitly not planned:

- **Any background process** — daemon, LaunchAgent, or menu bar app. v0.1 was all three; removing them was the point of v0.2.
- **Reading the OAuth token**, from Keychain or `.credentials.json`.
- **API probes** to read rate-limit headers. Claude Code hands us the numbers already; manufacturing a request to observe them spends the quota being measured.
- **Third-party dependencies.** This must run under whatever bare interpreter Claude Code invokes.
- Usage history, trends, or stored state — Claude Code's built-in `/usage` covers breakdowns.

PRs for the above will be closed without comment. See `docs/BRIEF.md` for the reasoning.
