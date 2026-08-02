# Clawdbar — Brief

Architecture, decisions, and current state. CLAUDE.md points here; read this before non-trivial work. Session-by-session changes go in `docs/LOG.md`, not here.

## What this is

Clawdbar prints your official Claude.ai usage into the Claude Code status line:

```
Opus 5 · 5h 62% · 7d 20%
```

One stdlib Python file. No processes, no dependencies, no credentials, no network.

## Architecture

```
  Claude Code  ──(JSON on stdin, every status line render)──▶  clawdbar_statusline.py
                                                                        │
    {                                                                   ▼
      "model": { "display_name": "Opus 5" },                    "Opus 5 · 5h 62% · 7d 20%"
      "rate_limits": {                                           (stdout, one line)
        "five_hour": { "used_percentage": 62, "resets_at": … },
        "seven_day": { "used_percentage": 20, "resets_at": … }
      }
    }
```

That is the complete data flow. `render()` is pure; `main()` is a stdin wrapper that swallows errors.

### Fields

`rate_limits` is optional — Claude Code documents it as *"Only present for subscribers after first API response"*, so a fresh session shows just the model until the first exchange. `seven_day` is independently optional. Both are handled by dropping the segment rather than rendering a placeholder.

`used_percentage` is **already 0–100**. The raw `anthropic-ratelimit-unified-*-utilization` headers the v0.1 daemon parsed were 0.0–1.0 and needed ×100. Scaling this one would pin the reading at 100%; there is a test named for it.

There is no `overage` window and no `representative-claim` field here, both of which the raw headers carry. If you need those, they are not available through this seam.

### Reset countdown

Shown only when a window is at or above `RESET_HINT_AT` (default 80%). The design rule: surface the countdown when it is the number you would act on, and stay quiet otherwise. A `resets_at` in the past is treated as absent rather than rendered as a negative duration.

## Why it looks like this (the v0.2.0 rewrite)

v0.1.x was a macOS menu bar app:

- a daemon read the Claude OAuth token from the Keychain (or `~/.claude/.credentials.json`),
- POSTed a minimal Haiku request every 60 seconds purely to read the response headers,
- parsed `anthropic-ratelimit-unified-5h/7d/overage-*`,
- served JSON + a pixel-art PWA on `127.0.0.1:7337`,
- and a PyObjC status item displayed the percentage, with two LaunchAgents keeping it all alive.

It worked. It was also a scraper against surfaces Anthropic never promised, and the maintenance record shows it: **12 of the first 20 commits were repairs, 13 touched the daemon.** It went dark whenever an OAuth refresh failed (a Cloudflare 1010, then a 429, both in June 2026) and stayed dark until Claude Code happened to refresh the Keychain.

The structural problem was not error handling. It was that the daemon had to *manufacture a request in order to observe a header* — it spent the quota it measured. Claude Code sees those headers as a side effect of work you were already doing, and now hands them to status line commands directly.

**The rule this encodes:** to observe a system, ride traffic that already exists. Generating traffic in order to observe is the last resort, and it is the option that decays.

## What not to rebuild

Anything requiring a daemon, a LaunchAgent, the OAuth token, or an API probe. If a feature appears to need one, that is v0.1 repeating — flag it rather than building it.

For usage attribution (what is *driving* consumption: cache misses, >150k context, subagent-heavy sessions, parallel sessions), Claude Code's built-in `/usage` already does this using data only the client holds. Don't reimplement it.

## Risks

- **The seam is documented but not versioned.** If Claude Code renames or drops `rate_limits`, the segment silently disappears and only the model name renders. That is the intended failure mode — quiet degradation, never a broken status line — but it also means breakage is easy to miss. The tests pin the field names.
- **No staleness signal.** The line only updates when Claude Code renders it. Inherent to the seam and acceptable: if no session is open, there is no usage to watch.
