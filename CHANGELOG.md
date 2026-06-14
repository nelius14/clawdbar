# Changelog

All notable changes to Clawdbar. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.1.0] — 2026-06-14

Initial public release as **Clawdbar**.

### Added
- Menu bar app (`menubar/clawdbar_menubar.py`): PyObjC NSStatusItem showing a pixel Clawd icon + live session %; click popover hosts a WKWebView loading the full PWA. Single-instance flock, right-click context menu (Refresh Now, Quit), popover auto-resizes.
- Daemon (`daemon/daemon.py`): loopback HTTP server that reads the Claude OAuth token from macOS Keychain (or `~/.claude/.credentials.json` as fallback), POSTs a minimal Haiku API probe, and parses `anthropic-ratelimit-unified-5h/7d-*` headers into JSON at `/usage`. Auto-refreshes OAuth token after sleep/wake. Keeps last good percentages on transient API errors.
- PWA (`static/index.html`): pixel-art animation engine (13 presets), session (5h) and weekly (7d) usage bars, reset countdowns, "official quota" labels. Comet spark animation (`·✢✶✻※✽✢·` at 2.8s cycle).
- `install.sh`: one-command install — builds uv venv at `~/.clawdbar/.venv`, generates menu bar icon, stages project, loads two LaunchAgents.
- `io.clawdbar.plist` + `io.clawdbar.menubar.plist`: launchd LaunchAgents for daemon and menu bar app.
- `tests/test_daemon.py`: unit tests for token extraction, rate-limit header parsing, and static path-traversal guard (stdlib + pytest; no network or Keychain).
- `LICENSE`: MIT.
- `.github/workflows/ci.yml`: GitHub Actions CI on `macos-latest`.
- `.github/ISSUE_TEMPLATE/`: bug report and feature request templates.
- `CONTRIBUTING.md`.

### Architecture notes
- Loopback-only daemon (`127.0.0.1:7337`). OAuth token never logged or exposed at `/usage`.
- Inspired by [HermannBjorgvin/Clawdmeter](https://github.com/HermannBjorgvin/Clawdmeter) — same header-based approach, clean rewrite.
