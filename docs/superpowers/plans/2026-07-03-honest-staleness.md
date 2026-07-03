# Honest Staleness + Instant Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the clawdbar daemon stop showing wrong/frozen usage numbers after the Mac sleeps, by expiring readings past their reset windows, serving an honest "waiting for Claude Code" state instead of futile relaunches, and recovering the instant Claude Code refreshes the OAuth token — without ever refreshing the token itself.

**Architecture:** The daemon (`daemon/daemon.py`) stays a read-only consumer of Claude Code's Keychain OAuth token (no refresh-token exchange — see the spec's Decision section). Three additive changes: (1) a pure `_expire_windows()` helper that zeros a stale reading's `session_pct`/`weekly_pct` once `now` passes its reset epoch, wired into the stale/idle payload builders; (2) a `token_expires_at()` reader + a `waiting_for_claude` state so an expired-token-while-idle no longer counts auth strikes or relaunches; (3) a per-cycle credential-change watcher that forces one probe when Claude Code writes a new token.

**Tech Stack:** Python 3 stdlib only (no new deps; `uv` venv). Tests via `pytest`. Process manager: launchd (unchanged).

## Global Constraints

- **Loopback only.** Daemon binds `127.0.0.1:7337`. Never bind `0.0.0.0`.
- **Token stays local.** Read OAuth from Keychain/credentials file; never log token material. Only the `expiresAt` timestamp (an integer) may appear in logs.
- **No OAuth self-refresh, no Keychain writes.** The daemon never exchanges the refresh token and never mutates `Claude Code-credentials`.
- **Minimal probes.** One tiny Haiku request per poll at most; the credential watcher uses a `security` read only (no API call). This change must not increase probe frequency.
- **No new background processes.** Work inside the existing daemon poll loop.
- **Preserve the PWA animation engine.** `static/index.html` is untouched; `/usage` gains one field (`token_state`) that the existing UI can ignore.
- **Best-effort everywhere.** Every new Keychain/file read is wrapped so any failure logs and falls back to today's behavior; `/usage` must never gain a new failure mode.
- **Tests:** `uv run --with pytest pytest -q` (run from repo root). Commit only the daemon + test + doc files touched.

---

### Task 1: Expire stale readings past their reset window

**Files:**
- Modify: `daemon/daemon.py` (add `_expire_windows` after `state_from_headers`, ~line 328; call it inside `_stale_payload` ~line 414 and `_idle_payload` ~line 427)
- Test: `tests/test_daemon.py`

**Interfaces:**
- Consumes: the `/usage` payload dict shape (`session_pct`, `weekly_pct`, `session_reset_at`, `weekly_reset_at`, `session_reset_min`, `weekly_reset_min`) from `state_from_headers`.
- Produces: `_expire_windows(state: dict, now: float | None = None) -> dict` — returns a copy with `session_pct` set to `0` and `session_reset_min`/`session_reset_at` set to `None` when `now` is at/after `session_reset_at`; same for the weekly pair; unchanged when the reset epoch is absent or still in the future.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_daemon.py` (after the existing `state_from_headers` tests, ~line 193):

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --with pytest pytest tests/test_daemon.py -q -k expire_windows`
Expected: FAIL — `AttributeError: module 'daemon' has no attribute '_expire_windows'`

- [ ] **Step 3: Implement `_expire_windows`**

Insert into `daemon/daemon.py` immediately after `state_from_headers` (after line 327, before `poll_api`):

```python
def _expire_windows(state: dict, now: float | None = None) -> dict:
    """Zero a stale reading whose reset window has already elapsed.

    A served stale/idle reading can outlive its own window (e.g. the 5h session
    resets while the Mac sleeps), leaving the bar on a number that is no longer
    just old but *wrong*. When `now` is at/after an absolute reset epoch, the
    window has rolled over and genuinely starts at 0 — so zero the pct and clear
    that window's reset fields. Absent reset epochs are left untouched (we can't
    judge). Returns a copy; never mutates the input.
    """
    if now is None:
        now = time.time()
    out = dict(state)
    for at_key, min_key, pct_key in (
        ("session_reset_at", "session_reset_min", "session_pct"),
        ("weekly_reset_at", "weekly_reset_min", "weekly_pct"),
    ):
        at = state.get(at_key)
        if at is not None and now >= at:
            out[pct_key] = 0
            out[min_key] = None
            out[at_key] = None
    return out
```

- [ ] **Step 4: Wire `_expire_windows` into the stale and idle payloads**

In `daemon/daemon.py`, change `_stale_payload` (currently line 424) from:

```python
    if _last_good_state is None:
        return _error_state(err)
    return {**_last_good_state, "ok": False, "stale": True, "error": err}
```

to:

```python
    if _last_good_state is None:
        return _error_state(err)
    return _expire_windows({**_last_good_state, "ok": False, "stale": True, "error": err})
```

And change the final line of `_idle_payload` (currently line 438) from:

```python
    return {**_last_good_state, "ok": True, "stale": False, "idle": True, "error": None}
```

to:

```python
    return _expire_windows({**_last_good_state, "ok": True, "stale": False, "idle": True, "error": None})
```

- [ ] **Step 5: Run the full suite to verify pass and no regressions**

Run: `uv run --with pytest pytest -q`
Expected: PASS — all previous tests plus the 4 new ones (46 passed).

- [ ] **Step 6: Commit**

```bash
git add daemon/daemon.py tests/test_daemon.py
git commit -m "$(cat <<'EOF'
Expire stale readings past their reset window

A served stale/idle reading could outlive its own reset epoch (the 5h session
resets while the Mac sleeps), showing a number that is wrong, not just old.
_expire_windows zeroes the pct and clears the reset fields once now passes the
absolute reset epoch; wired into _stale_payload and _idle_payload.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Read the token's expiry timestamp

**Files:**
- Modify: `daemon/daemon.py` (add `token_expires_at` + `_token_expired` after `read_token`, ~line 240)
- Test: `tests/test_daemon.py`

**Interfaces:**
- Consumes: existing `_read_keychain_blob() -> str | None`, `CREDENTIALS_PATH`, and the pure `_extract_expires_at(blob) -> float | None` (line 143).
- Produces:
  - `token_expires_at() -> float | None` — the OAuth token's `expiresAt` in unix seconds, read from Keychain first then the credentials file; `None` if neither yields one.
  - `_token_expired(now: float | None = None) -> bool` — `True` only when `token_expires_at()` returns a value that is at/before `now`. Unknown expiry (`None`) → `False` (never suppress work on missing data).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_daemon.py` (after the `_extract_expires_at` tests, or at end of file):

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --with pytest pytest tests/test_daemon.py -q -k "token_expires_at or token_expired"`
Expected: FAIL — `AttributeError: module 'daemon' has no attribute 'token_expires_at'`

- [ ] **Step 3: Implement `token_expires_at` and `_token_expired`**

Insert into `daemon/daemon.py` immediately after `read_token` (after its `return read_token_file()`, ~line 221):

```python
def token_expires_at() -> float | None:
    """OAuth token expiry (unix seconds) from Keychain, then the file. None if absent.

    Same source order as read_token(). Best-effort and read-only: a stale
    Keychain view can return a stale expiresAt, so callers use this only to
    *suppress* futile work while idle, never as a correctness guarantee (the
    stale-view relaunch machinery remains the backstop). Never logs the token.
    """
    blob = _read_keychain_blob()
    exp = _extract_expires_at(blob) if blob else None
    if exp is not None:
        return exp
    try:
        raw = CREDENTIALS_PATH.read_text(encoding="utf-8")
    except OSError:
        return None
    return _extract_expires_at(raw)


def _token_expired(now: float | None = None) -> bool:
    """True only when a known expiry is at/before now. Unknown expiry → False."""
    if now is None:
        now = time.time()
    exp = token_expires_at()
    return exp is not None and exp <= now
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --with pytest pytest tests/test_daemon.py -q -k "token_expires_at or token_expired"`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add daemon/daemon.py tests/test_daemon.py
git commit -m "$(cat <<'EOF'
Read OAuth token expiry (Keychain then file)

token_expires_at() reuses the dead _extract_expires_at parser; _token_expired()
reports a past expiry only when known (unknown → False, never suppress work on
missing data). Read-only, never logs token material.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Serve a "waiting for Claude Code" state instead of futile relaunches

**Files:**
- Modify: `daemon/daemon.py` (add `"token_state"` to `_state` ~line 95, `_error_state` ~line 360, and `state_from_headers` return ~line 313; add `_waiting_payload`; branch the idle serve in `poll_and_update` ~line 622; gate the two stale-view relaunch call sites on `is_active()` — `do_GET` `/refresh` ~line 523 and the auth-failure handler ~line 648)
- Modify: `docs/TROUBLESHOOTING.md` (document the new state)
- Test: `tests/test_daemon.py`

**Interfaces:**
- Consumes: `_token_expired` (Task 2), `_expire_windows` via `_stale_payload` (Task 1), existing `is_active`, `_last_good_state`, `_idle_payload`, `_should_relaunch_for_stale_view`.
- Produces:
  - Every payload carries `token_state: "ok" | "waiting_for_claude"`.
  - `_waiting_payload() -> dict` — a stale payload (`ok: False`, `stale: True`) with `token_state: "waiting_for_claude"` and `error: "token expired — waiting for Claude Code"`.
  - Behavior: while idle with an expired token, `poll_and_update` serves the waiting payload, does not increment auth failures, and does not relaunch. The stale-view relaunch fires only while `is_active()`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_daemon.py` (near the other `poll_and_update` tests, ~line 250):

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --with pytest pytest tests/test_daemon.py -q -k "waiting or relaunch"`
Expected: FAIL — `KeyError: 'token_state'` (and the idle-expired test probes because the branch doesn't exist yet).

- [ ] **Step 3: Add the `token_state` field to the three payload sources**

In `daemon/daemon.py`, add `"token_state": "ok",` to the `_state` initializer (inside the dict at line 95, e.g. after `"idle": False,`):

```python
    "idle": False,
    "token_state": "ok",
}
```

Add the same key to the `_error_state` return dict (after `"idle": False,` ~line 374):

```python
        "idle": False,
        "token_state": "ok",
    }
```

Add it to the `state_from_headers` return dict (after `"idle": False,` ~line 326):

```python
        "idle": False,
        "token_state": "ok",
    }
```

- [ ] **Step 4: Add `_waiting_payload`**

Insert into `daemon/daemon.py` immediately after `_idle_payload` (after line 438):

```python
def _waiting_payload() -> dict:
    """Stale payload for the expired-token-while-idle case.

    Distinct from _idle_payload (normal 'resting' moon): the token is genuinely
    expired and only Claude Code can mint a new one, so we surface that honestly
    instead of pretending to rest. Runs through _expire_windows via _stale_payload
    so the number can't be wrong either.
    """
    payload = _stale_payload("token expired — waiting for Claude Code")
    payload["token_state"] = "waiting_for_claude"
    return payload
```

- [ ] **Step 5: Branch the idle serve in `poll_and_update`**

In `poll_and_update` (line 622), change:

```python
    if not force and not is_active() and _last_good_state is not None:
        with _auth_lock:
            _consecutive_auth_failures = 0
        payload = _idle_payload()
```

to:

```python
    if not force and not is_active() and _last_good_state is not None:
        with _auth_lock:
            _consecutive_auth_failures = 0
        payload = _waiting_payload() if _token_expired() else _idle_payload()
```

- [ ] **Step 6: Gate the auth-failure relaunch on `is_active()`**

In the auth-failure handler (line 648), change:

```python
        relaunching = n >= MAX_AUTH_FAILURES and _should_relaunch_for_stale_view()
```

to:

```python
        # Only a *fresh* token this process can't yet see justifies a relaunch —
        # that's only possible while Claude Code is active. Expired + idle means
        # no newer token exists, so serve waiting instead of thrashing.
        relaunching = (n >= MAX_AUTH_FAILURES and is_active()
                       and _should_relaunch_for_stale_view())
```

And change the payload built in that handler (currently `payload = _stale_payload(err)`, ~line 652) to reflect the idle case:

```python
        payload = _stale_payload(err) if is_active() else _waiting_payload()
```

- [ ] **Step 7: Gate the `/refresh` handler relaunch on `is_active()`**

In `do_GET`, the `/refresh` branch (line 523), change:

```python
            if not result.get("ok") and _should_relaunch_for_stale_view():
```

to:

```python
            if not result.get("ok") and is_active() and _should_relaunch_for_stale_view():
```

- [ ] **Step 8: Run the waiting/relaunch tests, then the full suite**

Run: `uv run --with pytest pytest tests/test_daemon.py -q -k "waiting or relaunch"`
Expected: PASS (4 passed).

Run: `uv run --with pytest pytest -q`
Expected: PASS — full suite green. If `test_stale_reading_survives_relaunch` (the existing WIP test) now fails on `is_active`, it already monkeypatches `is_active` to `True`, so it stays green; do not modify it.

- [ ] **Step 9: Document the new state in TROUBLESHOOTING.md**

In `docs/TROUBLESHOOTING.md`, update the symbol table (section 0) — replace the `5%*` row's meaning/action with the expired-token nuance and add the waiting note. Change the row:

```markdown
| `5%*` (dimmed + star) | **Stale token.** The daemon's last probe was rejected (OAuth token expired/rotated) and it's serving the last good reading while it waits. See the recurring Keychain issue in LOG.md. | Open Claude Code briefly (it refreshes the token), or right-click → **Refresh Now**. Usually self-heals. |
```

to:

```markdown
| `5%*` (dimmed + star) | **Waiting for Claude Code.** The OAuth token expired (usually overnight — it lasts ~8h and nothing refreshes it while the Mac sleeps) and only Claude Code can mint a new one. The daemon serves the last reading, dimmed; if its window has since reset it shows `0%*`. `/usage` reports `"token_state": "waiting_for_claude"`. | Run any Claude Code command — it refreshes the token, and the bar recovers within a minute (the daemon watches the credential and re-probes the moment it changes). No relaunch needed. |
```

- [ ] **Step 10: Commit**

```bash
git add daemon/daemon.py tests/test_daemon.py docs/TROUBLESHOOTING.md
git commit -m "$(cat <<'EOF'
Serve a waiting-for-Claude state; stop futile relaunches

When the token is expired while Claude Code is idle, no fresh token can exist,
so a relaunch can't help. poll_and_update now serves a distinct
token_state="waiting_for_claude" payload, skips the auth-strike/relaunch path,
and gates the stale-view relaunch (both the /refresh handler and the auth
handler) on is_active(). Adds token_state to every /usage payload.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Recover instantly when Claude Code refreshes the token

**Files:**
- Modify: `daemon/daemon.py` (add `_last_seen_expires_at` global + `_credential_changed`; call it in `poll_loop` ~line 683)
- Test: `tests/test_daemon.py`

**Interfaces:**
- Consumes: `token_expires_at` (Task 2), `_state` / `_state_lock`, `poll_and_update`.
- Produces: `_credential_changed() -> bool` — `True` the first time `token_expires_at()` returns a value different from the previous call (i.e. Claude Code wrote a new token); seeds silently on the first call (returns `False` when there's no prior value); read failure → `False`. Used by `poll_loop` to force one immediate probe when the current served state is not live (`stale` or `waiting_for_claude`).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_daemon.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --with pytest pytest tests/test_daemon.py -q -k credential_changed`
Expected: FAIL — `AttributeError: module 'daemon' has no attribute '_credential_changed'`

- [ ] **Step 3: Implement the global and `_credential_changed`**

In `daemon/daemon.py`, near the other poll globals (after `_was_idle = False`, ~line 555), add:

```python
# Last-seen token expiry, for detecting a Claude Code refresh mid-outage.
_last_seen_expires_at: float | None = None
```

Then add the function (near `poll_loop`, before it):

```python
def _credential_changed() -> bool:
    """True once when the token's expiresAt changes (Claude Code minted a new one).

    Seeds silently on the first call. Best-effort: any read error returns False so
    the poll loop is never disrupted. Lets us re-probe the instant the credential
    is refreshed instead of waiting for the activity gate.
    """
    global _last_seen_expires_at
    try:
        exp = token_expires_at()
    except Exception as e:  # best-effort watcher, never crash the loop
        log.debug("Credential-change check failed: %s", e)
        return False
    prev, _last_seen_expires_at = _last_seen_expires_at, exp
    return prev is not None and exp is not None and exp != prev
```

- [ ] **Step 4: Wire it into `poll_loop`**

In `poll_loop` (line 683), at the top of the `while True:` body, before the `try:` that calls `poll_and_update()`, insert the watcher so a credential change forces an immediate probe when the served state isn't live:

```python
    while True:
        with _state_lock:
            not_live = _state.get("stale") or _state.get("token_state") == "waiting_for_claude"
        if not_live and _credential_changed():
            log.info("Credential refreshed — re-probing now")
            try:
                poll_and_update(force=True)
            except Exception as e:
                log.error("Forced re-probe after credential change failed: %s", e)
        try:
            result = poll_and_update()
        except Exception as e:
```

(The existing `try/except`, backoff, and `time.sleep` below are unchanged.)

- [ ] **Step 5: Run the tests, then the full suite**

Run: `uv run --with pytest pytest tests/test_daemon.py -q -k credential_changed`
Expected: PASS (2 passed).

Run: `uv run --with pytest pytest -q`
Expected: PASS — full suite green.

- [ ] **Step 6: Commit**

```bash
git add daemon/daemon.py tests/test_daemon.py
git commit -m "$(cat <<'EOF'
Re-probe instantly when Claude Code refreshes the token

_credential_changed() tracks the token's expiresAt across poll cycles; when it
changes while the served state is stale/waiting, poll_loop forces an immediate
probe so the bar recovers within a minute instead of waiting for the activity
gate. Best-effort — any read error is swallowed so the loop never stalls.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: End-to-end verification against a real credential

**Files:** none (verification only; may append one clarifying line to `docs/TROUBLESHOOTING.md` if a gap is found)

**Interfaces:** none.

- [ ] **Step 1: Full suite green**

Run: `uv run --with pytest pytest -q`
Expected: PASS — all tasks' tests plus the originals (≈58 passed).

- [ ] **Step 2: Install and confirm the daemon answers with `token_state`**

Run: `bash install.sh`
Then: `curl -s http://127.0.0.1:7337/usage | python3 -m json.tool`
Expected: JSON includes `"token_state": "ok"` (assuming a valid token), plus the usual `session_pct` etc.

- [ ] **Step 3: Simulate an expired token and confirm the waiting state**

Create a fixture credential with a past `expiresAt` and point the daemon at it via the documented env override, then restart and read `/usage`:

```bash
python3 - <<'PY'
import json, pathlib, time
p = pathlib.Path.home() / ".clawdbar" / "expired-fixture.json"
p.write_text(json.dumps({"claudeAiOauth": {
    "accessToken": "expired-fixture-token",
    "expiresAt": int((time.time() - 3600) * 1000),  # 1h in the past, ms
}}))
print(p)
PY
CLAWDBAR_CREDENTIALS="$HOME/.clawdbar/expired-fixture.json" \
  launchctl setenv CLAWDBAR_CREDENTIALS "$HOME/.clawdbar/expired-fixture.json"
launchctl kickstart -k gui/$(id -u)/io.clawdbar
sleep 3
curl -s http://127.0.0.1:7337/usage | python3 -m json.tool
```

Expected (while Claude Code is idle): `"token_state": "waiting_for_claude"`, `"stale": true`, and — if the last-good window had elapsed — `"session_pct": 0`. The daemon log (`tail -n 20 ~/Library/Logs/clawdbar.out.log`) should show **no** "Relaunching daemon" line for this expired-idle case.

- [ ] **Step 4: Restore the real credential**

```bash
launchctl unsetenv CLAWDBAR_CREDENTIALS
rm -f "$HOME/.clawdbar/expired-fixture.json"
launchctl kickstart -k gui/$(id -u)/io.clawdbar
sleep 3
curl -s http://127.0.0.1:7337/usage | python3 -m json.tool   # back to token_state: ok
```

Expected: `"token_state": "ok"` again. No commit for this task unless a doc gap was found in Step 3.
```
