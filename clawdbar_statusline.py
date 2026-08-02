#!/usr/bin/env python3
"""Clawd usage in the Claude Code status line.

Claude Code pipes a JSON blob to its status line command on every render,
including the official Claude.ai subscription usage:

    "rate_limits": {
      "five_hour": { "used_percentage": number, "resets_at": number },
      "seven_day": { "used_percentage": number, "resets_at": number }
    }

Those are the same 5h/7d percentages Anthropic returns in its unified
rate-limit headers — handed over directly, as a side effect of requests you
were already making. No OAuth token, no API probe, no daemon.

Renders, e.g.:

    Opus 5 · 5h 62% · 7d 20%
    Opus 5 · 5h 91% (resets in 2h14m) · 7d 34%     <- countdown appears when it matters

Install: see README.md. Requires nothing but Python 3 and stdlib.

This runs on every status line render, so it must never fail loudly: any
error degrades to printing less rather than breaking the status line.
"""
import json
import sys
import time

# Show the reset countdown only once a window is close enough to matter.
# Below this it is noise; above it, it is the number you actually act on.
RESET_HINT_AT = 80

SEPARATOR = " · "

# Claude Code exposes these two windows. Order is display order.
WINDOWS = (("five_hour", "5h"), ("seven_day", "7d"))


def _pct(value) -> int | None:
    """`used_percentage` is already 0-100 — no scaling, unlike the raw headers."""
    try:
        return max(0, min(100, int(round(float(value)))))
    except (TypeError, ValueError):
        return None


def format_duration(seconds: float) -> str:
    """Compact human countdown: 3d4h, 2h14m, 45m, <1m."""
    seconds = int(seconds)
    if seconds < 60:
        return "<1m"
    minutes, hours = (seconds // 60) % 60, seconds // 3600
    if hours >= 24:
        return f"{hours // 24}d{hours % 24}h"
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m"


def format_window(label: str, window, now: float) -> str | None:
    """One window as `5h 62%`, plus a reset countdown when it's running hot."""
    if not isinstance(window, dict):
        return None
    pct = _pct(window.get("used_percentage"))
    if pct is None:
        return None

    text = f"{label} {pct}%"
    if pct >= RESET_HINT_AT:
        try:
            remaining = float(window["resets_at"]) - now
        except (TypeError, ValueError, KeyError):
            remaining = 0
        if remaining > 0:
            text += f" (resets in {format_duration(remaining)})"
    return text


def render(payload: dict, now: float | None = None) -> str:
    """Build the status line. Missing pieces are dropped, never faked."""
    now = time.time() if now is None else now
    parts = []

    model = (payload.get("model") or {}).get("display_name")
    if model:
        parts.append(model)

    rate_limits = payload.get("rate_limits")
    if isinstance(rate_limits, dict):
        for key, label in WINDOWS:
            text = format_window(label, rate_limits.get(key), now)
            if text:
                parts.append(text)

    return SEPARATOR.join(parts)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        return 0
    if not isinstance(payload, dict):
        return 0
    try:
        line = render(payload)
    except Exception:
        return 0
    if line:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
