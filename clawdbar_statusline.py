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

    Opus 5.5 · 5h 62% ↻2h14m · 7d 20%      <- ↻ is time until the 5h window resets
    Opus 5.5 · 5h 96% ↻11m · 7d 34%

Each percentage turns amber at WARN_AT and red at CRIT_AT (ANSI colour, on
by default; set COLOR = False for plain text).

Install: see README.md. Requires nothing but Python 3 and stdlib.

This runs on every status line render, so it must never fail loudly: any
error degrades to printing less rather than breaking the status line.
"""
import json
import sys
import time

SEPARATOR = " · "

# Marks a countdown, so `2h14m` can't be misread as time already used.
RESET_GLYPH = "↻"

# Severity colour on the percentage. Each window is judged on its own.
COLOR = True
WARN_AT = 80   # amber from here
CRIT_AT = 95   # red from here
_AMBER, _RED, _RESET = "\033[33m", "\033[31m", "\033[0m"

# Claude Code exposes these two windows. Order is display order; the flag is
# whether to show the reset countdown.
#
# Only the session window gets one. It resets several times a day, so "how
# long until this clears" is a number you act on. The weekly reset is days
# away and never changes what you do next — showing it is just noise.
WINDOWS = (
    ("five_hour", "5h", True),
    ("seven_day", "7d", False),
)


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


def _paint(pct: int, text: str) -> str:
    """Wrap text in amber/red once pct crosses WARN_AT / CRIT_AT."""
    if pct >= CRIT_AT:
        return f"{_RED}{text}{_RESET}"
    if pct >= WARN_AT:
        return f"{_AMBER}{text}{_RESET}"
    return text


def format_window(label: str, window, now: float, show_reset: bool = False,
                  color: bool = False) -> str | None:
    """One window as `5h 62%`, optionally with its reset countdown appended.

    With color, only the percentage is painted; the label and countdown stay plain.
    """
    if not isinstance(window, dict):
        return None
    pct = _pct(window.get("used_percentage"))
    if pct is None:
        return None

    pct_text = f"{pct}%"
    text = f"{label} {_paint(pct, pct_text) if color else pct_text}"
    if show_reset:
        try:
            remaining = float(window["resets_at"]) - now
        except (TypeError, ValueError, KeyError):
            remaining = 0
        # A reset already in the past is stale data, not a negative countdown.
        if remaining > 0:
            text += f" {RESET_GLYPH}{format_duration(remaining)}"
    return text


def render(payload: dict, now: float | None = None, color: bool = False) -> str:
    """Build the status line. Missing pieces are dropped, never faked.

    Plain by default so render() stays pure; main() opts in via COLOR.
    """
    now = time.time() if now is None else now
    parts = []

    model = (payload.get("model") or {}).get("display_name")
    if model:
        parts.append(model)

    rate_limits = payload.get("rate_limits")
    if isinstance(rate_limits, dict):
        for key, label, show_reset in WINDOWS:
            text = format_window(label, rate_limits.get(key), now, show_reset, color)
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
        line = render(payload, color=COLOR)
    except Exception:
        return 0
    if line:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
