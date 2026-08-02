"""Unit tests for the shipped LaunchAgent property lists.

Stdlib + pytest only — parses the repo's plist templates directly (the
REPLACE_USER placeholder is irrelevant to the keys under test).

Run: uv run --with pytest pytest -q
"""
import plistlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DAEMON_PLIST = ROOT / "io.clawdbar.plist"
MENUBAR_PLIST = ROOT / "io.clawdbar.menubar.plist"


def _load(path):
    with path.open("rb") as fh:
        return plistlib.load(fh)


# ---- menu bar: Quit must stay quit --------------------------------------

def test_menubar_keepalive_does_not_restart_on_clean_exit():
    """"Quit Clawdbar" calls NSApp.terminate_(), which exits 0.

    An unconditional `KeepAlive` makes launchd relaunch the app on *any*
    exit, so Quit looks like a reload. Gate the restart on SuccessfulExit
    so a deliberate quit sticks.
    """
    keepalive = _load(MENUBAR_PLIST)["KeepAlive"]
    assert isinstance(keepalive, dict), (
        "KeepAlive must be a dict, not an unconditional bool — "
        "otherwise launchd relaunches the app after Quit"
    )
    assert keepalive["SuccessfulExit"] is False


def test_menubar_keepalive_still_restarts_on_crash():
    """SuccessfulExit=False is the only key: a non-zero exit still restarts."""
    keepalive = _load(MENUBAR_PLIST)["KeepAlive"]
    assert set(keepalive) == {"SuccessfulExit"}


# ---- daemon: unattended, restarts unconditionally ------------------------

def test_daemon_keepalive_is_unconditional():
    """The daemon has no user-facing quit, so it should always come back."""
    assert _load(DAEMON_PLIST)["KeepAlive"] is True


# ---- both agents ---------------------------------------------------------

@pytest.mark.parametrize("path", [DAEMON_PLIST, MENUBAR_PLIST])
def test_agent_runs_at_load(path):
    assert _load(path)["RunAtLoad"] is True
