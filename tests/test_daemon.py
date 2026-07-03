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


@pytest.fixture(autouse=True)
def _isolate_last_good_state(tmp_path, monkeypatch):
    """Keep persisted-state writes out of the real ~/.clawdbar during tests."""
    monkeypatch.setattr(daemon, "LAST_GOOD_STATE_PATH", tmp_path / "last_good_state.json")


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


# ---- token_expires_at / _token_expired -----------------------------------

def test_token_expires_at_reads_keychain(monkeypatch):
    blob = '{"claudeAiOauth": {"expiresAt": 5000000}}'  # ms → 5000.0 s
    monkeypatch.setattr(daemon, "_read_keychain_blob", lambda: blob)
    assert daemon.token_expires_at() == 5000.0


def test_token_expires_at_falls_back_to_file(monkeypatch, tmp_path):
    monkeypatch.setattr(daemon, "_read_keychain_blob", lambda: None)
    cred = tmp_path / ".credentials.json"
    cred.write_text('{"claudeAiOauth": {"expiresAt": 7000000}}')
    monkeypatch.setattr(daemon, "CREDENTIALS_PATH", cred)
    assert daemon.token_expires_at() == 7000.0


def test_token_expires_at_none_when_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(daemon, "_read_keychain_blob", lambda: None)
    monkeypatch.setattr(daemon, "CREDENTIALS_PATH", tmp_path / "nope.json")
    assert daemon.token_expires_at() is None


def test_token_expired_true_when_past(monkeypatch):
    monkeypatch.setattr(daemon, "token_expires_at", lambda: 1000.0)
    assert daemon._token_expired(now=2000.0) is True


def test_token_expired_false_when_future(monkeypatch):
    monkeypatch.setattr(daemon, "token_expires_at", lambda: 3000.0)
    assert daemon._token_expired(now=2000.0) is False


def test_token_expired_false_when_unknown(monkeypatch):
    monkeypatch.setattr(daemon, "token_expires_at", lambda: None)
    assert daemon._token_expired(now=2000.0) is False


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


# ---- graceful stale state (serve last-known data while token refreshes) -

def _reset_poll_globals():
    daemon._consecutive_auth_failures = 0
    daemon._last_good_state = None
    daemon._state.update({
        "ok": False, "status": "unknown", "error": None, "last_updated": None,
        "session_pct": 0, "weekly_pct": 0, "session_reset_min": None,
        "weekly_reset_min": None, "source": "api", "stale": False, "idle": False,
        "session_reset_at": None, "weekly_reset_at": None,
    })


def test_state_from_headers_not_stale():
    st = daemon.state_from_headers({}, now=1000)
    assert st["stale"] is False


# ---- _expire_windows -----------------------------------------------------

def _good(**over):
    base = {
        "ok": True, "status": "allowed", "error": None, "last_updated": 1000,
        "session_pct": 24, "weekly_pct": 31, "session_reset_min": 5,
        "weekly_reset_min": 500, "session_reset_at": 2000, "weekly_reset_at": 9000,
        "source": "api", "stale": False, "idle": False, "token_state": "ok",
    }
    base.update(over)
    return base


def test_expire_windows_zeroes_elapsed_session():
    # now is past session_reset_at (2000) but before weekly_reset_at (9000)
    out = daemon._expire_windows(_good(), now=3000)
    assert out["session_pct"] == 0
    assert out["session_reset_min"] is None
    assert out["session_reset_at"] is None
    assert out["weekly_pct"] == 31          # weekly untouched
    assert out["weekly_reset_at"] == 9000


def test_expire_windows_zeroes_elapsed_weekly():
    out = daemon._expire_windows(_good(), now=10000)
    assert out["session_pct"] == 0          # both elapsed
    assert out["weekly_pct"] == 0
    assert out["weekly_reset_min"] is None
    assert out["weekly_reset_at"] is None


def test_expire_windows_noop_before_reset():
    out = daemon._expire_windows(_good(), now=1500)
    assert out == _good()                    # nothing elapsed → unchanged copy


def test_expire_windows_noop_when_reset_at_absent():
    st = _good(session_reset_at=None, weekly_reset_at=None)
    out = daemon._expire_windows(st, now=10**12)
    assert out == st                         # can't judge → leave as-is


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
    # Active so probes actually run (else the idle gate serves _idle_payload and
    # refresh_state is never reached — CI has no ~/.claude/projects activity).
    monkeypatch.setattr(daemon, "is_active", lambda: True)
    # Two failures (< MAX_AUTH_FAILURES) → serve stale, no relaunch.

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


def test_save_and_load_last_good_state_roundtrip():
    state = {
        "ok": True, "status": "allowed", "error": None, "last_updated": 1000,
        "session_pct": 42, "weekly_pct": 7, "session_reset_min": 100,
        "weekly_reset_min": 500, "source": "api", "stale": False, "idle": False,
        "session_reset_at": 1234, "weekly_reset_at": 5678,
    }
    daemon._save_last_good_state(state)
    assert daemon._load_last_good_state() == state


def test_load_last_good_state_missing_returns_none():
    # autouse fixture points the path at a fresh tmp dir; nothing written yet.
    assert daemon._load_last_good_state() is None


def test_load_last_good_state_corrupt_returns_none():
    daemon.LAST_GOOD_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    daemon.LAST_GOOD_STATE_PATH.write_text("{ not json")
    assert daemon._load_last_good_state() is None


def test_load_last_good_state_rejects_non_good():
    # A persisted not-ok payload is meaningless as a "last good" fallback.
    daemon._save_last_good_state({"ok": False, "session_pct": 9})
    assert daemon._load_last_good_state() is None


def test_poll_and_update_persists_last_good_on_success(monkeypatch):
    _reset_poll_globals()
    good = daemon.state_from_headers({
        "anthropic-ratelimit-unified-5h-utilization": "0.33",
    }, now=2000)
    monkeypatch.setattr(daemon, "is_active", lambda: True)
    monkeypatch.setattr(daemon, "refresh_state", lambda: good)

    daemon.poll_and_update()

    persisted = daemon._load_last_good_state()
    assert persisted is not None
    assert persisted["session_pct"] == 33


def test_stale_reading_survives_relaunch(monkeypatch):
    # The actual bug: the auto-relaunch (os._exit) wipes the in-memory
    # _last_good_state, so after a relaunch a still-failing token would drop the
    # bar to --% instead of the dimmed last reading. Persisting + reloading it
    # must let _stale_payload keep serving the last % across the relaunch.
    _reset_poll_globals()
    good = {
        "ok": True, "status": "allowed", "error": None, "last_updated": 1000,
        "session_pct": 88, "weekly_pct": 41, "session_reset_min": 100,
        "weekly_reset_min": 500, "source": "api", "stale": False, "idle": False,
        "session_reset_at": None, "weekly_reset_at": None,
    }
    daemon._save_last_good_state(good)

    # Simulate the relaunch: fresh process → in-memory cache gone, reload disk.
    daemon._last_good_state = None
    daemon._last_good_state = daemon._load_last_good_state()

    # Token still rejected after the relaunch (genuine outage continues).
    monkeypatch.setattr(daemon, "RELAUNCH_STAMP", daemon.LAST_GOOD_STATE_PATH.parent / "last_relaunch")
    monkeypatch.setattr(daemon, "is_active", lambda: True)
    monkeypatch.setattr(daemon, "refresh_state",
                        lambda: (_ for _ in ()).throw(daemon.AuthError(401)))
    monkeypatch.setattr(daemon, "_relaunch", lambda reason: None)

    s = daemon.poll_and_update()
    assert s["ok"] is False
    assert s["stale"] is True
    assert s["session_pct"] == 88          # preserved across the relaunch
    assert s["weekly_pct"] == 41


def test_poll_and_update_error_without_prior_good_is_not_stale(monkeypatch):
    _reset_poll_globals()
    monkeypatch.setattr(daemon, "refresh_state",
                        lambda: (_ for _ in ()).throw(daemon.AuthError(401)))
    s = daemon.poll_and_update()
    assert s["ok"] is False
    assert s["stale"] is False             # nothing to fall back to
    assert s["session_pct"] == 0


# ---- waiting-for-claude state --------------------------------------------

def test_poll_idle_expired_token_serves_waiting(monkeypatch):
    _reset_poll_globals()
    daemon._last_good_state = _good()
    monkeypatch.setattr(daemon, "is_active", lambda: False)      # idle
    monkeypatch.setattr(daemon, "_token_expired", lambda now=None: True)
    spy = {"probed": False}
    monkeypatch.setattr(daemon, "refresh_state",
                        lambda: spy.__setitem__("probed", True) or _good())

    payload = daemon.poll_and_update()

    assert spy["probed"] is False                                # never probed
    assert payload["token_state"] == "waiting_for_claude"
    assert payload["ok"] is False and payload["stale"] is True
    assert payload["error"] == "token expired — waiting for Claude Code"


def test_poll_idle_valid_token_still_moon(monkeypatch):
    _reset_poll_globals()
    daemon._last_good_state = _good()
    monkeypatch.setattr(daemon, "is_active", lambda: False)      # idle
    monkeypatch.setattr(daemon, "_token_expired", lambda now=None: False)

    payload = daemon.poll_and_update()

    assert payload["idle"] is True
    assert payload["token_state"] == "ok"                        # normal resting


def test_auth_failure_while_idle_does_not_relaunch(monkeypatch):
    # Forced refresh (e.g. wake) hits an expired token while Claude Code is idle:
    # no fresh token can exist, so serve waiting and DON'T relaunch.
    _reset_poll_globals()
    daemon._last_good_state = _good()
    monkeypatch.setattr(daemon, "is_active", lambda: False)      # idle
    monkeypatch.setattr(daemon, "refresh_state",
                        lambda: (_ for _ in ()).throw(daemon.AuthError(401)))
    relaunched = {"n": 0}
    monkeypatch.setattr(daemon, "_relaunch",
                        lambda reason: relaunched.__setitem__("n", relaunched["n"] + 1))
    monkeypatch.setattr(daemon, "_should_relaunch_for_stale_view", lambda now=None: True)

    payload = daemon.poll_and_update(force=True)

    assert relaunched["n"] == 0                                  # no futile relaunch
    assert payload["token_state"] == "waiting_for_claude"


def test_auth_failure_while_active_can_relaunch(monkeypatch):
    _reset_poll_globals()
    daemon._last_good_state = _good()
    monkeypatch.setattr(daemon, "is_active", lambda: True)       # active → fresh token may exist
    daemon._consecutive_auth_failures = daemon.MAX_AUTH_FAILURES - 1
    monkeypatch.setattr(daemon, "refresh_state",
                        lambda: (_ for _ in ()).throw(daemon.AuthError(401)))
    relaunched = {"n": 0}
    monkeypatch.setattr(daemon, "_relaunch",
                        lambda reason: relaunched.__setitem__("n", relaunched["n"] + 1))
    monkeypatch.setattr(daemon, "_should_relaunch_for_stale_view", lambda now=None: True)

    daemon.poll_and_update(force=True)

    assert relaunched["n"] == 1                                  # stale-view relaunch still works


# ---- activity gating (only probe when Claude Code is active) ------------

def _make_transcript(dirpath, name, age_s):
    import os, time as _t
    p = dirpath / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{}\n", encoding="utf-8")
    t = _t.time() - age_s
    os.utime(p, (t, t))
    return p


def test_latest_activity_mtime_newest_excludes_agents(monkeypatch, tmp_path):
    import time as _t
    proj = tmp_path / "projects"
    _make_transcript(proj / "a", "old.jsonl", 3600)        # 1h old
    _make_transcript(proj / "b", "recent.jsonl", 30)        # 30s old
    _make_transcript(proj / "c", "agent-skip.jsonl", 1)     # newest but agent-* → ignored
    monkeypatch.setattr(daemon, "PROJECTS_DIR", str(proj))
    newest = daemon.latest_activity_mtime()
    # newest non-agent file is ~30s old
    assert _t.time() - newest < 60
    assert _t.time() - newest > 10


def test_latest_activity_mtime_empty_when_none(monkeypatch, tmp_path):
    monkeypatch.setattr(daemon, "PROJECTS_DIR", str(tmp_path / "nope"))
    assert daemon.latest_activity_mtime() == 0.0


def test_is_active(monkeypatch, tmp_path):
    proj = tmp_path / "projects"
    monkeypatch.setattr(daemon, "PROJECTS_DIR", str(proj))
    monkeypatch.setattr(daemon, "ACTIVE_WINDOW", 720)
    _make_transcript(proj / "a", "x.jsonl", 60)             # 1 min ago → active
    assert daemon.is_active() is True
    _make_transcript(proj / "a", "x.jsonl", 3600)           # 1h ago → idle
    assert daemon.is_active() is False


def test_poll_and_update_idle_skips_probe(monkeypatch):
    _reset_poll_globals()
    daemon._last_good_state = {
        "ok": True, "status": "allowed", "error": None, "last_updated": 1000,
        "session_pct": 55, "weekly_pct": 12, "session_reset_min": 100,
        "weekly_reset_min": 500, "source": "api", "stale": False, "idle": False,
        "session_reset_at": None, "weekly_reset_at": None,
    }
    probed = {"n": 0}

    def spy_refresh():
        probed["n"] += 1
        raise AssertionError("must not probe the API while idle")

    monkeypatch.setattr(daemon, "refresh_state", spy_refresh)
    monkeypatch.setattr(daemon, "is_active", lambda: False)

    s = daemon.poll_and_update()
    assert probed["n"] == 0                 # never called the API
    assert s["ok"] is True
    assert s["idle"] is True
    assert s["stale"] is False
    assert s["session_pct"] == 55           # last-known preserved
    assert s["error"] is None
    assert daemon._consecutive_auth_failures == 0


def test_poll_and_update_force_probes_while_idle(monkeypatch):
    # A manual "Refresh Now" (force=True) must probe for real even while idle,
    # bypassing the activity gate, and clear the idle flag with the fresh read.
    _reset_poll_globals()
    daemon._last_good_state = {
        "ok": True, "status": "allowed", "error": None, "last_updated": 1000,
        "session_pct": 55, "weekly_pct": 12, "session_reset_min": 100,
        "weekly_reset_min": 500, "source": "api", "stale": False, "idle": False,
        "session_reset_at": None, "weekly_reset_at": None,
    }
    fresh = daemon.state_from_headers({}, now=2000)
    probed = {"n": 0}

    def spy_refresh():
        probed["n"] += 1
        return fresh

    monkeypatch.setattr(daemon, "refresh_state", spy_refresh)
    monkeypatch.setattr(daemon, "is_active", lambda: False)  # idle

    s = daemon.poll_and_update(force=True)
    assert probed["n"] == 1          # forced through the idle gate
    assert s["ok"] is True
    assert s["idle"] is False        # serving the fresh reading, not resting


def test_poll_and_update_active_probes(monkeypatch):
    _reset_poll_globals()
    good = daemon.state_from_headers({}, now=2000)
    monkeypatch.setattr(daemon, "is_active", lambda: True)
    monkeypatch.setattr(daemon, "refresh_state", lambda: good)
    s = daemon.poll_and_update()
    assert s["ok"] is True
    assert s["idle"] is False


# ---- stale-Keychain relaunch (cooldown-gated, not expiresAt-gated) -------

def test_should_relaunch_for_stale_view_cooldown(monkeypatch, tmp_path):
    import time as _t
    stamp = tmp_path / "last_relaunch"
    monkeypatch.setattr(daemon, "RELAUNCH_STAMP", stamp)
    monkeypatch.setattr(daemon, "RELAUNCH_COOLDOWN", 600)

    # No stamp yet → a relaunch could help.
    assert daemon._should_relaunch_for_stale_view() is True

    # Just relaunched → within cooldown, a fresh process already failed → wait.
    stamp.write_text(str(_t.time()))
    assert daemon._should_relaunch_for_stale_view() is False

    # Stamp older than the cooldown → worth retrying a relaunch.
    stamp.write_text(str(_t.time() - 601))
    assert daemon._should_relaunch_for_stale_view() is True


def test_poll_and_update_relaunches_on_stale_view(monkeypatch, tmp_path):
    # The historical bug: the relaunch was gated on the stored token's expiresAt,
    # read from the *same* stale Keychain view — so it reported "expired" and the
    # daemon never relaunched, staying dark for hours even though a fresh process
    # would read a perfectly valid token. The relaunch must depend only on the
    # cooldown, not on a re-read of the stale view.
    _reset_poll_globals()
    monkeypatch.setattr(daemon, "RELAUNCH_STAMP", tmp_path / "last_relaunch")
    monkeypatch.setattr(daemon, "is_active", lambda: True)
    monkeypatch.setattr(daemon, "refresh_state",
                        lambda: (_ for _ in ()).throw(daemon.AuthError(401)))
    relaunches = []
    monkeypatch.setattr(daemon, "_relaunch", lambda reason: relaunches.append(reason))

    for _ in range(daemon.MAX_AUTH_FAILURES):
        daemon.poll_and_update()

    assert len(relaunches) == 1


def test_poll_and_update_no_relaunch_within_cooldown(monkeypatch, tmp_path):
    # A fresh process that *still* can't authenticate this soon after a relaunch
    # means the Keychain token is genuinely bad — relaunching won't help, so wait.
    import time as _t
    _reset_poll_globals()
    stamp = tmp_path / "last_relaunch"
    stamp.write_text(str(_t.time()))            # relaunched just now
    monkeypatch.setattr(daemon, "RELAUNCH_STAMP", stamp)
    monkeypatch.setattr(daemon, "RELAUNCH_COOLDOWN", 600)
    monkeypatch.setattr(daemon, "is_active", lambda: True)
    monkeypatch.setattr(daemon, "refresh_state",
                        lambda: (_ for _ in ()).throw(daemon.AuthError(401)))
    relaunches = []
    monkeypatch.setattr(daemon, "_relaunch", lambda reason: relaunches.append(reason))

    for _ in range(daemon.MAX_AUTH_FAILURES + 2):
        daemon.poll_and_update()

    assert relaunches == []


def test_with_live_resets_recomputes_from_epoch():
    now = 10_000.0
    state = {
        "session_reset_at": int(now + 3600),   # 60 min out
        "weekly_reset_at": int(now + 120),     # 2 min out
        "session_reset_min": 999,              # stale value to be overwritten
        "weekly_reset_min": 999,
    }
    out = daemon._with_live_resets(state, now=now)
    assert out["session_reset_min"] == 60
    assert out["weekly_reset_min"] == 2


# ---- _serve_payload (serve-time composition) -----------------------------

def test_serve_payload_zeroes_window_that_reset_since_last_poll():
    # Written while the session window was still open (pct 24, resets at 2000),
    # but served after it elapsed (now=3000): the served pct must be 0, not 24.
    st = _good(session_pct=24, session_reset_at=2000, session_reset_min=5)
    out = daemon._serve_payload(st, now=3000)
    assert out["session_pct"] == 0
    assert out["session_reset_min"] is None
    assert out["session_reset_at"] is None
    assert out["weekly_pct"] == 31            # weekly window still open → untouched


def test_serve_payload_keeps_live_countdown_before_reset():
    # Before any reset: pct preserved, countdown recomputed live from reset_at.
    st = _good(session_pct=24, session_reset_at=2000)
    out = daemon._serve_payload(st, now=1400)   # 600s → 10 min to session reset
    assert out["session_pct"] == 24
    assert out["session_reset_min"] == 10


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


# ---- _credential_changed -------------------------------------------------

def test_credential_changed_seeds_then_detects(monkeypatch):
    daemon._last_seen_expires_at = None
    vals = iter([5000.0, 5000.0, 9000.0])
    monkeypatch.setattr(daemon, "token_expires_at", lambda: next(vals))
    assert daemon._credential_changed() is False   # first call seeds
    assert daemon._credential_changed() is False   # unchanged
    assert daemon._credential_changed() is True    # Claude Code refreshed


def test_credential_changed_read_failure_is_false(monkeypatch):
    daemon._last_seen_expires_at = 5000.0
    def boom():
        raise RuntimeError("keychain locked")
    monkeypatch.setattr(daemon, "token_expires_at", boom)
    assert daemon._credential_changed() is False   # never crashes the poll loop
