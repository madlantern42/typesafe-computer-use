"""Exercise the GUI's voice/run boundary without creating windows or accessing devices."""

import sys
from types import SimpleNamespace

import pytest

if sys.platform != "darwin":
    pytest.skip("AppKit front end is macOS-only", allow_module_level=True)

from typesafe_computer_use import macos_app
from typesafe_computer_use.voice import VoiceSnapshot


class Control:
    def __init__(self, value=""):
        self.value = value
        self.enabled = True
        self.hidden = False
        self.editable = True

    def setStringValue_(self, value):
        self.value = value

    def stringValue(self):
        return self.value

    def setTitle_(self, value):
        self.value = value

    def setEnabled_(self, value):
        self.enabled = value

    def setHidden_(self, value):
        self.hidden = value

    def setEditable_(self, value):
        self.editable = value


class Session:
    def __init__(self, phase="listening", text="new partial", message="Listening"):
        self.state = VoiceSnapshot(phase=phase, text=text, message=message)
        self.cancelled = False
        self.finished = False

    def snapshot(self):
        return self.state

    def cancel(self):
        self.cancelled = True
        self.state = VoiceSnapshot(phase="cancelling", text="", message="Cancelling")

    def finish(self):
        self.finished = True
        self.state = VoiceSnapshot(phase="finishing", text=self.state.text, message="Finishing")


@pytest.fixture
def app(monkeypatch):
    # NSObject allocation has no UI/device side effects; build_window/main are never called.
    result = macos_app.MenuApp.alloc().init()
    for name in ("goal", "status", "start_button", "menu_start", "menu_stop", "menu_voice", "voice_button", "voice_cancel"):
        setattr(result, name, Control())
    button = Control()
    result.status_item = SimpleNamespace(button=lambda: button)
    result.voice_original_goal = "previous goal"
    result.goal.value = "previous goal"
    saved = {}
    defaults = SimpleNamespace(setObject_forKey_=lambda value, key: saved.update({key: value}))
    monkeypatch.setattr(macos_app, "NSUserDefaults", SimpleNamespace(standardUserDefaults=lambda: defaults))
    monkeypatch.setattr(macos_app.Quartz, "CGPreflightScreenCaptureAccess", lambda: True)
    monkeypatch.setattr(macos_app.AS, "AXIsProcessTrusted", lambda: True)
    result.saved_for_test = saved
    return result


def test_streaming_voice_never_starts_computer_use_or_persists_partial(app):
    app.voice_session = Session()

    app.refresh_(None)
    app.startStop_(None)

    assert app.goal.value == "new partial"
    assert app.worker is None
    assert app.saved_for_test == {}
    assert not app.start_button.enabled and not app.menu_start.enabled
    assert not app.goal.editable and not app.voice_cancel.hidden
    assert app.voice_button.value == "Done"


def test_done_waits_for_final_tokens_then_enables_review_and_start(app):
    session = app.voice_session = Session()
    app.dictate_(None)
    assert session.finished
    app.refresh_(None)
    assert not app.start_button.enabled

    session.state = VoiceSnapshot(phase="done", text="final corrected goal", message="Done")
    app.refresh_(None)

    assert app.goal.value == "final corrected goal"
    assert app.saved_for_test == {"lastGoal": "final corrected goal"}
    assert app.voice_session is None and app.worker is None
    assert app.start_button.enabled and app.goal.editable


@pytest.mark.parametrize("phase", ["cancelled", "error"])
def test_cancel_or_error_restores_original_goal_and_never_persists_partial(app, phase):
    session = app.voice_session = Session()
    app.refresh_(None)
    session.state = VoiceSnapshot(phase=phase, text="untrusted partial", message="Stopped")

    app.refresh_(None)

    assert app.goal.value == "previous goal"
    assert app.saved_for_test == {}
    assert app.voice_session is None and app.worker is None


@pytest.mark.parametrize("action", ["cancel", "stop", "close", "quit"])
def test_all_exit_controls_cancel_dictation(app, action):
    session = app.voice_session = Session()
    if action == "cancel":
        app.cancelDictation_(None)
    elif action == "stop":
        app.stopRun_(None)
    elif action == "close":
        assert app.windowShouldClose_(None)
    else:
        assert app.applicationShouldTerminate_(None) == macos_app.NSTerminateCancel
        assert app.quit_pending

    assert session.cancelled
    assert app.worker is None
