import json
import queue
import threading
import time

import pytest
import websocket

from typesafe_computer_use import voice
from typesafe_computer_use.voice import AudioFormat, VoiceSession


class FakeMicrophone:
    def __init__(self):
        self.started = threading.Event()
        self.stopped = threading.Event()
        self.on_audio = None
        self.on_error = None

    def prepare(self, cancel_event):
        return AudioFormat("pcm_s16le", 8000, 1)

    def start(self, on_audio, on_error):
        self.on_audio, self.on_error = on_audio, on_error
        self.started.set()

    def stop(self):
        self.stopped.set()


class FakeSocket:
    def __init__(self):
        self.incoming = queue.Queue()
        self.sent = []
        self.ended = threading.Event()
        self.closed = threading.Event()

    def settimeout(self, timeout):
        self.timeout = timeout

    def send(self, data, opcode=websocket.ABNF.OPCODE_TEXT):
        self.sent.append((data, opcode))
        if data == "":
            self.ended.set()

    def recv(self):
        try:
            return self.incoming.get(timeout=0.05)
        except queue.Empty:
            raise websocket.WebSocketTimeoutException() from None

    def shutdown(self):
        self.closed.set()
        self.incoming.put("")

    def response(self, tokens=(), **fields):
        self.incoming.put(json.dumps({"tokens": list(tokens), **fields}))


def token(text, final=False):
    return {"text": text, "is_final": final}


def wait_for(predicate):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if predicate():
            return
        threading.Event().wait(0.005)
    raise AssertionError("Timed out waiting for the offline voice worker")


@pytest.fixture
def session():
    microphone, connection = FakeMicrophone(), FakeSocket()
    calls = []

    def connect(url, **kwargs):
        calls.append((url, kwargs))
        return connection

    session = VoiceSession("test-key", microphone, connect=connect)
    session.start()
    assert microphone.started.wait(2)
    yield session, microphone, connection, calls
    session.cancel()
    wait_for(lambda: not session.busy)


def test_sends_raw_audio_and_done_drains_final_tokens_before_committing(session):
    session, microphone, connection, calls = session
    config = json.loads(connection.sent[0][0])
    assert calls[0][0] == voice.SONIOX_URL
    assert calls[0][1]["timeout"] == voice.CONNECT_TIMEOUT
    assert config == {
        "api_key": "test-key",
        "model": "stt-rt-v5",
        "audio_format": "pcm_s16le",
        "sample_rate": 8000,
        "num_channels": 1,
        "enable_endpoint_detection": True,
    }
    microphone.on_audio(b"\x00\x01")
    microphone.on_audio(b"\x02\x03")
    connection.response([token("Search", False)])
    wait_for(lambda: session.snapshot().text == "Search")

    session.finish()
    assert connection.ended.wait(2)
    assert microphone.stopped.is_set()
    assert session.snapshot().phase == "finishing" and session.busy
    assert connection.sent[1:] == [
        (b"\x00\x01", websocket.ABNF.OPCODE_BINARY),
        (b"\x02\x03", websocket.ABNF.OPCODE_BINARY),
        ("", websocket.ABNF.OPCODE_TEXT),
    ]
    connection.response([token("Search mail", True), token("<fin>", True)])
    wait_for(lambda: session.snapshot().text == "Search mail")
    assert session.busy  # A final token alone is not the finished acknowledgement.
    connection.response(finished=True)
    wait_for(lambda: session.snapshot().phase == "done")
    assert session.snapshot().text == "Search mail"
    assert connection.closed.is_set() and not session.snapshot().busy


def test_partial_revisions_replace_guesses_and_endpoint_keeps_prior_utterance(session):
    session, _, connection, _ = session
    connection.response([token("Sea")])
    wait_for(lambda: session.snapshot().text == "Sea")
    first_snapshot = session.snapshot()
    connection.response([token("Search ", True), token("Grace")])
    wait_for(lambda: session.snapshot().text == "Search Grace")
    connection.response([token("Gracie")])
    wait_for(lambda: session.snapshot().text == "Search Gracie")
    connection.response([token("Gracie", True), token("<end>", True)])
    wait_for(lambda: session._confirmed == "Search Gracie")
    assert session.snapshot().phase == "listening"
    connection.response([token(" tomorrow")])
    wait_for(lambda: session.snapshot().text == "Search Gracie tomorrow")
    assert first_snapshot.text == "Sea"  # Pollers keep an immutable value, not mutable shared state.
    session.finish()
    assert connection.ended.wait(2)
    connection.response([token(" tomorrow", True)], finished=True)
    wait_for(lambda: not session.busy)
    assert session.snapshot().text == "Search Gracie tomorrow"


def test_cancel_revokes_text_ignores_late_results_and_cannot_restart(session):
    session, microphone, connection, calls = session
    connection.response([token("private draft", True)])
    wait_for(lambda: bool(session.snapshot().text))
    session.cancel()
    connection.response([token(" stale response", True)], finished=True)
    microphone.on_audio(b"late audio")
    wait_for(lambda: not session.busy)
    assert session.snapshot().phase == "cancelled" and session.snapshot().text == ""
    assert connection.closed.is_set() and microphone.stopped.is_set()
    session.start()
    assert len(calls) == 1 and not connection.ended.is_set()


def test_cancel_during_connect_stays_busy_until_late_socket_is_closed():
    microphone, connection = FakeMicrophone(), FakeSocket()
    connecting, release = threading.Event(), threading.Event()

    def connect(*args, **kwargs):
        connecting.set()
        assert release.wait(2)
        return connection

    session = VoiceSession("test-key", microphone, connect=connect)
    session.start()
    assert connecting.wait(2)
    session.cancel()
    assert session.snapshot().phase == "cancelling" and session.busy
    release.set()
    wait_for(lambda: not session.busy)
    assert connection.closed.is_set() and not connection.sent
    assert not microphone.started.is_set()


def test_microphone_queue_overflow_is_explicit_and_does_not_drop_audio_silently():
    class BurstMicrophone(FakeMicrophone):
        def start(self, on_audio, on_error):
            super().start(on_audio, on_error)
            on_audio(bytes(16000))
            on_audio(bytes(16000))
            on_audio(b"\x00")  # Queued chunks together exceed two seconds at 16kB/sec.

    microphone, connection = BurstMicrophone(), FakeSocket()
    session = VoiceSession("test-key", microphone, connect=lambda *a, **kw: connection)
    session.start()
    wait_for(lambda: session.snapshot().phase == "error")
    assert "keep up" in session.snapshot().message
    assert microphone.stopped.is_set() and connection.closed.is_set()
    assert len(connection.sent) == 1  # Only configuration, no malformed or partial audio upload.


def test_error_stays_busy_until_microphone_cleanup_finishes():
    stopping, release = threading.Event(), threading.Event()

    class SlowStopMicrophone(FakeMicrophone):
        def stop(self):
            stopping.set()
            assert release.wait(2)
            super().stop()

    microphone, connection = SlowStopMicrophone(), FakeSocket()
    session = VoiceSession("test-key", microphone, connect=lambda *a, **kw: connection)
    session.start()
    assert microphone.started.wait(2)
    connection.response(error_code=503)
    assert stopping.wait(2)
    assert session.busy and session.snapshot().phase == "finishing"
    release.set()
    wait_for(lambda: session.snapshot().phase == "error")
    assert microphone.stopped.is_set() and connection.closed.is_set()


@pytest.mark.parametrize("response", [{"error_code": 401, "error_message": "test-key private transcript"}, {"tokens": "private"}])
def test_provider_errors_are_masked_and_clean_up(session, response):
    session, microphone, connection, _ = session
    connection.incoming.put(json.dumps(response))
    wait_for(lambda: session.snapshot().phase == "error")
    assert "test-key" not in session.snapshot().message and "private" not in session.snapshot().message
    assert session.snapshot().text == ""
    assert microphone.stopped.is_set() and connection.closed.is_set()


def test_transport_exception_is_masked_and_not_retried():
    calls = []

    def connect(*args, **kwargs):
        calls.append(1)
        raise RuntimeError("test-key private transcript")

    microphone = FakeMicrophone()
    session = VoiceSession("test-key", microphone, connect=connect)
    session.start()
    wait_for(lambda: session.snapshot().phase == "error")
    assert "test-key" not in session.snapshot().message and "private" not in session.snapshot().message
    assert calls == [1] and microphone.stopped.is_set()


def test_microphone_error_is_masked(session):
    session, microphone, connection, _ = session
    microphone.on_error("private hardware error containing test-key")
    wait_for(lambda: session.snapshot().phase == "error")
    assert "private" not in session.snapshot().message and "test-key" not in session.snapshot().message
    assert microphone.stopped.is_set() and connection.closed.is_set()


def test_finished_without_done_is_an_error(session):
    session, _, connection, _ = session
    connection.response([token("unexpected", True)], finished=True)
    wait_for(lambda: session.snapshot().phase == "error")
    assert session.snapshot().text == ""


def test_done_has_a_bounded_final_drain(session, monkeypatch):
    monkeypatch.setattr(voice, "FINAL_TIMEOUT", 0.02)
    session, microphone, connection, _ = session
    session.finish()
    assert connection.ended.wait(2)
    wait_for(lambda: session.snapshot().phase == "error")
    assert "timed out" in session.snapshot().message
    assert microphone.stopped.is_set() and connection.closed.is_set()


def test_recording_has_a_bounded_duration(monkeypatch):
    monkeypatch.setattr(voice, "MAX_RECORDING_SECONDS", 0.02)
    microphone, connection = FakeMicrophone(), FakeSocket()
    session = VoiceSession("test-key", microphone, connect=lambda *a, **kw: connection)
    session.start()
    assert connection.ended.wait(2)
    assert session.snapshot().phase == "finishing" and microphone.stopped.is_set()
    connection.response([token("A complete goal", True)], finished=True)
    wait_for(lambda: session.snapshot().phase == "done")
    assert session.snapshot().text == "A complete goal"
    assert microphone.stopped.is_set() and connection.closed.is_set()


@pytest.mark.parametrize("final_tokens", [[], [token(" unfinished")]])
def test_finished_cannot_commit_a_stable_prefix_while_provisional_speech_remains(session, final_tokens):
    session, _, connection, _ = session
    connection.response([token("Find ", True), token("the unfinished goal")])
    wait_for(lambda: session.snapshot().text == "Find the unfinished goal")
    session.finish()
    assert connection.ended.wait(2)
    connection.response(final_tokens, finished=True)
    wait_for(lambda: session.snapshot().phase == "error")
    assert session.snapshot().text == ""
    assert "finalized" in session.snapshot().message


def test_cancel_during_permission_never_connects_or_starts_microphone():
    entered = threading.Event()

    class PermissionMicrophone(FakeMicrophone):
        def prepare(self, cancel_event):
            entered.set()
            assert cancel_event.wait(2)
            raise RuntimeError("Cancelled permission request")

    calls = []
    microphone = PermissionMicrophone()
    session = VoiceSession("test-key", microphone, connect=lambda *a, **kw: calls.append(1))
    session.start()
    assert entered.wait(2)
    session.cancel()
    wait_for(lambda: not session.busy)
    assert session.snapshot().phase == "cancelled" and not calls
    assert not microphone.started.is_set() and microphone.stopped.is_set()


def test_native_float_format_has_correct_queue_byte_budget():
    assert AudioFormat("pcm_f32le", 48000, 1).bytes_per_second == 192000
    assert AudioFormat("pcm_s16le", 16000, 2).bytes_per_second == 64000
    with pytest.raises(ValueError):
        _ = AudioFormat("auto", 48000, 1).bytes_per_second
