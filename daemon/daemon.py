#!/usr/bin/env python3
"""
Clawdbar daemon — polls Anthropic rate-limit headers, serves locally.

Reads the Claude OAuth token from macOS Keychain (or ~/.claude/.credentials.json
as fallback), POSTs a minimal /v1/messages request, and parses the unified 5h/7d
rate-limit headers into the /usage JSON the menu bar PWA expects. Serves that
payload plus the PWA at GET / on 127.0.0.1:7337.

The bar percentages are Anthropic's official quota utilization — inspired by
https://github.com/HermannBjorgvin/Clawdmeter — not a self-set budget.
"""

import getpass
import glob
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("clawdbar")

# ── server ───────────────────────────────────────────────────────────
HOST = "127.0.0.1"  # loopback only — never exposed on the network
PORT = 7337
POLL_INTERVAL = int(os.environ.get("CLAWDBAR_POLL", "60"))  # seconds between API polls
STATIC_DIR = Path(__file__).parent.parent / "static"

# ── Anthropic API (minimal probe — same as upstream Clawdmeter) ──────
API_URL = "https://api.anthropic.com/v1/messages"
API_HEADERS = {
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "oauth-2025-04-20",
    "Content-Type": "application/json",
    "User-Agent": "claude-code/2.1.5",
}
API_BODY = {
    "model": "claude-haiku-4-5-20251001",
    "max_tokens": 1,
    "messages": [{"role": "user", "content": "hi"}],
}

KEYCHAIN_SERVICE = "Claude Code-credentials"
CREDENTIALS_PATH = Path(
    os.environ.get("CLAWDBAR_CREDENTIALS", str(Path.home() / ".claude" / ".credentials.json"))
)

# ── Activity gating ──────────────────────────────────────────────────
# Only probe the API when Claude Code has been used recently. The OAuth token
# only expires (and stays expired) while Claude Code is idle — probing then just
# fails against a dead token AND would itself open/keep-alive a 5h window. So we
# gate on transcript activity: active → probe; idle → serve the last reading.
PROJECTS_DIR = os.environ.get("CLAWDBAR_PROJECTS", str(Path.home() / ".claude" / "projects"))
ACTIVE_WINDOW = int(os.environ.get("CLAWDBAR_ACTIVE_WINDOW", "720"))  # seconds (12 min)

# ── Auto-relaunch on stale Keychain ──────────────────────────────────
# A long-lived launchd process gets a stale view of the Keychain item: when
# Claude Code rotates the OAuth token (~every 8h) the daemon's `security`
# subprocess keeps returning the OLD token for this launchd session, so the
# API 401/403s forever. A fresh process reads the current token fine. After
# this many consecutive auth failures we exit; the plist's KeepAlive relaunches
# us within ThrottleInterval (~10s) with a working Keychain session.
# See docs/LOG.md 2026-06-19 for the full investigation.
MAX_AUTH_FAILURES = 3

# Shared state served at /usage
_state_lock = threading.Lock()
_state: dict = {
    "ok": False,
    "status": "unknown",
    "error": None,
    "last_updated": None,
    "session_pct": 0,
    "weekly_pct": 0,
    "session_reset_min": None,
    "weekly_reset_min": None,
    "session_reset_at": None,
    "weekly_reset_at": None,
    "source": "api",
    "stale": False,
    "idle": False,
}


class AuthError(Exception):
    """401/403 from the API — token expired or invalid."""


# ── pure helpers (unit-tested) ───────────────────────────────────────

def _extract_access_token(blob: str) -> str | None:
    """Pull accessToken from a credentials blob (JSON or raw token)."""
    blob = blob.strip()
    if not blob:
        return None
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        if isinstance(data.get("accessToken"), str) and data["accessToken"].strip():
            return data["accessToken"]
        for v in data.values():
            if isinstance(v, dict):
                tok = v.get("accessToken")
                if isinstance(tok, str) and tok.strip():
                    return tok
    m = re.search(r'"accessToken"\s*:\s*"([^"]+)"', blob)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_\-.~+/=]{20,}", blob):
        return blob
    return None


def _extract_expires_at(blob: str) -> float | None:
    """Pull expiresAt from a credentials blob → unix seconds (or None).

    Claude Code stores expiresAt as JS milliseconds, top-level or one dict deep.
    """
    if not blob or not blob.strip():
        return None
    val = None
    try:
        data = json.loads(blob)
    except (json.JSONDecodeError, TypeError):
        data = None
    if isinstance(data, dict):
        if isinstance(data.get("expiresAt"), (int, float)):
            val = data["expiresAt"]
        else:
            for v in data.values():
                if isinstance(v, dict) and isinstance(v.get("expiresAt"), (int, float)):
                    val = v["expiresAt"]
                    break
    if val is None:
        m = re.search(r'"expiresAt"\s*:\s*(\d+)', blob)
        if m:
            val = int(m.group(1))
    return val / 1000.0 if val is not None else None


def _read_keychain_blob() -> str | None:
    """Return the raw Keychain password string for KEYCHAIN_SERVICE, or None."""
    try:
        out = subprocess.run(
            [
                "security", "find-generic-password",
                "-s", KEYCHAIN_SERVICE,
                "-a", getpass.getuser(),
                "-w",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return out.stdout
    except subprocess.CalledProcessError as e:
        log.warning("Keychain read failed (rc=%s): %s", e.returncode, e.stderr.strip())
        return None
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        log.warning("Keychain access error: %s", e)
        return None


def read_token_keychain() -> str | None:
    """Read Claude OAuth access token from macOS Keychain."""
    blob = _read_keychain_blob()
    return _extract_access_token(blob) if blob else None


def read_token_file() -> str | None:
    """Read Claude OAuth token from the credentials file."""
    try:
        raw = CREDENTIALS_PATH.read_text(encoding="utf-8")
    except OSError as e:
        log.debug("Could not read credentials file: %s", e)
        return None
    return _extract_access_token(raw)


def read_token() -> str | None:
    """Return the current Claude OAuth access token from Keychain (or file).

    No caching: Claude Code rotates the token in Keychain and we must always
    read the latest. (Staleness is handled at the process level — see
    MAX_AUTH_FAILURES.)
    """
    if sys.platform == "darwin":
        tok = read_token_keychain()
        if tok:
            return tok
    return read_token_file()


def token_should_be_valid(now: float | None = None) -> bool:
    """True if the stored token's expiresAt is still in the future.

    When the API rejects a token that *should* still be valid, this launchd
    process is reading a stale Keychain view — a relaunch fixes it. If the
    token is genuinely expired, relaunching won't help, so we just wait.
    """
    if now is None:
        now = time.time()
    blob = None
    if sys.platform == "darwin":
        blob = _read_keychain_blob()
    if not blob:
        try:
            blob = CREDENTIALS_PATH.read_text(encoding="utf-8")
        except OSError:
            return False
    exp = _extract_expires_at(blob) if blob else None
    return exp is not None and exp > now


def latest_activity_mtime() -> float:
    """Newest mtime among Claude Code transcript files, or 0.0 if none.

    Cheap stat-only scan of ~/.claude/projects/**/*.jsonl. `agent-*` transcripts
    are subagent runs, not user activity, so they're skipped.
    """
    newest = 0.0
    for f in glob.glob(os.path.join(PROJECTS_DIR, "**", "*.jsonl"), recursive=True):
        if os.path.basename(f).startswith("agent-"):
            continue
        try:
            m = os.path.getmtime(f)
        except OSError:
            continue
        if m > newest:
            newest = m
    return newest


def is_active(now: float | None = None) -> bool:
    """True if Claude Code wrote a transcript within ACTIVE_WINDOW seconds.

    While active, Claude Code keeps the Keychain OAuth token fresh, so probing
    succeeds. While idle the token expires — so we don't probe (see ACTIVE_WINDOW).
    """
    if now is None:
        now = time.time()
    return (now - latest_activity_mtime()) < ACTIVE_WINDOW


def _hdr(headers: dict[str, str], name: str, default: str = "0") -> str:
    # HTTP headers are case-insensitive; normalize lookup.
    lower = name.lower()
    for k, v in headers.items():
        if k.lower() == lower:
            return v
    return default


def reset_minutes(reset_ts: str, now: float) -> int | None:
    """Seconds-until-reset header → whole minutes remaining (or None if past)."""
    try:
        r = float(reset_ts)
    except (ValueError, TypeError):
        return None
    mins = (r - now) / 60.0
    return max(0, int(round(mins))) if mins > 0 else None


def _epoch_or_none(reset_ts: str) -> int | None:
    """Reset header (absolute epoch seconds) → int, or None if unparseable."""
    try:
        return int(float(reset_ts))
    except (ValueError, TypeError):
        return None


def _with_live_resets(state: dict, now: float | None = None) -> dict:
    """Recompute *_reset_min from the stored absolute *_reset_at epochs.

    Keeps the "resets in" countdown live at serve-time (and between polls /
    while idle), instead of freezing at whatever it was at the last poll.
    """
    if now is None:
        now = time.time()
    out = dict(state)
    for at_key, min_key in (("session_reset_at", "session_reset_min"),
                            ("weekly_reset_at", "weekly_reset_min")):
        at = state.get(at_key)
        if at is not None:
            out[min_key] = reset_minutes(str(at), now)
    return out


def utilization_pct(util: str) -> int:
    """0.0–1.0 utilization header → integer percentage."""
    try:
        return int(round(float(util) * 100))
    except (ValueError, TypeError):
        return 0


def state_from_headers(headers: dict[str, str], now: float | None = None) -> dict:
    """Build the /usage payload from Anthropic rate-limit response headers."""
    if now is None:
        now = time.time()
    status = _hdr(headers, "anthropic-ratelimit-unified-5h-status", "unknown")
    session_reset_at = _epoch_or_none(_hdr(headers, "anthropic-ratelimit-unified-5h-reset"))
    weekly_reset_at = _epoch_or_none(_hdr(headers, "anthropic-ratelimit-unified-7d-reset"))
    return {
        "ok": True,
        "status": status,
        "error": None,
        "last_updated": int(now),
        "session_pct": utilization_pct(_hdr(headers, "anthropic-ratelimit-unified-5h-utilization")),
        "weekly_pct": utilization_pct(_hdr(headers, "anthropic-ratelimit-unified-7d-utilization")),
        "session_reset_min": reset_minutes(_hdr(headers, "anthropic-ratelimit-unified-5h-reset"), now),
        "weekly_reset_min": reset_minutes(_hdr(headers, "anthropic-ratelimit-unified-7d-reset"), now),
        "session_reset_at": session_reset_at,
        "weekly_reset_at": weekly_reset_at,
        "source": "api",
        "stale": False,
        "idle": False,
    }


def poll_api(token: str, timeout: float = 20.0) -> dict:
    """POST a minimal message and return parsed rate-limit state.

    Raises AuthError on 401/403. Returns error-state dict on other failures.
    """
    body = json.dumps(API_BODY).encode()
    req = urllib.request.Request(
        API_URL,
        data=body,
        headers={**API_HEADERS, "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            # Headers arrive as email.message.Message; convert to plain dict.
            raw = {k: v for k, v in resp.headers.items()}
            return state_from_headers(raw)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise AuthError(e.code) from e
        log.error("API HTTP %s: %s", e.code, e.read()[:200])
        return _error_state(f"API HTTP {e.code}")
    except urllib.error.URLError as e:
        log.error("API call failed: %s", e.reason)
        return _error_state(f"API unreachable: {e.reason}")
    except TimeoutError:
        log.error("API call timed out")
        return _error_state("API timed out")


def _error_state(msg: str) -> dict:
    return {
        "ok": False,
        "status": "error",
        "error": msg,
        "last_updated": int(time.time()),
        "session_pct": 0,
        "weekly_pct": 0,
        "session_reset_min": None,
        "weekly_reset_min": None,
        "session_reset_at": None,
        "weekly_reset_at": None,
        "source": "api",
        "stale": False,
        "idle": False,
    }


# Last successful /usage payload — lets us serve dimmed, clearly-dated data
# instead of blanking the bar while we wait for Claude Code to refresh the token.
_last_good_state: dict | None = None


def _stale_payload(err: str) -> dict:
    """Not-ok payload that preserves the last successful reading.

    Keeps the last-known %s and the timestamp of the last *successful* poll, so
    the menu bar can show "as of HH:MM" dimmed data rather than going dark
    during a token-expiry gap. Falls back to a plain error state if we have
    never had a good reading.
    """
    if _last_good_state is None:
        return _error_state(err)
    return {**_last_good_state, "ok": False, "stale": True, "error": err}


def _idle_payload() -> dict:
    """Payload served while idle — last good reading, flagged idle, no error.

    Idle is normal, not a failure: Claude Code hasn't been used recently so we
    deliberately skip the API probe. Show the last-known data calmly (the
    countdown still ticks via _with_live_resets at serve-time).
    """
    if _last_good_state is None:
        # Nothing measured yet (e.g. daemon (re)started during an idle stretch).
        base = _error_state("idle — no reading yet")
        return {**base, "ok": True, "status": "idle", "error": None, "idle": True}
    return {**_last_good_state, "ok": True, "stale": False, "idle": True, "error": None}


def refresh_state() -> dict:
    """Read token, poll API, return /usage payload.

    Raises AuthError if the API rejects the token even after re-reading
    Keychain (the caller decides whether to relaunch). Returns an error-state
    dict for missing-token and transient network failures.
    """
    token = read_token()
    if not token:
        return _error_state("no token — run `claude login`")
    try:
        return poll_api(token)
    except AuthError:
        # Claude Code may have just rotated the token in Keychain — re-read once
        # and retry before giving up. If the freshly-read token is identical and
        # still rejected, this launchd process likely has a stale Keychain view;
        # the caller relaunches us so a fresh process can read the live token.
        fresh = read_token()
        if fresh and fresh != token:
            return poll_api(fresh)  # propagates AuthError if this also fails
        raise


# ── HTTP serving (loopback) ──────────────────────────────────────────

def serve_file(handler, path: Path, content_type: str):
    try:
        data = path.read_bytes()
        handler.send_response(200)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)
    except FileNotFoundError:
        handler.send_error(404)


def safe_static_path(static_dir: Path, request_path: str) -> Path | None:
    """Resolve a URL path to a file inside static_dir, or None if it escapes it."""
    rel = request_path.split("?", 1)[0].lstrip("/")
    root = static_dir.resolve()
    target = (root / rel).resolve()
    if target == root or root in target.parents:
        return target
    return None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # suppress per-request logs

    def _send_json(self, payload: dict):
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/usage":
            with _state_lock:
                snapshot = dict(_state)
            # Recompute countdowns from absolute reset epochs so "resets in"
            # stays live between polls and while idle.
            self._send_json(_with_live_resets(snapshot))

        elif path == "/refresh":
            # Force an immediate re-poll (the menubar "Refresh Now" button).
            # poll_and_update() updates _state and relaunches if it hits the
            # stale-Keychain failure threshold. Respond first; if the token
            # looks valid but was rejected, relaunch right away so the manual
            # click actually recovers without waiting for the 3-strike loop.
            try:
                result = poll_and_update()
            except Exception as e:
                result = _error_state(f"refresh error: {e}")
            with _state_lock:
                snapshot = dict(_state)
            self._send_json(snapshot)
            if not result.get("ok") and token_should_be_valid():
                threading.Thread(
                    target=lambda: (time.sleep(0.3),
                                    _relaunch("manual refresh with a valid-looking token")),
                    daemon=True,
                ).start()

        elif path in ("/", "/index.html"):
            serve_file(self, STATIC_DIR / "index.html", "text/html; charset=utf-8")

        else:
            target = safe_static_path(STATIC_DIR, path)
            if target and target.is_file():
                ext = target.suffix
                types = {
                    ".js": "text/javascript",
                    ".css": "text/css",
                    ".json": "application/json",
                    ".png": "image/png",
                    ".ico": "image/x-icon",
                    ".webmanifest": "application/manifest+json",
                }
                serve_file(self, target, types.get(ext, "application/octet-stream"))
            else:
                self.send_error(404)


# Consecutive auth failures across polls — drives the stale-Keychain relaunch.
_consecutive_auth_failures = 0
_auth_lock = threading.Lock()

# Tracks idle⇄active transitions so we log them once, not every poll.
_was_idle = False


def _relaunch(reason: str) -> None:
    """Hard-exit so launchd's KeepAlive relaunches us with a fresh process.

    A fresh process gets a fresh Keychain session and reads the live token.
    """
    log.error("Relaunching daemon (%s) — launchd will restart with a fresh Keychain session", reason)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(1)


def poll_and_update() -> dict:
    """Run one poll, update shared `_state`, and return the result dict.

    On auth failure, counts consecutive failures and relaunches the process
    after MAX_AUTH_FAILURES — but only if the stored token still looks valid
    (future expiresAt), the signature of a stale Keychain view. If the token is
    genuinely expired we just wait for Claude Code to refresh it.
    """
    global _consecutive_auth_failures, _last_good_state, _was_idle

    # Activity gate: while Claude Code is idle the OAuth token expires, so a
    # probe would just fail (and would itself keep a 5h window alive). Skip it
    # and serve the last reading. We still probe if we have no reading yet.
    if not is_active() and _last_good_state is not None:
        with _auth_lock:
            _consecutive_auth_failures = 0
        payload = _idle_payload()
        with _state_lock:
            _state.update(payload)
        if not _was_idle:
            log.info("Idle — no Claude Code activity in %ds; pausing API probes, "
                     "serving last reading (session %s%%)", ACTIVE_WINDOW, payload["session_pct"])
            _was_idle = True
        return payload
    if _was_idle:
        log.info("Active again — resuming API probes")
        _was_idle = False

    try:
        result = refresh_state()
    except AuthError:
        with _auth_lock:
            _consecutive_auth_failures += 1
            n = _consecutive_auth_failures
        looks_valid = token_should_be_valid()
        err = ("token rejected but Keychain says valid — stale view"
               if looks_valid else "token expired — run `claude login`")
        payload = _stale_payload(err)
        with _state_lock:
            _state.update(payload)
        log.warning("Auth failed (%d/%d consecutive)%s", n, MAX_AUTH_FAILURES,
                    " — token looks valid, will relaunch" if looks_valid else " — token genuinely expired")
        if n >= MAX_AUTH_FAILURES and looks_valid:
            _relaunch(f"{n} consecutive auth failures with a valid-looking token")
        return payload

    with _auth_lock:
        _consecutive_auth_failures = 0
    if result["ok"]:
        _last_good_state = dict(result)
    else:
        # Transient (network/timeout) failure — keep showing last-known data.
        result = _stale_payload(result["error"])
    with _state_lock:
        _state.update(result)
    if result["ok"]:
        log.info(
            "Poll OK — session %s%% (%s) weekly %s%% — resets in %sm / %sm",
            result["session_pct"], result["status"], result["weekly_pct"],
            result["session_reset_min"], result["weekly_reset_min"],
        )
    else:
        log.error("Poll failed: %s", result["error"])
    return result


def poll_loop():
    backoff = 5  # seconds; doubles on each failure up to POLL_INTERVAL
    while True:
        try:
            result = poll_and_update()
        except Exception as e:
            log.error("Poll loop error: %s", e)
            with _state_lock:
                _state["ok"] = False
                _state["status"] = "error"
                _state["error"] = str(e)
            result = {"ok": False}
        if result.get("ok"):
            backoff = 5
            time.sleep(POLL_INTERVAL)
        else:
            time.sleep(backoff)
            backoff = min(backoff * 2, POLL_INTERVAL)


if __name__ == "__main__":
    log.info("Clawdbar daemon starting on port %s (API poll every %ss)", PORT, POLL_INTERVAL)
    log.info("Credentials: Keychain [%s] then %s", KEYCHAIN_SERVICE, CREDENTIALS_PATH)

    t = threading.Thread(target=poll_loop, daemon=True)
    t.start()

    server = HTTPServer((HOST, PORT), Handler)
    log.info("Serving at http://%s:%s (loopback only)", HOST, PORT)
    server.serve_forever()
