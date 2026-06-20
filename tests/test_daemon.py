"""Unit tests for the Clawdmeter daemon's pure logic.

Stdlib + pytest only — no network, no pyobjc, no real Keychain. Covers token
extraction, rate-limit header parsing, and the static path-traversal guard.

Run: uv run --with pytest pytest -q
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "daemon"))
import daemon  # noqa: E402


# ---- _extract_access_token ----------------------------------------------

def test_extract_access_token_direct_json():
    blob = json.dumps({"accessToken": "sk-ant-test-token-1234567890"})
    assert daemon._extract_access_token(blob) == "sk-ant-test-token-1234567890"


def test_extract_access_token_nested_oauth():
    blob = json.dumps({"claudeAiOauth": {"accessToken": "nested-token-abc1234567890"}})
    assert daemon._extract_access_token(blob) == "nested-token-abc1234567890"


def test_extract_access_token_regex_fallback():
    blob = 'garbage {"accessToken": "regex-token-abc1234567890"} more'
    assert daemon._extract_access_token(blob) == "regex-token-abc1234567890"


def test_extract_access_token_raw_token():
    assert daemon._extract_access_token("sk-ant-raw-token-abcdefghijklmnop") == "sk-ant-raw-token-abcdefghijklmnop"


def test_extract_access_token_empty():
    assert daemon._extract_access_token("") is None
    assert daemon._extract_access_token("{}") is None


# ---- _extract_expires_at ------------------------------------------------

def test_extract_expires_at_direct():
    blob = json.dumps({"accessToken": "t", "expiresAt": 1_700_000_000_000})
    assert daemon._extract_expires_at(blob) == 1_700_000_000.0  # ms → seconds


def test_extract_expires_at_nested():
    blob = json.dumps({"claudeAiOauth": {"expiresAt": 1_700_000_000_000}})
    assert daemon._extract_expires_at(blob) == 1_700_000_000.0


def test_extract_expires_at_regex_fallback():
    blob = 'noise {"expiresAt": 1700000000000} more'
    assert daemon._extract_expires_at(blob) == 1_700_000_000.0


def test_extract_expires_at_missing():
    assert daemon._extract_expires_at(json.dumps({"accessToken": "t"})) is None
    assert daemon._extract_expires_at("") is None
    assert daemon._extract_expires_at("not json, no field") is None


# ---- utilization_pct / reset_minutes ------------------------------------

def test_utilization_pct():
    assert daemon.utilization_pct("0.42") == 42
    assert daemon.utilization_pct("0.006") == 1
    assert daemon.utilization_pct("bad") == 0


def test_reset_minutes_future():
    now = 1_700_000_000.0
    reset = now + 90 * 60  # 90 minutes from now
    assert daemon.reset_minutes(str(reset), now) == 90


def test_reset_minutes_past():
    now = 1_700_000_000.0
    assert daemon.reset_minutes(str(now - 60), now) is None


def test_reset_minutes_invalid():
    assert daemon.reset_minutes("not-a-ts", 0) is None


# ---- state_from_headers -------------------------------------------------

def test_state_from_headers_happy():
    now = 1_700_000_000.0
    reset_5h = now + 3 * 3600
    reset_7d = now + 5 * 86400
    headers = {
        "anthropic-ratelimit-unified-5h-utilization": "0.47",
        "anthropic-ratelimit-unified-5h-reset": str(reset_5h),
        "anthropic-ratelimit-unified-7d-utilization": "0.15",
        "anthropic-ratelimit-unified-7d-reset": str(reset_7d),
        "anthropic-ratelimit-unified-5h-status": "active",
    }
    st = daemon.state_from_headers(headers, now=now)
    assert st["ok"] is True
    assert st["session_pct"] == 47
    assert st["weekly_pct"] == 15
    assert st["status"] == "active"
    assert st["session_reset_min"] == 180
    assert st["weekly_reset_min"] == pytest.approx(5 * 86400 / 60, abs=1)
    assert st["source"] == "api"


def test_state_from_headers_case_insensitive():
    now = 1_700_000_000.0
    headers = {
        "Anthropic-Ratelimit-Unified-5h-Utilization": "0.10",
        "ANTHROPIC-RATELIMIT-UNIFIED-7D-UTILIZATION": "0.20",
    }
    st = daemon.state_from_headers(headers, now=now)
    assert st["session_pct"] == 10
    assert st["weekly_pct"] == 20


def test_state_from_headers_missing_defaults():
    st = daemon.state_from_headers({}, now=1_700_000_000.0)
    assert st["session_pct"] == 0
    assert st["weekly_pct"] == 0
    assert st["status"] == "unknown"


# ---- refresh_state / relaunch decision ----------------------------------

def test_refresh_state_no_token(monkeypatch):
    monkeypatch.setattr(daemon, "read_token", lambda: None)
    st = daemon.refresh_state()
    assert st["ok"] is False
    assert "no token" in st["error"]


def test_refresh_state_raises_on_persistent_rejection(monkeypatch):
    # Same token read both times + always rejected → propagate AuthError so the
    # caller can decide to relaunch.
    monkeypatch.setattr(daemon, "read_token", lambda: "same-token")

    def reject(token, timeout=20.0):
        raise daemon.AuthError(401)

    monkeypatch.setattr(daemon, "poll_api", reject)
    with pytest.raises(daemon.AuthError):
        daemon.refresh_state()


def test_refresh_state_retries_with_rotated_token(monkeypatch):
    tokens = iter(["old-token", "new-token"])
    monkeypatch.setattr(daemon, "read_token", lambda: next(tokens))
    seen = []

    def poll(token, timeout=20.0):
        seen.append(token)
        if token == "old-token":
            raise daemon.AuthError(401)
        return {"ok": True, "session_pct": 5}

    monkeypatch.setattr(daemon, "poll_api", poll)
    st = daemon.refresh_state()
    assert st["ok"] is True
    assert seen == ["old-token", "new-token"]


def test_token_should_be_valid(monkeypatch):
    import time as _t
    monkeypatch.setattr(daemon.sys, "platform", "darwin")
    future = int((_t.time() + 3600) * 1000)
    monkeypatch.setattr(daemon, "_read_keychain_blob",
                        lambda: json.dumps({"accessToken": "t", "expiresAt": future}))
    assert daemon.token_should_be_valid() is True

    past = int((_t.time() - 3600) * 1000)
    monkeypatch.setattr(daemon, "_read_keychain_blob",
                        lambda: json.dumps({"accessToken": "t", "expiresAt": past}))
    assert daemon.token_should_be_valid() is False


# ---- graceful stale state (serve last-known data while token refreshes) -

def _reset_poll_globals():
    daemon._consecutive_auth_failures = 0
    daemon._last_good_state = None
    daemon._state.update({
        "ok": False, "status": "unknown", "error": None, "last_updated": None,
        "session_pct": 0, "weekly_pct": 0, "session_reset_min": None,
        "weekly_reset_min": None, "source": "api", "stale": False,
    })


def test_state_from_headers_not_stale():
    st = daemon.state_from_headers({}, now=1000)
    assert st["stale"] is False


def test_poll_and_update_serves_stale_after_failure(monkeypatch):
    _reset_poll_globals()
    good = {
        "ok": True, "status": "allowed", "error": None, "last_updated": 1000,
        "session_pct": 42, "weekly_pct": 7, "session_reset_min": 100,
        "weekly_reset_min": 500, "source": "api", "stale": False,
    }
    calls = {"n": 0}

    def fake_refresh():
        calls["n"] += 1
        if calls["n"] == 1:
            return good
        raise daemon.AuthError(401)

    monkeypatch.setattr(daemon, "refresh_state", fake_refresh)
    # Genuinely expired → no relaunch, just wait (the dark-window path).
    monkeypatch.setattr(daemon, "token_should_be_valid", lambda: False)

    daemon.poll_and_update()            # success → records last-good
    s1 = daemon.poll_and_update()       # 1st failure
    s2 = daemon.poll_and_update()       # 2nd consecutive failure

    for s in (s1, s2):
        assert s["ok"] is False
        assert s["stale"] is True
        assert s["session_pct"] == 42      # preserved, not zeroed to 0
        assert s["weekly_pct"] == 7
        assert s["last_updated"] == 1000   # last *success*, not the failure time
        assert s["error"]


def test_poll_and_update_error_without_prior_good_is_not_stale(monkeypatch):
    _reset_poll_globals()
    monkeypatch.setattr(daemon, "refresh_state",
                        lambda: (_ for _ in ()).throw(daemon.AuthError(401)))
    monkeypatch.setattr(daemon, "token_should_be_valid", lambda: False)
    s = daemon.poll_and_update()
    assert s["ok"] is False
    assert s["stale"] is False             # nothing to fall back to
    assert s["session_pct"] == 0


# ---- safe_static_path (path-traversal guard) ----------------------------

def test_safe_static_path_allows_normal_files():
    root = daemon.STATIC_DIR
    assert daemon.safe_static_path(root, "/index.html") is not None
    assert daemon.safe_static_path(root, "/manifest.webmanifest") is not None


def test_safe_static_path_blocks_traversal():
    root = daemon.STATIC_DIR
    assert daemon.safe_static_path(root, "/../../.claude/.credentials.json") is None
    assert daemon.safe_static_path(root, "/../daemon/daemon.py") is None


def test_safe_static_path_strips_query():
    p = daemon.safe_static_path(daemon.STATIC_DIR, "/index.html?v=2")
    assert p is not None and p.name == "index.html"


# ---- loopback invariant -------------------------------------------------

def test_daemon_binds_loopback_only():
    assert daemon.HOST == "127.0.0.1"
