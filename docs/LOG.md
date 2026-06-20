# Session Log

Append-only. One entry per work session.

---

## 2026-06-20 — A SECOND, distinct cause: genuine token-expiry gaps → graceful stale UX

### "It stops showing usage again" — but NOT the 06-19 stale-Keychain bug
Investigated via the daemon logs (the `/debug` Claude Code log is irrelevant — this is a daemon issue). Two dark windows today:
- 00:35:05 → 00:50:15 (~15 min)
- 08:51:22 → 09:08:46 (~17 min)

Both logged "token genuinely expired" (`token_should_be_valid()` → False). **Zero `Relaunching` lines in the entire log history** — the 06-19 stale-Keychain relaunch fix has never even fired. So this is a *different* cause.

### Decisive evidence it's genuine expiry, not stale view
The Keychain item's last write (`mdat`) was **20260620 08:07:49 UTC = 09:07:49 local**, and the daemon's very next poll (09:08:46) succeeded. So during 08:51–09:07 there was **no valid token in the Keychain at all** — the previous access token had expired and Claude Code (idle, not being used) hadn't rotated it yet. A relaunch can't conjure a token that doesn't exist, so the daemon correctly waited — and *that waiting is the dark window*. The daemon has no way to refresh the token itself; it piggybacks entirely on Claude Code keeping the Keychain fresh.

### Decision
Self-refresh via `refreshToken` (present in Keychain) is the only thing that eliminates the gap, but that path was deleted twice (1010/429, never worked). Per the user's choice, we did **NOT** re-introduce self-refresh. Instead: **graceful stale UX** — stop blanking, show the last-known reading dimmed while waiting.

### Fix implemented (2026-06-20)
- **Daemon:** added `stale` to the `/usage` shape. New `_last_good_state` records the last successful payload; `_stale_payload(err)` returns it with `ok:false, stale:true` (preserving `session_pct`/`weekly_pct` and the **last-success** `last_updated`). Fixes a latent bug where the old merge only preserved %s on the *first* failure and zeroed them on every consecutive failure after.
- **Menu bar:** `_poll_usage()` returns `(pct, stale)`; the bar now shows the last % dimmed with a `*` (e.g. ` 92%*`) instead of ` --%`. Context-menu header notes "(stale — waiting for token refresh)".
- **PWA (`static/index.html`, data-binding layer only — engine untouched):** `fetchUsage` no longer throws stale data to the error overlay (`!data.ok && !data.stale`); `updateUI` renders a `#stale-banner` "Last updated HH:MM — waiting for Claude Code to refresh token".
- **Tests:** added `test_state_from_headers_not_stale`, `test_poll_and_update_serves_stale_after_failure` (incl. the consecutive-failure preservation), `test_poll_and_update_error_without_prior_good_is_not_stale`. 27 passing.
- Deployed by syncing the 3 changed files into `~/.clawdbar` + `launchctl kickstart -k` (no venv/dep change, so skipped the full `install.sh`). Verified live `/usage` serves `"stale": false` with no regression. The stale *rendering* wasn't triggered live (token currently valid); it's covered by unit tests + the payload contract.

---

## 2026-06-19 — THE ACTUAL ROOT CAUSE: stale Keychain in long-lived launchd daemon

### The "new root cause every day" was one root cause all along
Three sessions, three surface errors — Cloudflare 1010 (06-16), rate-limit 429 (06-17), and "token expired" again (06-19). They looked different but were the **same underlying bug** masked by a useless fallback.

### Decisive evidence gathered this session
1. Daemon's `/usage` reported `token expired`, but the **Keychain token was valid** — 8 hours of life left (`expiresAt` 06-20 00:30).
2. Running the **exact same probe from a shell** with that Keychain token returned **HTTP 200** with good rate-limit headers.
3. Keychain item `mdat` (modified) was **15:30 UTC today** — Claude Code rotated the token then. The daemon kept failing at 15:57, 16:14, 16:27–16:30, i.e. **well after** a fresh token was already in Keychain.
4. Only **one** Keychain item exists (no duplicate-entry confusion); a daemon-style `security` read from the shell returns the valid token.
5. Deployed code only *reads* `_token_cache` (never writes it) — so there is **no in-memory token cache** to blame.
6. **`launchctl kickstart -k io.clawdbar` (restart) fixed it instantly** — first poll after restart was `Poll OK`.
7. `clawdbar.err.log` has **2,276** `token expired` lines — the daemon has been dark for huge stretches, not occasionally.

### Root cause
The daemon is a long-lived **launchd** process. A long-running launchd agent gets a **stale view of the Keychain item**: when Claude Code rotates the OAuth access token (~every 8h), the daemon's freshly-spawned `security find-generic-password` subprocess keeps returning the **old** token to *that* launchd session. The API then returns 401/403, which the daemon mislabels as "token expired." It never self-heals. A brand-new process (restart) gets a fresh Keychain session and reads the current token correctly.

The 1010 and 429 errors from the prior two sessions were the **OAuth self-refresh fallback flailing** against `platform.claude.com` — a layer of noise on top of the real bug. That self-refresh has **never once succeeded** in any log; every real recovery was a fresh process reading the rotated token.

### Secondary bug (explains "manual refresh doesn't work")
The menubar **"Refresh Now"** item (`refreshNow_`) only re-fetches the daemon's cached `/usage` and reloads the webview. It does **not** ask the daemon to re-poll the API, so it can never recover a dark state — it just redisplays the same error. `_poll_session_pct()` reads `/usage`; there is no force-repoll path.

### Immediate action taken
Restarted the daemon (`launchctl kickstart -k`); usage is live again. This is a manual stopgap, not the fix.

### The real fix (IMPLEMENTED 2026-06-19)
Stop relying on a long-lived process to track a rotating credential:
- **Auto-relaunch:** `poll_and_update()` counts consecutive auth failures; after `MAX_AUTH_FAILURES` (3) it calls `_relaunch()` → `os._exit(1)`, and the plist's `KeepAlive` + `ThrottleInterval: 10` brings up a fresh process (fresh Keychain session). Verified: killing the daemon relaunches it in ~1 s.
- **Only relaunch when it'll help:** `token_should_be_valid()` checks the Keychain `expiresAt`. We relaunch *only* when the token still looks valid but is rejected (the stale-view signature). If genuinely expired, we just wait for Claude Code — no relaunch loop.
- **Real manual refresh:** new daemon `GET /refresh` forces an immediate re-poll and, on a valid-but-rejected token, relaunches right after responding. Menubar "Refresh Now" now calls it (was only re-reading the cached `/usage`).
- **Deleted** the OAuth self-refresh entirely (`try_refresh_oauth_token`, `_update_credentials_file`, `_extract_refresh_token`, `read_refresh_token_keychain`, OAuth constants, the dead `_token_cache`). It never once worked and only produced the 1010/429 noise.
- Tests: added coverage for `_extract_expires_at`, `refresh_state` (no-token / persistent-rejection raises / rotated-token retry), and `token_should_be_valid`. 24 passing.

---

## 2026-06-17 — Architectural fix: stop self-refreshing OAuth tokens

### Why the symptom kept recurring
Both the 1010 and 429 errors were symptoms of the same mistake: the daemon was calling `platform.claude.com/v1/oauth/token` to refresh its own access token. Reviewing the logs showed this approach **never worked** — every recovery (`Poll OK` with no preceding `OAuth token refreshed successfully`) was Claude Code updating Keychain independently. We were fighting a losing battle against an endpoint we had no business calling.

### Fix
Removed the OAuth refresh call from `refresh_state()`. On auth failure, the daemon now clears its in-memory token cache, re-reads Keychain/credentials (in case Claude Code just updated them), and if still expired, waits for the next poll. `try_refresh_oauth_token()` is kept in the codebase but marked as a diagnostic utility, not part of the poll loop.

### Expected behaviour going forward
When the token expires, `clawdbar.err.log` will show:
```
WARNING Token expired — waiting for Claude Code to refresh (run `claude` to speed this up)
ERROR Poll failed: token expired — run `claude login`
```
No 1010 or 429 errors. Recovery happens on the next poll after Claude Code runs.

---

## 2026-06-17 — Token refresh rate-limit 429 bug

### Symptom
Same "usage goes dark" pattern. `clawdbar.err.log` shows:
```
22:35:15 WARNING Token refresh HTTP 429: {"error":{"type":"rate_limit_error","message":"Rate limited. Please try again later."}}
```
repeating every 60 s. No `Retry-After` header in the response.

### Root cause
The 2026-06-16 User-Agent fix bypassed Cloudflare's bot-detection, but now requests reach the real OAuth rate limiter. With the poll backoff capped at 60 s, the daemon hits the refresh endpoint once per minute (~60/hour), triggering the rate limit. Recovery still only happens when Claude Code CLI refreshes Keychain.

### Fix
Added `_refresh_blocked_until` global. On HTTP 429, `try_refresh_oauth_token()` sets it to `now + max(Retry-After, 300)` (minimum 5-minute backoff). Subsequent calls return `None` immediately without touching the endpoint. Keychain re-reads still happen on every poll so a Claude Code refresh is picked up immediately.

### If it recurs
Check if the 429 is getting a non-zero `Retry-After` header that we're not reading correctly. Extend `MIN_REFRESH_BACKOFF` above 300 s if needed.

---

## 2026-06-16 — Token refresh Cloudflare 1010 bug

### Symptom
Usage data goes dark for extended periods (~30+ minutes). `clawdbar.err.log` shows a long run of:
```
23:04:09 INFO Access token rejected — attempting OAuth refresh
23:04:09 WARNING Token refresh HTTP 403: b'error code: 1010'
23:04:09 ERROR Poll failed: token expired — run `claude login`
```
Recovery happens only when the user actively uses Claude Code CLI (no user action needed in Clawdbar itself).

### Root cause
`try_refresh_oauth_token()` posted to `platform.claude.com/v1/oauth/token` with no `User-Agent` header. Cloudflare's bot-detection blocks headless requests, returning `HTTP 403: error code: 1010`. The API probe to `api.anthropic.com` was unaffected because it already sent `User-Agent: claude-code/2.1.5`.

Confirmation: recovery at 23:37 produced a `Poll OK` log with **no** preceding `OAuth token refreshed successfully` — meaning our daemon's refresh never worked; it was Claude Code CLI refreshing Keychain from its own session.

### Fix
Added `"User-Agent": API_HEADERS["User-Agent"]` to the refresh request headers in `daemon.py:try_refresh_oauth_token()`. The User-Agent reference is indirect so it tracks any future bumps to `API_HEADERS`.

### If it recurs
1. Check `clawdbar.err.log` for `error code: 1010`.
2. Verify the User-Agent in `API_HEADERS` matches the current Claude Code version (`claude --version`).
3. If the User-Agent is already correct, Cloudflare may have changed its policy — the next debugging step is to capture the full response headers from the failing refresh request.
