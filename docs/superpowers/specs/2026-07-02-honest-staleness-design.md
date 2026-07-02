# Honest Staleness + Instant Recovery (No OAuth Self-Refresh)

**Date:** 2026-07-02
**Status:** Approved for planning
**Scope:** `daemon/daemon.py`, `tests/test_daemon.py`, `docs/TROUBLESHOOTING.md`. No changes to the PWA animation engine or LaunchAgents.

---

## Problem

After the Mac sleeps overnight, the menu bar freezes on the last pre-sleep reading and the
number is **wrong**, not just stale: the 5h session window resets during sleep, so the frozen
"24%" no longer corresponds to any real window. Observed incident (2026-07-01 → 07-02 logs):

```
22:13  Idle — pausing probes (serving session 24%)
~sleep~
08:19  Active again → Auth failed → relaunch (reload Keychain) → Auth failed AGAIN
       → frozen on 24% for ~15h
23:11  User runs Claude Code → Claude Code refreshes token → Poll OK (17%) → recovered
```

### Root cause

The daemon is a **read-only consumer** of Claude Code's OAuth access token
(Keychain `Claude Code-credentials`). It performs no refresh-token exchange. While the Mac
sleeps, Claude Code isn't running, the access token (~8h lifetime) expires, and **nobody can
mint a new one**. The stale-Keychain-view relaunch trick cannot help — a fresh process reads
the same expired token. Recovery only happens when the user next runs Claude Code.

Three distinct failures compound:

1. **Wrong numbers** — the served stale reading outlives its own `session_reset_at`.
2. **Futile work** — on wake, the daemon burns its auth-failure strikes and a relaunch on a
   token that is genuinely expired, not merely stale-in-view.
3. **Slow recovery** — after Claude Code mints a fresh token, the daemon waits for the
   activity gate instead of probing immediately.

---

## Decision: the daemon will NOT refresh OAuth tokens itself

Evidence gathered 2026-07-02:

- Anthropic's OAuth refresh tokens are **rotating and single-use**
  ([openclaw #2036](https://github.com/openclaw/openclaw/issues/2036): "OAuth refresh
  tokens are single-use"; Claude Code's refresh invalidates any shared copy).
- Reusing a stale rotated token can **revoke the whole token family**
  ([claude-code #54443](https://github.com/anthropics/claude-code/issues/54443)),
  i.e. log the user out of Claude Code entirely.
- Anthropic has revoked refresh tokens for third-party usage
  ([community reports](https://www.answeroverflow.com/m/1470716228757753906)).

clawdbar and Claude Code would be two **uncoordinated writers of a single-use token**.
Claude Code will never honor a clawdbar-side lock, so the `invalid_grant` / family-revocation
race cannot be eliminated from our side. A stale menubar number is annoying; breaking the
user's Claude Code login is materially worse. Therefore: no self-refresh, ever, in this design.

### Ecosystem survey (2026-07-02) — nobody has solved self-refresh

| Project | Approach | Outcome |
|---|---|---|
| [CodexBar](https://github.com/steipete/CodexBar/issues/1287) | Reads same Keychain entry; tried delegating refresh to a spawned `claude` CLI subprocess | Open unresolved issue: CLI delegation "always times out"; even `claude -p "hi"` returned 401 instead of refreshing |
| [claude-usage](https://github.com/bishojbk/claude-usage) | Same Haiku-probe + headers architecture | Reads Keychain once at startup; no refresh handling at all |
| [Usage4Claude](https://github.com/f-is-h/usage4claude), [ClaudeMeter](https://github.com/eddmann/ClaudeMeter) | claude.ai `sessionKey` cookie (weeks–months lifetime) | Sidesteps expiry, but scrapes undocumented web endpoints (ToS-gray per ClaudeMeter's own README) |
| [openclaw](https://github.com/openclaw/openclaw/issues/2036) | The one project that self-refreshes a shared token | Source of the rotation race-condition bug above |

The only viable patterns in the wild are (a) live honestly with staleness and recover fast —
this design — or (b) switch credentials to the sessionKey cookie, rejected below.

---

## Goals

1. Never display a reading that is provably wrong (its window has reset).
2. Show an honest, distinct state while the token is expired and Claude Code is idle,
   instead of burning auth strikes and relaunches that cannot succeed.
3. Recover within ~1 poll cycle (≤ ~60s) of Claude Code minting a fresh token.
4. Preserve all hard constraints: loopback only, token never logged, minimal probes
   (this design *reduces* probe count), no new background processes.

## Non-goals

- OAuth refresh-token exchange or any write to the Keychain (see Decision).
- Changes to the PWA animation engine.
- Notifying the user to "go run Claude Code" (possible future follow-up; out of scope here).

---

## Design

Builds on top of the currently-uncommitted work (persisted last-good state,
`workspaceDidWake_` forced refresh, "Reconnect" menu item), which is complementary and
should land first as its own commit.

### 1. Expire stale readings (fixes "wrong numbers")

When constructing a stale or idle payload (`_stale_payload`, `_idle_payload`), compare `now`
against the last-good reading's absolute reset epochs:

- `now > session_reset_at` → serve `session_pct: 0` and clear `session_reset_min` /
  `session_reset_at`. A reset window genuinely starts at 0, so 0 is the best honest estimate;
  `stale: true` keeps the UI dimmed to signal "estimate, not measurement."
- Same rule for `weekly_reset_at` / `weekly_pct` (rarely elapses, same logic).
- Applies wherever the last-good state is consumed, including the state reloaded from
  `last_good_state.json` at startup — a persisted reading must not resurrect past its window.
- If `session_reset_at` is absent on an old persisted blob, fall back to current behavior
  (serve as-is, stale-dimmed).

**Rendering decision:** expired-window session renders as `0%` dimmed (stale), not `--%`.
`--%` remains reserved for "no data at all."

### 2. Token-expiry awareness (kills the futile relaunch)

Resurrect the currently-dead `_extract_expires_at()` into a `token_expires_at() -> float | None`
helper (Keychain first, credentials-file fallback — same order as `read_token()`).

New gate in `poll_and_update()`, before probing, when **not forced**:

- If `token_expires_at()` is in the past **and** `is_active()` is false **and** a last-good
  reading exists → do not probe, do not increment auth failures, do not relaunch. Serve the
  (window-expired-adjusted, per §1) last reading with a distinct error string, e.g.
  `"token expired — waiting for Claude Code"`, and a new field `token_state`.
- If the token is expired but Claude Code **is** active, probe normally — Claude Code is
  about to refresh it, and the existing 3-strikes → cooldown-gated relaunch machinery
  remains the correctness backstop for the *stale-Keychain-view* case (which `expiresAt`
  cannot distinguish from real expiry, per the lesson recorded at `daemon.py:575`).
- Forced refresh (`/refresh`, the menubar button) always probes, as today.

`/usage` gains `token_state: "ok" | "waiting_for_claude"`. The menubar/PWA reuse the
existing dimmed-stale rendering; no new UI machinery.

### 3. Credential-change watcher (instant recovery)

Once per poll cycle the daemon already wakes; add one cheap `security` read (no API probe):

- Track the last-seen `expiresAt` value. When it **changes** while we're in the
  `waiting_for_claude` (or stale-auth) state, Claude Code has just minted a fresh token →
  immediately run a real probe bypassing the idle gate (one-shot force).
- Also fold in the mtime of `~/.claude/.credentials.json` when that file exists, as a
  secondary change signal.
- Best-effort by design: if a stale Keychain view hides the change, behavior degrades to
  exactly today's (activity gate + relaunch backstop). The watcher only ever *accelerates*
  recovery; correctness never depends on it.

### 4. Failure handling

- All new reads (`token_expires_at`, watcher) are best-effort: any exception → log at
  debug/warning, fall back to current behavior. No new failure modes for `/usage`.
- No token material is ever logged; only the `expiresAt` timestamp may appear in logs.

---

## Testing

Pure-logic unit tests in the existing `tests/test_daemon.py` style (monkeypatched globals,
tmp-path isolation for persisted state):

1. Stale payload with `now > session_reset_at` serves `session_pct == 0`, clears reset
   fields, keeps `stale: true`; weekly analog; absent `session_reset_at` → unchanged payload.
2. Persisted last-good state reloaded past its window is window-expired at serve time.
3. `token_expires_at()` parsing: ms-epoch top-level, nested one-dict-deep, raw/garbage → None.
4. Gate matrix: (expired, idle) → waiting state, no auth-failure increment, no relaunch;
   (expired, active) → probes; (valid, idle) → existing idle path; forced → always probes.
5. Watcher: `expiresAt` change while waiting → exactly one forced probe; no change → no probe;
   read failure → no crash, no probe.

Manual verification: `bash install.sh`, then
`curl -s http://127.0.0.1:7337/usage | python3 -m json.tool` showing `token_state`, and a
simulated expiry (temporarily point `CLAWDBAR_CREDENTIALS` at a fixture with past `expiresAt`).

`docs/TROUBLESHOOTING.md`: document the new "waiting for Claude Code" state and its
recovery path ("run any Claude Code command; the bar recovers within a minute").

---

## Alternatives considered and rejected

1. **OAuth self-refresh + atomic Keychain write-back.** Correct under rotation *only if*
   clawdbar is the sole refresher at that moment — but Claude Code can refresh concurrently,
   and its in-memory copy going stale forces `/login` or worse (family revocation,
   [claude-code #54443](https://github.com/anthropics/claude-code/issues/54443)). Two
   uncoordinated writers of a single-use token; unfixable from one side. Also risks
   third-party revocation by Anthropic. Rejected.
2. **In-memory self-refresh (no write-back).** Strictly worse than #1: under rotation it
   *silently invalidates Claude Code's refresh token* on every clawdbar refresh. Rejected.
3. **Delegate refresh by spawning the `claude` CLI.** Empirically broken in CodexBar
   ([#1287](https://github.com/steipete/CodexBar/issues/1287)): REPL-mode subprocess times
   out; `claude -p "hi"` 401s instead of refreshing. Would also burn a real turn and
   open/keep-alive a 5h usage window. Rejected.
4. **Switch to the claude.ai `sessionKey` cookie** (Usage4Claude/ClaudeMeter pattern).
   Eliminates the expiry problem (weeks-long credential) but requires scraping undocumented
   claude.ai web endpoints — ToS-gray, fragile, and abandons this project's core design
   decision of official rate-limit headers (CLAUDE.md hard constraint #3). Rejected.
5. **Notify the user on expiry (macOS notification).** Doesn't fix wrongness or recovery
   latency by itself; may be a nice follow-up on top of `token_state`. Deferred, not rejected.

---

## Rollout

1. Commit the existing WIP (persist last-good, wake-refresh, Reconnect) as its own commit.
2. Implement §1–§4 with tests (TDD per implementation plan).
3. `bash install.sh` to stage + restart LaunchAgents; verify against a real sleep/wake cycle.
