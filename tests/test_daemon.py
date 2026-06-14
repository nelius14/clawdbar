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
