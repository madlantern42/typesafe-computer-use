"""A small macOS menu-bar front end that runs the computer-use loop in this app process.

Keeping capture and input in the signed app process gives macOS one stable identity to
grant Screen Recording and Accessibility to. The CLI remains available separately.
"""

from __future__ import annotations

import os
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path

import ApplicationServices as AS
import objc
import Quartz
from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSBackingStoreBuffered,
    NSButton,
    NSMakeRect,
    NSMenu,
    NSMenuItem,
    NSScrollView,
    NSStatusBar,
    NSTerminateCancel,
    NSTerminateNow,
    NSTextField,
    NSTextView,
    NSVariableStatusItemLength,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskTitled,
)
from Foundation import NSURL, NSBundle, NSObject, NSTimer, NSUserDefaults

from . import config, macos
from .actions import Context
from .runner import RunConfig, run
from .writer import make_writer

APP_NAME = "TypeSafe Computer Use"
MAX_STEPS = 20


def support_dir() -> Path:
    path = Path.home() / "Library" / "Application Support" / APP_NAME
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def config_path() -> Path:
    configured = NSBundle.mainBundle().objectForInfoDictionaryKey_("TypeSafeConfigPath")
    return Path(str(configured)) if configured else Path.cwd() / ".env"


def label(text: str, frame) -> NSTextField:
    view = NSTextField.labelWithString_(text)
    view.setFrame_(frame)
    return view


class MenuApp(NSObject):
    def init(self):
        self = objc.super(MenuApp, self).init()
        if self is None:
            return None
        self.worker: threading.Thread | None = None
        self.result: tuple[str, str] | None = None
        self.run_dir: Path | None = None
        self.quit_pending = False
        self.last_log = ""
        return self

    def applicationDidFinishLaunching_(self, _notification):
        os.umask(0o077)
        home = support_dir()
        if sys.stdout is None:
            sys.stdout = (home / "app.stdout.log").open("a", encoding="utf-8")
        if sys.stderr is None:
            sys.stderr = (home / "app.stderr.log").open("a", encoding="utf-8")
        NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)
        self.build_window()
        self.build_menu()
        self.refresh_(None)
        self.window.makeKeyAndOrderFront_(None)
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self.timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(0.5, self, "refresh:", None, True)

    def build_window(self):
        style = NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskMiniaturizable
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, 560, 370), style, NSBackingStoreBuffered, False
        )
        self.window.setTitle_(APP_NAME)
        self.window.setReleasedWhenClosed_(False)
        self.window.center()
        content = self.window.contentView()
        content.addSubview_(label("Goal", NSMakeRect(20, 320, 520, 20)))
        self.goal = NSTextField.alloc().initWithFrame_(NSMakeRect(20, 287, 520, 28))
        saved = NSUserDefaults.standardUserDefaults().stringForKey_("lastGoal") or ""
        self.goal.setStringValue_(saved)
        content.addSubview_(self.goal)

        self.start_button = NSButton.alloc().initWithFrame_(NSMakeRect(20, 245, 112, 32))
        self.start_button.setTitle_("Start")
        self.start_button.setTarget_(self)
        self.start_button.setAction_("startStop:")
        content.addSubview_(self.start_button)

        self.access_button = NSButton.alloc().initWithFrame_(NSMakeRect(140, 245, 178, 32))
        self.access_button.setTitle_("Request Permissions")
        self.access_button.setTarget_(self)
        self.access_button.setAction_("requestPermissions:")
        content.addSubview_(self.access_button)

        self.status = label("Ready", NSMakeRect(20, 216, 520, 20))
        content.addSubview_(self.status)
        content.addSubview_(label("Recent run output", NSMakeRect(20, 190, 520, 20)))

        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(20, 20, 520, 166))
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(2)
        self.log_view = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, 520, 166))
        self.log_view.setEditable_(False)
        scroll.setDocumentView_(self.log_view)
        content.addSubview_(scroll)

    def build_menu(self):
        self.status_item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
        self.menu = NSMenu.alloc().init()

        def item(title: str, action: str):
            entry = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, "")
            entry.setTarget_(self)
            self.menu.addItem_(entry)
            return entry

        item("Show TypeSafe", "showWindow:")
        self.menu.addItem_(NSMenuItem.separatorItem())
        self.menu_start = item("Start", "startStop:")
        self.menu_stop = item("Stop", "stopRun:")
        item("Open Run Folder", "openRunFolder:")
        self.menu.addItem_(NSMenuItem.separatorItem())
        item("Quit", "quitApp:")
        self.status_item.setMenu_(self.menu)

    def running(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def permissions(self) -> tuple[bool, bool]:
        return bool(Quartz.CGPreflightScreenCaptureAccess()), bool(AS.AXIsProcessTrusted())

    @objc.IBAction
    def showWindow_(self, _sender):
        self.window.makeKeyAndOrderFront_(None)
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)

    @objc.IBAction
    def requestPermissions_(self, _sender):
        screen, access = self.permissions()
        if not screen:
            Quartz.CGRequestScreenCaptureAccess()
        if not access:
            AS.AXIsProcessTrustedWithOptions({AS.kAXTrustedCheckOptionPrompt: True})
        self.refresh_(None)

    @objc.IBAction
    def startStop_(self, _sender):
        if self.running():
            self.stopRun_(None)
            return
        goal = str(self.goal.stringValue()).strip()
        if not goal:
            self.status.setStringValue_("Enter a goal before starting.")
            return
        screen, access = self.permissions()
        if not screen or not access:
            self.status.setStringValue_("Grant Screen Recording and Accessibility, then restart this app.")
            return
        config.load_dotenv(config_path())
        if not os.environ.get("TYPESAFE_API_KEY"):
            self.status.setStringValue_(f"TYPESAFE_API_KEY is missing from {config_path()}")
            return
        try:
            writer = make_writer()
            config.writer_vision()
        except ValueError as exc:
            self.status.setStringValue_(str(exc))
            return
        NSUserDefaults.standardUserDefaults().setObject_forKey_(goal, "lastGoal")
        macos.clear_stop()
        self.result = None
        self.run_dir = support_dir() / "runs" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        self.run_dir.mkdir(parents=True, mode=0o700)
        self.run_dir.chmod(0o700)

        def work():
            try:
                cfg = RunConfig(goal=goal, out=self.run_dir, act=True, steps=MAX_STEPS)

                def context(typesafe, history):
                    return Context(
                        goal=goal,
                        browser=config.browser(),
                        email=config.email(),
                        typesafe=typesafe,
                        writer=writer,
                        history=history,
                    )

                state = run(cfg, context)
                self.result = (state.outcome, "")
            except Exception:
                (self.run_dir / "app-error.log").write_text(traceback.format_exc(), encoding="utf-8")
                self.result = ("failed", "See app-error.log in the run folder")

        self.worker = threading.Thread(target=work, name="typesafe-computer-use", daemon=True)
        self.worker.start()
        self.refresh_(None)

    @objc.IBAction
    def stopRun_(self, _sender):
        if self.running():
            macos.request_stop()
            self.status.setStringValue_("Stopping before the next computer action…")

    @objc.IBAction
    def openRunFolder_(self, _sender):
        path = self.run_dir or support_dir() / "runs"
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        from AppKit import NSWorkspace

        NSWorkspace.sharedWorkspace().openURL_(NSURL.fileURLWithPath_(str(path)))

    @objc.IBAction
    def quitApp_(self, _sender):
        NSApplication.sharedApplication().terminate_(None)

    def applicationShouldTerminate_(self, _sender):
        if self.running():
            self.quit_pending = True
            self.stopRun_(None)
            return NSTerminateCancel
        return NSTerminateNow

    def refresh_(self, _timer):
        active = self.running()
        self.status_item.button().setTitle_("■ TypeSafe" if active else "▶ TypeSafe")
        self.start_button.setTitle_("Stop" if active else "Start")
        self.menu_start.setEnabled_(not active)
        self.menu_stop.setEnabled_(active)
        if active and not macos.stop_requested():
            self.status.setStringValue_("Running live on this Mac. Stop here or move the mouse to the top-left corner.")
        elif not active and self.result is not None:
            outcome, detail = self.result
            self.status.setStringValue_(f"Finished: {outcome}. {detail}".strip())
        elif not active and self.result is None:
            screen, access = self.permissions()
            if not screen or not access:
                missing = " and ".join(name for name, ok in (("Screen Recording", screen), ("Accessibility", access)) if not ok)
                self.status.setStringValue_(f"Permission needed: {missing}")
        if self.run_dir is not None:
            log = self.run_dir / "run.log"
            if log.exists():
                recent = "\n".join(log.read_text(encoding="utf-8").splitlines()[-18:])
                if recent != self.last_log:
                    self.log_view.setString_(recent)
                    self.last_log = recent
        if not active and self.quit_pending:
            NSApplication.sharedApplication().terminate_(None)


def main() -> None:
    app = NSApplication.sharedApplication()
    global _delegate
    _delegate = MenuApp.alloc().init()
    app.setDelegate_(_delegate)
    app.run()


if __name__ == "__main__":
    main()
