# Clawdbar

Claude usage in your Claude Code status line, from Anthropic's official numbers.

```
Opus 5 · 5h 62% ↻2h14m · 7d 20%
```

`↻` is how long until the 5-hour session window resets. Only the session window
gets a countdown — it resets several times a day, so it's a number you act on.
The weekly reset is days away and never changes what you do next.

## What it does

Claude Code pipes a JSON blob to its status line command on every render, and that blob already contains your official Claude.ai subscription usage under `rate_limits` — the same 5h/7d percentages Anthropic returns in its unified rate-limit headers.

Clawdbar formats them. That's the whole tool: one stdlib Python file, no dependencies, no background process, no API calls, no credentials.

## Requirements

- **Claude Code** with a Claude.ai subscription
- **Python 3** (the system one is fine)

## Install

Clone anywhere, then point Claude Code at the script in `~/.claude/settings.json`:

```json
{
  "statusLine": {
    "type": "command",
    "command": "/absolute/path/to/clawdbar/clawdbar_statusline.py"
  }
}
```

Make sure it's executable (`chmod +x clawdbar_statusline.py`). New sessions pick it up immediately.

`rate_limits` only arrives after a session's first API response, so a brand-new session shows just the model name until you send one message.

## Tuning

Everything worth changing is a constant at the top of `clawdbar_statusline.py`:

| Constant | Default | Meaning |
|---|---|---|
| `RESET_GLYPH` | `"↻"` | Marker before the reset countdown |
| `SEPARATOR` | `" · "` | Text between segments |
| `WINDOWS` | 5h (with countdown), 7d (without) | Which windows to show, their labels, and whether each gets a reset countdown |

To add context-window usage, which Claude Code passes in the same blob:

```python
remaining = (payload.get("context_window") or {}).get("remaining_percentage")
if remaining is not None:
    parts.append(f"ctx {round(remaining)}%")
```

## Commands

```bash
uv run --with pytest pytest -q                    # tests
echo '{"model":{"display_name":"Opus 5"},"rate_limits":{"five_hour":{"used_percentage":62}}}' \
  | ./clawdbar_statusline.py                      # render a sample line
```

## Uninstall

Remove the `statusLine` block from `~/.claude/settings.json`. There is nothing else — no agents, no daemon, no staged copy.

## History

Clawdbar started in June 2026 as a macOS menu bar app: a daemon read the Claude OAuth token from the Keychain, sent a minimal Haiku probe every 60 seconds, parsed the unified rate-limit headers, and served them to a PyObjC menu bar app over loopback.

It worked, but it was a scraper against surfaces Anthropic never promised — 12 of its first 20 commits were repairs, most of them to the daemon, and it went dark whenever an OAuth refresh failed. It also spent the quota it measured.

Claude Code later exposed the same official numbers directly to status line commands, which made all of that machinery unnecessary. v0.2.0 removed it. See `docs/BRIEF.md` for the reasoning and `git log` for the menu bar version, which remains in history at `282a7e9`.

If you want the same numbers on demand rather than always-on, Claude Code's built-in `/usage` covers it, and breaks usage down by cause.

## Attribution

- Inspired by [HermannBjorgvin/Clawdmeter](https://github.com/HermannBjorgvin/Clawdmeter) — the original rate-limit header approach.
- Earlier versions embedded pixel-art Clawd animations by [@amaanbuilds](https://github.com/amaanbuilds) from [claudepix.vercel.app](https://claudepix.vercel.app/). Those assets were removed in v0.2.0 and remain only in git history.
- The Clawd character is Anthropic's mascot. This project is not affiliated with or endorsed by Anthropic.

## Legal

Earlier versions bundled third-party visual assets (claudepix sprites) and loaded proprietary Anthropic typefaces, which is why this repository carries a licensing notice rather than a standard open-source license — see `LICENSE`.

**As of v0.2.0 the working tree is original code only**: no sprites, no fonts, no mascot artwork. The notice has not been revised to reflect that, and the removed assets are still reachable in git history. Revisit `LICENSE` before publishing or relicensing.
