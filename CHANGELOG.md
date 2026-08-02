# Changelog

All notable changes to Clawdbar. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.2.0] — 2026-08-02

Stripped to a Claude Code status line. **Breaking:** the menu bar app, the daemon, and both LaunchAgents are gone.

### Added
- `clawdbar_statusline.py`: the whole tool — reads Claude Code's status line JSON on stdin and prints `Opus 5 · 5h 62% · 7d 20%`. Stdlib only, no dependencies, no background process, no credentials, no network calls. Reset countdown appears only once a window passes `RESET_HINT_AT` (80%).
- `tests/test_statusline.py`: 26 tests, including subprocess tests that invoke the hook exactly as Claude Code does, and cases pinning the guarantee that malformed input can never break the status line.

### Removed
- `daemon/daemon.py` — the loopback server, Keychain token read, Haiku API probe, and header parsing.
- `menubar/clawdbar_menubar.py` — the PyObjC status item and WKWebView popover.
- `static/` — the pixel-art PWA, icons, and animation engine.
- `tools/make_icon.py`, `install.sh`, `io.clawdbar.plist`, `io.clawdbar.menubar.plist`, `tests/test_daemon.py`, `docs/TROUBLESHOOTING.md`.

### Why
Claude Code now passes official `rate_limits` (5h/7d `used_percentage` + `resets_at`) to status line commands, as a side effect of requests already being made. The daemon existed only to manufacture a request to read those same numbers from response headers — it spent the quota it measured, and 12 of the project's first 20 commits were repairs to that scraping layer. The seam moved; the machinery was no longer earning its keep. `docs/BRIEF.md` has the full reasoning.

### Notes
- CI moved from `macos-latest` to `ubuntu-latest` — nothing is macOS-specific now.
- The working tree is original code only; the bundled claudepix sprites and Anthropic typeface loading are gone. `LICENSE` still describes the old situation and should be revisited before publishing.

## [0.1.1] — 2026-08-02

### Fixed
- "Quit Clawdbar" relaunched instead of quitting. `io.clawdbar.menubar.plist` set `KeepAlive` to an unconditional `<true/>`, so launchd restarted the app after the clean `exit(0)` from `NSApp.terminate_()`. Gated on `SuccessfulExit` so a deliberate quit sticks while a crash still recovers. (Superseded by 0.2.0, which removes the LaunchAgent entirely.)

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
