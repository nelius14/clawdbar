"""Unit tests for the Clawdbar status line.

Stdlib + pytest only. The whole tool is one pure function plus a stdin
wrapper, so these cover it end to end.

Run: uv run --with pytest pytest -q
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import clawdbar_statusline as cs  # noqa: E402

NOW = 1_800_000_000.0
SCRIPT = ROOT / "clawdbar_statusline.py"


def _payload(five=None, seven=None, model="Opus 5"):
    rate_limits = {}
    if five is not None:
        rate_limits["five_hour"] = five
    if seven is not None:
        rate_limits["seven_day"] = seven
    out = {"model": {"display_name": model}}
    if rate_limits:
        out["rate_limits"] = rate_limits
    return out


# ---- percentages ---------------------------------------------------------

def test_used_percentage_is_already_0_to_100():
    """The raw API headers are 0.0-1.0; this field is not. No scaling."""
    line = cs.render(_payload(five={"used_percentage": 62.4}), NOW)
    assert line == "Opus 5 · 5h 62%"


def test_both_windows_render_in_order():
    line = cs.render(
        _payload(five={"used_percentage": 62}, seven={"used_percentage": 20}), NOW
    )
    assert line == "Opus 5 · 5h 62% · 7d 20%"


def test_weekly_window_may_be_absent():
    """Claude Code marks seven_day optional."""
    assert cs.render(_payload(five={"used_percentage": 5}), NOW) == "Opus 5 · 5h 5%"


def test_zero_percent_still_renders():
    """0 is a real reading, not a missing one."""
    assert "5h 0%" in cs.render(_payload(five={"used_percentage": 0}), NOW)


@pytest.mark.parametrize("bad", [None, "abc", {}, {"used_percentage": None}])
def test_unusable_window_is_dropped_not_faked(bad):
    line = cs.render(_payload(five=bad, seven={"used_percentage": 30}), NOW)
    assert line == "Opus 5 · 7d 30%"


# ---- reset countdown -----------------------------------------------------

def test_session_countdown_always_shows_even_at_low_usage():
    """The 5h window resets several times a day, so the countdown is always
    worth knowing — not only when you are nearly out."""
    line = cs.render(
        _payload(five={"used_percentage": 40, "resets_at": NOW + 8040}), NOW
    )
    assert line == "Opus 5 · 5h 40% ↻2h14m"


def test_weekly_countdown_is_never_shown():
    """A reset next week is not actionable; it would just crowd the line."""
    line = cs.render(
        _payload(seven={"used_percentage": 91, "resets_at": NOW + 8040}), NOW
    )
    assert line == "Opus 5 · 7d 91%"


def test_both_windows_with_only_the_session_countdown():
    line = cs.render(
        _payload(five={"used_percentage": 62, "resets_at": NOW + 8040},
                 seven={"used_percentage": 20, "resets_at": NOW + 400_000}), NOW
    )
    assert line == "Opus 5 · 5h 62% ↻2h14m · 7d 20%"


def test_countdown_suppressed_once_the_window_has_passed():
    """A reset in the past is stale data, not a negative countdown."""
    line = cs.render(
        _payload(five={"used_percentage": 95, "resets_at": NOW - 60}), NOW
    )
    assert line == "Opus 5 · 5h 95%"


def test_session_window_without_a_reset_time_still_renders():
    line = cs.render(_payload(five={"used_percentage": 99}), NOW)
    assert line == "Opus 5 · 5h 99%"


@pytest.mark.parametrize("seconds,expected", [
    (30, "<1m"),
    (60, "1m"),
    (2700, "45m"),
    (8040, "2h14m"),
    (3660, "1h01m"),
    (100_000, "1d3h"),
])
def test_duration_formatting(seconds, expected):
    assert cs.format_duration(seconds) == expected


# ---- degrading gracefully ------------------------------------------------

def test_no_rate_limits_shows_just_the_model():
    """rate_limits is absent until the session's first API response."""
    assert cs.render({"model": {"display_name": "Opus 5"}}, NOW) == "Opus 5"


def test_empty_payload_renders_nothing():
    assert cs.render({}, NOW) == ""


# ---- the actual hook, as Claude Code invokes it --------------------------

def _run(stdin_text):
    return subprocess.run(
        [sys.executable, str(SCRIPT)], input=stdin_text,
        capture_output=True, text=True, timeout=30,
    )


def test_hook_renders_from_stdin():
    r = _run(json.dumps(_payload(
        five={"used_percentage": 62}, seven={"used_percentage": 20})))
    assert r.returncode == 0
    assert r.stdout.strip() == "Opus 5 · 5h 62% · 7d 20%"


@pytest.mark.parametrize("junk", ["", "not json", "[]", "null", "   "])
def test_hook_never_breaks_the_statusline(junk):
    """A Clawdbar failure must not take the status line down with it."""
    r = _run(junk)
    assert r.returncode == 0
    assert r.stderr == ""
