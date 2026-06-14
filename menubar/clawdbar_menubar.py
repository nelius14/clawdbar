#!/usr/bin/env python3
"""
Clawdbar menu bar app (macOS).

- Shows a static pixel Clawd icon + session % in the menu bar.
- Clicking opens a popover containing a WKWebView that loads the full PWA
  (animations + both usage bars + both reset times) from the local daemon.
- Polls the daemon's /usage endpoint every 60s to keep the bar % fresh.

Requires the daemon (daemon.py) running locally on 127.0.0.1:7337.
The daemon polls Anthropic's rate-limit headers for official quota %.

Dependencies: pyobjc (Cocoa, WebKit, Quartz)
"""

import base64
import fcntl
import json
import os
import sys
import urllib.request
from pathlib import Path

LOCK_FILE = Path.home() / ".clawdbar" / "menubar.lock"


def _acquire_lock():
    """Acquire an exclusive lock so only one instance runs. Returns the open file
    handle (must stay open for the lock to hold), or None if already locked."""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    f = open(LOCK_FILE, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        f.write(str(os.getpid()))
        f.flush()
        return f
    except OSError:
        f.close()
        return None

import objc
from Cocoa import (
    NSApplication, NSApp, NSObject, NSStatusBar, NSVariableStatusItemLength,
    NSImage, NSMakeRect, NSMakeSize, NSPopover, NSPopoverBehaviorTransient,
    NSViewController, NSView, NSTimer, NSImageLeft,
    NSApplicationActivationPolicyAccessory, NSRunLoop, NSDefaultRunLoopMode,
    NSMenu, NSMenuItem,
)

# NSEventMask constants (numeric to avoid PyObjC version issues)
_MASK_LEFT_MOUSE_DOWN  = 1 << 1   # NSEventMaskLeftMouseDown  = 2
_MASK_RIGHT_MOUSE_DOWN = 1 << 3   # NSEventMaskRightMouseDown = 8
from WebKit import WKWebView, WKWebViewConfiguration, WKUserScript, WKUserContentController
from Foundation import NSURL, NSURLRequest

# ── config ───────────────────────────────────────────────────────────
DAEMON_HOST = "127.0.0.1"     # loopback IP (avoids ATS; localhost would need a config key)
DAEMON_PORT = 7337
DAEMON_BASE = f"http://{DAEMON_HOST}:{DAEMON_PORT}"
USAGE_URL = f"{DAEMON_BASE}/usage"
POLL_SECONDS = 60.0
POPOVER_W = 320
POPOVER_H = 260  # tight-fit: header (40px) + 2 cards, no footer

ICON_PATH = Path(__file__).parent.parent / "static" / "clawd_icon.png"

# ── font injection ────────────────────────────────────────────────────
def _font_face_css() -> str:
    """Build @font-face CSS with fonts embedded as base64 data URIs."""
    fonts = [
        ("Anthropic Sans",  Path.home() / "Library/Fonts/AnthropicSans-Romans-Variable-25x258.ttf"),
        ("Anthropic Serif", Path.home() / "Library/Fonts/AnthropicSerif-Romans-Variable-25x258.ttf"),
    ]
    css = ""
    for family, path in fonts:
        if path.exists():
            b64 = base64.b64encode(path.read_bytes()).decode("ascii")
            css += (
                f"@font-face {{"
                f"font-family:'{family}';"
                f"src:url('data:font/truetype;base64,{b64}') format('truetype');"
                f"}}"
            )
    return css

FONT_CSS = _font_face_css()


class _NavDelegate(NSObject):
    """Resizes the popover and webview to fit content after the page finishes loading."""

    def initWithPopover_webview_width_(self, popover, webview, width):
        self = objc.super(_NavDelegate, self).init()
        if self is None:
            return None
        self._popover = popover
        self._webview = webview
        self._width = width
        return self

    def webView_didFinishNavigation_(self, webview, navigation):
        popover = self._popover
        wv = self._webview
        width = self._width

        def _resize(result, error):
            if result is not None:
                h = float(result)
                popover.setContentSize_(NSMakeSize(width, h))
                wv.setFrame_(NSMakeRect(0, 0, width, h))

        webview.evaluateJavaScript_completionHandler_(
            "document.getElementById('app').scrollHeight",
            _resize,
        )


class ClawdbarDelegate(NSObject):

    def applicationDidFinishLaunching_(self, notification):
        # Accessory app — no Dock icon, menu bar only
        NSApp.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

        # ── status item ──
        self.statusItem = NSStatusBar.systemStatusBar().statusItemWithLength_(
            NSVariableStatusItemLength
        )
        button = self.statusItem.button()

        # Clawd icon
        if ICON_PATH.exists():
            img = NSImage.alloc().initWithContentsOfFile_(str(ICON_PATH))
            # Render at a crisp small size in the bar
            img.setSize_(NSMakeSize(17, 15))
            img.setTemplate_(False)  # keep the orange; set True for monochrome auto-tint
            button.setImage_(img)
            button.setImagePosition_(NSImageLeft)

        button.setTitle_(" --%")
        button.setTarget_(self)
        button.setAction_(objc.selector(self.togglePopover_, signature=b"v@:@"))
        # Enable right-click on the status button
        button.sendActionOn_(_MASK_LEFT_MOUSE_DOWN | _MASK_RIGHT_MOUSE_DOWN)

        # ── popover with WebView ──
        self.popover = NSPopover.alloc().init()
        self.popover.setContentSize_(NSMakeSize(POPOVER_W, POPOVER_H))
        self.popover.setBehavior_(NSPopoverBehaviorTransient)

        vc = NSViewController.alloc().init()
        container = NSView.alloc().initWithFrame_(
            NSMakeRect(0, 0, POPOVER_W, POPOVER_H)
        )

        config = WKWebViewConfiguration.alloc().init()
        if FONT_CSS:
            script = WKUserScript.alloc().initWithSource_injectionTime_forMainFrameOnly_(
                f"(function(){{var s=document.createElement('style');s.textContent={repr(FONT_CSS)};document.head.appendChild(s);}})();",
                0,   # WKUserScriptInjectionTimeAtDocumentStart
                True
            )
            config.userContentController().addUserScript_(script)

        self.webview = WKWebView.alloc().initWithFrame_configuration_(
            NSMakeRect(0, 0, POPOVER_W, POPOVER_H), config
        )
        self._nav_delegate = _NavDelegate.alloc().initWithPopover_webview_width_(self.popover, self.webview, POPOVER_W)
        self.webview.setNavigationDelegate_(self._nav_delegate)

        req = NSURLRequest.requestWithURL_(NSURL.URLWithString_(DAEMON_BASE + "/"))
        self.webview.loadRequest_(req)
        container.addSubview_(self.webview)
        vc.setView_(container)
        self.popover.setContentViewController_(vc)

        # ── poll timer for the bar title ──
        self.updateTitle_(None)  # immediate first poll
        self.timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            POLL_SECONDS, self,
            objc.selector(self.updateTitle_, signature=b"v@:@"),
            None, True
        )
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.timer, NSDefaultRunLoopMode)

    # ── actions ──
    def togglePopover_(self, sender):
        event = NSApp.currentEvent()
        # Right-click (type 3 = NSEventTypeRightMouseDown) → context menu
        if event and event.type() == 3:
            self._showContextMenu()
            return

        button = self.statusItem.button()
        if self.popover.isShown():
            self.popover.performClose_(sender)
        else:
            # Reload so the popover always shows fresh state
            self.webview.reload_(None)
            from Cocoa import NSRectEdgeMinY
            self.popover.showRelativeToRect_ofView_preferredEdge_(
                button.bounds(), button, NSRectEdgeMinY
            )
            NSApp.activateIgnoringOtherApps_(True)

    def _showContextMenu(self):
        menu = NSMenu.alloc().init()
        menu.setAutoenablesItems_(False)

        # Header: current usage (disabled label)
        pct = self._poll_session_pct()
        label = f"Session: {pct}% used" if pct is not None else "Session: -- (no data)"
        header = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(label, None, "")
        header.setEnabled_(False)
        menu.addItem_(header)

        menu.addItem_(NSMenuItem.separatorItem())

        # Refresh Now
        refresh = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Refresh Now", objc.selector(self.refreshNow_, signature=b"v@:@"), ""
        )
        refresh.setTarget_(self)
        menu.addItem_(refresh)

        menu.addItem_(NSMenuItem.separatorItem())

        # Quit
        quit_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Quit Clawdbar", objc.selector(self.quit_, signature=b"v@:@"), "q"
        )
        quit_item.setTarget_(self)
        menu.addItem_(quit_item)

        self.statusItem.popUpStatusItemMenu_(menu)

    def refreshNow_(self, sender):
        self.updateTitle_(None)
        if self.popover.isShown():
            self.webview.reload_(None)

    def quit_(self, sender):
        NSApp.terminate_(sender)

    def updateTitle_(self, timer):
        pct = self._poll_session_pct()
        button = self.statusItem.button()
        if pct is None:
            button.setTitle_(" --%")
        else:
            button.setTitle_(f" {pct}%")

    def _poll_session_pct(self):
        try:
            with urllib.request.urlopen(USAGE_URL, timeout=8) as resp:
                data = json.loads(resp.read().decode())
            if not data.get("ok"):
                return None
            return data.get("session_pct", 0)
        except Exception:
            return None


def main():
    lock = _acquire_lock()
    if lock is None:
        print("Clawdbar menubar already running — exiting.", file=sys.stderr)
        sys.exit(0)
    # Keep `lock` alive for the process lifetime so the flock holds.
    app = NSApplication.sharedApplication()
    delegate = ClawdbarDelegate.alloc().init()
    app.setDelegate_(delegate)
    app.run()


if __name__ == "__main__":
    main()
