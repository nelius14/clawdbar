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
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
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

# ── OAuth token refresh ──────────────────────────────────────────────
OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
# Public client ID for Claude Code CLI (no secret required — PKCE flow).
# Override with CLAWDBAR_OAUTH_CLIENT_ID if this ever changes.
OAUTH_CLIENT_ID = os.environ.get(
    "CLAWDBAR_OAUTH_CLIENT_ID", "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
)

# In-memory cache for a freshly-refreshed token so it survives a stale Keychain.
_token_cache: dict = {}
_token_lock = threading.Lock()

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
    "source": "api",
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


def _extract_refresh_token(blob: str) -> str | None:
    """Pull refreshToken from a credentials blob."""
    blob = blob.strip()
    if not blob:
        return None
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict):
        if isinstance(data.get("refreshToken"), str) and data["refreshToken"].strip():
            return data["refreshToken"]
        for v in data.values():
            if isinstance(v, dict):
                tok = v.get("refreshToken")
                if isinstance(tok, str) and tok.strip():
                    return tok
    return None


def _update_credentials_file(new_tokens: dict) -> None:
    """Atomically write refreshed access/refresh tokens back to the credentials file."""
    try:
        raw = CREDENTIALS_PATH.read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception as e:
        log.debug("Cannot read credentials for update: %s", e)
        return

    # Locate the dict that owns accessToken (top-level or one level deep).
    if "accessToken" in data:
        target = data
    else:
        target = next(
            (v for v in data.values() if isinstance(v, dict) and "accessToken" in v),
            None,
        )
    if target is None:
        return

    target["accessToken"] = new_tokens["access_token"]
    if "refresh_token" in new_tokens:
        target["refreshToken"] = new_tokens["refresh_token"]
    if "expires_in" in new_tokens:
        # Claude Code stores expiresAt as JS milliseconds.
        target["expiresAt"] = int((time.time() + new_tokens["expires_in"]) * 1000)

    tmp = CREDENTIALS_PATH.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(CREDENTIALS_PATH)
    except Exception as e:
        log.warning("Could not persist refreshed tokens: %s", e)
        tmp.unlink(missing_ok=True)


def try_refresh_oauth_token() -> str | None:
    """Use the stored refresh token to get a new access token.

    On success, caches the token in memory and updates the credentials file.
    Returns the new access token, or None if refresh is not possible.
    """
    try:
        raw = CREDENTIALS_PATH.read_text(encoding="utf-8")
    except OSError as e:
        log.debug("Cannot read credentials for refresh: %s", e)
        return None

    refresh_tok = _extract_refresh_token(raw)
    if not refresh_tok:
        log.warning("No refresh token in credentials — cannot auto-refresh")
        return None

    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_tok,
        "client_id": OAUTH_CLIENT_ID,
    }).encode()
    req = urllib.request.Request(
        OAUTH_TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20.0) as resp:
            response = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        log.warning("Token refresh HTTP %s: %s", e.code, e.read()[:200])
        return None
    except Exception as e:
        log.warning("Token refresh failed: %s", e)
        return None

    access_token = response.get("access_token")
    if not access_token:
        log.warning("Token refresh response missing access_token")
        return None

    expires_in = response.get("expires_in", 3600)
    with _token_lock:
        _token_cache["token"] = access_token
        _token_cache["expires_at"] = time.time() + expires_in - 60  # 60s safety buffer

    _update_credentials_file(response)
    log.info("OAuth token refreshed successfully (expires in %ss)", expires_in)
    return access_token


def read_token_keychain() -> str | None:
    """Read Claude OAuth token from macOS Keychain."""
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
    except subprocess.CalledProcessError as e:
        log.warning("Keychain read failed (rc=%s): %s", e.returncode, e.stderr.strip())
        return None
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        log.warning("Keychain access error: %s", e)
        return None
    return _extract_access_token(out.stdout)


def read_token_file() -> str | None:
    """Read Claude OAuth token from the credentials file."""
    try:
        raw = CREDENTIALS_PATH.read_text(encoding="utf-8")
    except OSError as e:
        log.debug("Could not read credentials file: %s", e)
        return None
    return _extract_access_token(raw)


def read_token() -> str | None:
    """Return the Claude OAuth access token, or None.

    Checks the in-memory refresh cache first so a freshly-refreshed token
    is used even if the Keychain still holds the old expired one.
    """
    with _token_lock:
        if _token_cache.get("token") and time.time() < _token_cache.get("expires_at", 0):
            return _token_cache["token"]
    if sys.platform == "darwin":
        tok = read_token_keychain()
        if tok:
            return tok
    return read_token_file()


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
    return {
        "ok": True,
        "status": status,
        "error": None,
        "last_updated": int(now),
        "session_pct": utilization_pct(_hdr(headers, "anthropic-ratelimit-unified-5h-utilization")),
        "weekly_pct": utilization_pct(_hdr(headers, "anthropic-ratelimit-unified-7d-utilization")),
        "session_reset_min": reset_minutes(_hdr(headers, "anthropic-ratelimit-unified-5h-reset"), now),
        "weekly_reset_min": reset_minutes(_hdr(headers, "anthropic-ratelimit-unified-7d-reset"), now),
        "source": "api",
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
        "source": "api",
    }


def refresh_state() -> dict:
    """Read token, poll API, return /usage payload. Auto-refreshes on 401."""
    token = read_token()
    if not token:
        return _error_state("no token — run `claude login`")
    try:
        return poll_api(token)
    except AuthError:
        log.info("Access token rejected — attempting OAuth refresh")
        new_token = try_refresh_oauth_token()
        if new_token:
            try:
                return poll_api(new_token)
            except AuthError:
                pass
        return _error_state("token expired — run `claude login`")


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

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/usage":
            with _state_lock:
                data = json.dumps(_state).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

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


def poll_loop():
    while True:
        try:
            result = refresh_state()
            with _state_lock:
                # Keep last good percentages on transient API errors
                if not result["ok"] and _state.get("ok"):
                    result = {**_state, "ok": False, "error": result["error"],
                              "last_updated": result["last_updated"]}
                _state.update(result)
            if result["ok"]:
                log.info(
                    "Poll OK — session %s%% (%s) weekly %s%% — resets in %sm / %sm",
                    result["session_pct"], result["status"], result["weekly_pct"],
                    result["session_reset_min"], result["weekly_reset_min"],
                )
            else:
                log.error("Poll failed: %s", result["error"])
        except Exception as e:
            log.error("Poll failed: %s", e)
            with _state_lock:
                _state["ok"] = False
                _state["status"] = "error"
                _state["error"] = str(e)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    log.info("Clawdbar daemon starting on port %s (API poll every %ss)", PORT, POLL_INTERVAL)
    log.info("Credentials: Keychain [%s] then %s", KEYCHAIN_SERVICE, CREDENTIALS_PATH)

    t = threading.Thread(target=poll_loop, daemon=True)
    t.start()

    server = HTTPServer((HOST, PORT), Handler)
    log.info("Serving at http://%s:%s (loopback only)", HOST, PORT)
    server.serve_forever()
