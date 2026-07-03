# Troubleshooting — the bar isn't showing

What to do when you open the laptop and the Clawd menu bar reading looks wrong or
missing. Work top-down; most cases are fixed by the first one or two steps.

## 0. Read the symbol first — it tells you which case you're in

| What the bar shows | What it means | Action |
|---|---|---|
| `5% ☾` (moon) | **Resting.** Claude Code has been idle, so the daemon paused API probes and is serving the last reading. This is normal, not a fault. | Nothing needed. Right-click → **Refresh Now** for a live number. |
| `5%*` (dimmed + star) | **Stale token.** The daemon's last probe was rejected (OAuth token expired/rotated) and it's serving the last good reading while it waits. See the recurring Keychain issue in LOG.md. | Open Claude Code briefly (it refreshes the token), or right-click → **Refresh Now**. Usually self-heals. |
| `--%` | The menu bar app is alive but got no data from the daemon — the daemon is likely down or not answering. | Go to step 2. |
| **No icon at all** | The menu bar app itself isn't running. | Go to step 3 (restart). |

After waking from sleep the bar refreshes itself within a few seconds (the app
listens for the macOS wake notification and forces a poll). Give it ~30s before
intervening.

## 1. Refresh Now — one click

Right-click the icon → **Refresh Now**. This forces the daemon to re-probe the
API right now (bypassing the idle gate) and, if it detects a stale Keychain view,
relaunches itself to read the live token. Clears most `%*` and `--%` cases.

## 2. Reconnect — restart the daemon from the menu

If Refresh Now doesn't help (e.g. the daemon process died and nothing is
answering on the port), right-click → **Reconnect (restart daemon)**. This
restarts the daemon LaunchAgent so a fresh process rebinds `127.0.0.1:7337` and
gets a fresh Keychain session, then repaints the bar. The bar shows `…` while it
works.

Equivalent from a terminal:

```bash
launchctl kickstart -k gui/$(id -u)/io.clawdbar
curl -s http://127.0.0.1:7337/usage | python3 -m json.tool   # confirm it's answering
```

## 3. Check which half is down

```bash
launchctl list | grep clawdbar    # expect BOTH io.clawdbar and io.clawdbar.menubar
curl -s http://127.0.0.1:7337/usage | python3 -m json.tool   # daemon answering?
```

- Missing `io.clawdbar.menubar` → the **menu bar app** is dead (no icon).
- `curl` hangs/fails → the **daemon** is dead (bar shows `--%`).
- `curl` returns `"stale": true` / `"ok": false` → it's the **token**; do step 1,
  or open Claude Code.

Logs, if you need detail:

```bash
tail -n 40 ~/Library/Logs/clawdbar.out.log            # daemon
tail -n 40 ~/Library/Logs/clawdbar-menubar.err.log    # menu bar app
```

## 4. Restart everything — the hammer

Reloads both LaunchAgents from the staged runtime copy:

```bash
bash ~/.clawdbar/install.sh
```

## Still dark after all of the above

It's almost always the OAuth token, not the app: the daemon can't read a valid
token because Claude Code's own refresh is failing upstream (Cloudflare / rate
limit — see `docs/LOG.md`). The bar recovers on its own once Claude Code writes a
fresh token to the Keychain, which happens the next time you actually use Claude
Code. Open Claude Code, run anything, and the bar should clear within a poll.
