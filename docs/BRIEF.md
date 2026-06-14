# Clawdmeter — Brief

Full architecture, decisions, and current state. CLAUDE.md points here; read this before non-trivial work. Session-by-session changes go in `docs/LOG.md`, not here.

## What this is

Clawdmeter surfaces Claude usage in the macOS menu bar by polling **Anthropic's official rate-limit headers** — the same approach as [HermannBjorgvin/Clawdmeter](https://github.com/HermannBjorgvin/Clawdmeter). Two local processes run on one Mac:

- a Python stdlib HTTP **daemon** that reads the Claude OAuth token, POSTs a minimal API probe, parses the unified 5h/7d utilization headers, and exposes them as JSON + serves the PWA;
- a PyObjC **menu bar app** that shows the current session % beside a pixel Clawd icon and, on click, opens a popover hosting a WKWebView pointed at the daemon's PWA.

Single user, single machine. The daemon binds loopback only; the only outbound network call is the minimal Anthropic API probe.

## Architecture

```
   macOS Keychain ("Claude Code-credentials")
        or ~/.claude/.credentials.json
                          │
                          ▼
   daemon.py ── POST /v1/messages (1 Haiku token)
     │            parse response headers:
     │              anthropic-ratelimit-unified-5h-utilization
     │              anthropic-ratelimit-unified-5h-reset
     │              anthropic-ratelimit-unified-7d-utilization
     │              anthropic-ratelimit-unified-7d-reset
     │              anthropic-ratelimit-unified-5h-status
     │
     ├─ GET /usage  → JSON {session_pct, weekly_pct, *_reset_min, status, ok, error}
     └─ GET /       → static/index.html (the PWA)          [127.0.0.1:7337]
                          ▲                    ▲
        poll /usage 60s   │                    │ load PWA in WKWebView
                  menubar/clawdbar_menubar.py (NSStatusItem + NSPopover)
```

## How usage is derived

Every `CLAWDBAR_POLL` seconds (default 60):

1. **Read token** from macOS Keychain (`Claude Code-credentials`) or, as fallback, `~/.claude/.credentials.json`. Extract `accessToken` from the JSON blob.
2. **POST** to `https://api.anthropic.com/v1/messages` with a minimal body (Haiku, `max_tokens: 1`, message `"hi"`) — identical probe to upstream Clawdmeter.
3. **Parse headers** into percentages and reset countdowns:
   - `session_pct` ← `anthropic-ratelimit-unified-5h-utilization` × 100
   - `weekly_pct` ← `anthropic-ratelimit-unified-7d-utilization` × 100
   - `session_reset_min` ← minutes until `anthropic-ratelimit-unified-5h-reset`
   - `weekly_reset_min` ← minutes until `anthropic-ratelimit-unified-7d-reset`
   - `status` ← `anthropic-ratelimit-unified-5h-status`

The bar percentages are **Anthropic's real subscription quota utilization**, not a self-set budget.

Token counts and cost estimates are **not available** from these headers; the PWA shows "official quota" under each bar instead.

## Repository structure

```
clawdbar/
├── CLAUDE.md                            # project brief (always loaded)
├── CLAUDE.local.md                      # machine-specific, gitignored
├── CHANGELOG.md
├── README.md                            # human-facing overview
├── install.sh                           # build uv venv + icon, stage to ~/.clawdbar, load LaunchAgents
├── io.clawdbar.plist                    # daemon LaunchAgent
├── io.clawdbar.menubar.plist            # menu bar LaunchAgent
├── daemon/daemon.py
├── menubar/clawdbar_menubar.py
├── static/                              # prebuilt PWA + generated icons
├── tools/make_icon.py
├── tests/test_daemon.py
├── .claude/settings.json                # read-deny rules + pytest Stop hook
├── docs/BRIEF.md                        # this file
└── docs/LOG.md                          # append-only session log
```

## Key decisions

- **API headers, not local transcripts.** Anthropic's unified rate-limit headers are the authoritative 5h/7d quota. Local JSONL parsing was replaced because it only saw locally-logged turns and used self-set budgets for the bar %.
- **Same probe as upstream.** Minimal Haiku POST, OAuth token from Keychain, same header names — aligned with HermannBjorgvin/Clawdmeter.
- **Loopback-only daemon.** Binds `127.0.0.1`. The menu bar WebView uses the IP (not `localhost`) so plain HTTP doesn't need an App Transport Security exception.
- **Reuse the PWA.** The menu bar app is a thin shell; all UI lives in `static/index.html`. The pixel-animation engine is untouched; the data-binding layer reads `/usage`.
- **uv, not pip.** Per global standing instructions. Venv at `~/.clawdbar/.venv`.
- **Single machine.** Develop in your checkout; `install.sh` stages a runtime copy to `~/.clawdbar`.

## Known risks / open questions

- **Token expiry.** 401/403 → "token expired — run `claude login`". Transient network errors keep the last good percentages but mark `ok: false`.
- **API probe cost.** Each poll burns ~1 Haiku output token. Default 60s interval ≈ 1440 probes/day — negligible but non-zero.
- **Header schema drift.** If Anthropic renames the unified rate-limit headers, usage drops to zero silently. Compare against `curl -i` on a manual probe if numbers look wrong.
- **Keychain access under launchd.** The daemon LaunchAgent must be able to read `Claude Code-credentials` from Keychain. If poll fails with "no token" despite being logged in, check Keychain access for the Python binary.
- **PyObjC under a uv venv + launchd.** Menu bar launch was never run end-to-end in chat; first real run on the Mac confirms it.
