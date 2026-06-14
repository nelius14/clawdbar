# Contributing to Clawdbar

Thanks for your interest. Clawdbar is a small, focused tool — contributions that keep it that way are very welcome.

## Dev setup

Requires **macOS**, **uv** (`brew install uv`), and Claude Code logged in.

```bash
git clone https://github.com/nealyee/clawdbar.git
cd clawdbar
bash install.sh
```

`install.sh` builds the venv at `~/.clawdbar/.venv`, stages the project, and loads both LaunchAgents. Re-run after any edits to restart.

## Running tests

```bash
uv run --with pytest pytest -q
```

Tests live in `tests/test_daemon.py` and cover the daemon's pure logic. No network or Keychain required.

## Submitting a PR

1. Fork the repo and create a branch from `main`.
2. Make your change. If it touches daemon logic, add a test.
3. Run `uv run --with pytest pytest -q` — all tests must pass.
4. Open a pull request describing what the change does and why.

## Out of scope

These are explicitly not planned:

- LAN / phone-facing daemon (loopback only by design)
- BLE or hardware transport
- Notarised `.app` bundle
- Homebrew formula (separate future project)

PRs for the above will be closed without comment.
