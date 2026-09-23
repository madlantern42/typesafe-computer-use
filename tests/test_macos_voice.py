"""Synthetic buffers and engines only: no device, prompt, or recording."""

from __future__ import annotations

import struct
import sys
import threading
from types import SimpleNamespace

import pytest

from typesafe_computer_use import macos_voice
from typesafe_computer_use.voice import AudioFormat

_START_AUDIO_ENGINE = macos_voice._start_audio_engine


class Format:
    def __init__(self, rate=48000.0, channels=2, interleaved=False, common=1):
        self.rate, self.channels, self.interleaved, self.common = rate, channels, interleaved, common

    def sampleRate(self):
        return self.rate

    def channelCount(self):
        return self.channels

    def isInterleaved(self):
        return self.interleaved

    def commonFormat(self):
        return self.common


class Channel:
    def __init__(self, samples):
        self.data = bytearray(struct.pack(f"<{len(samples)}f", *samples))
        self.counts = []

    def as_buffer(self, count):
        self.counts.append(count)
        assert count * 4 <= len(self.data), "native memory must not be over-read"
        return memoryview(self.data)[: count * 4]


class Buffer:
    def __init__(self, audio_format=None, frames=3, channels=None):
        self.audio_format = audio_format or Format()
        self.frames = self.capacity = frames
        self.channels = channels if channels is not None else (Channel([0.1, 0.2, 0.3]), Channel([0.4, 0.5, 0.6]))
        self.frame_stride = self.audio_format.channels if self.audio_format.interleaved else 1

    def format(self):
        return self.audio_format

    def frameLength(self):
        return self.frames

    def frameCapacity(self):
        return self.capacity

    def stride(self):
        return self.frame_stride

    def floatChannelData(self):
        return self.channels


class Engine:
    def __init__(self):
        self.audio_format = Format()
        self.started = self.stopped = self.removed = self.prepared = 0
        self.callback = None
        self.start_result = True
        self.on_prepare = lambda: None
        self.on_start = lambda: None

    def inputNode(self):
        return self

    def outputFormatForBus_(self, bus):
        assert bus == 0
        return self.audio_format

    def installTapOnBus_bufferSize_format_block_(self, bus, size, audio_format, callback):
        assert (bus, size, audio_format) == (0, 1024, self.audio_format)
        self.callback = callback

    def prepare(self):
        self.prepared += 1
        self.on_prepare()

    def startAndReturnError_(self, error):
        assert error is None
        self.started += 1
        self.on_start()
        return self.start_result, None if self.start_result else "private native error detail"

    def stop(self):
        self.stopped += 1

    def removeTapOnBus_(self, bus):
        assert bus == 0
        self.removed += 1


@pytest.fixture
def native(monkeypatch):
    engine = Engine()
    state = SimpleNamespace(status=3, requests=[], engines=[])
    monkeypatch.setattr(
        macos_voice,
        "AV",
        SimpleNamespace(AVAudioPCMFormatFloat32=1, AVAuthorizationStatusNotDetermined=0, AVAuthorizationStatusAuthorized=3),
    )
    monkeypatch.setattr(macos_voice, "_microphone_authorization", lambda: state.status)
    monkeypatch.setattr(macos_voice, "_request_microphone_access", state.requests.append)

    def new_engine():
        state.engines.append(engine)
        return engine

    monkeypatch.setattr(macos_voice, "_new_audio_engine", new_engine)
    monkeypatch.setattr(macos_voice, "_start_audio_engine", _START_AUDIO_ENGINE)
    return state, engine


@pytest.mark.parametrize(
    "boundary,args",
    [
        ("_microphone_authorization", ()),
        ("_request_microphone_access", (lambda granted: None,)),
        ("_new_audio_engine", ()),
        ("_start_audio_engine", (None,)),
    ],
)
def test_suite_guard_prevents_microphone_access(boundary, args):
    with pytest.raises(RuntimeError, match="a test reached the real machine"):
        getattr(macos_voice, boundary)(*args)


def test_construct_and_stop_need_no_native_access():
    microphone = macos_voice.Microphone()
    microphone.stop()
    microphone.stop()


def test_prepare_preserves_native_rate_and_does_not_record(native):
    state, engine = native
    engine.audio_format.rate = 44100.0
    microphone = macos_voice.Microphone()
    assert microphone.prepare(threading.Event()) == AudioFormat("pcm_f32le", 44100, 1)
    assert state.engines == [engine]
    assert not state.requests
    assert engine.started == 0 and engine.callback is None
    microphone.stop()


@pytest.mark.parametrize("status", [1, 2, 99])
def test_denied_or_restricted_permission_never_creates_engine(native, status):
    state, engine = native
    state.status = status
    with pytest.raises(RuntimeError, match="Microphone preparation failed"):
        macos_voice.Microphone().prepare(threading.Event())
    assert not state.requests and not state.engines and engine.started == 0


def test_already_cancelled_never_requests_permission(native):
    state, _engine = native
    state.status = 0
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(RuntimeError, match="cancelled"):
        macos_voice.Microphone().prepare(cancel)
    assert not state.requests and not state.engines


@pytest.mark.parametrize("cancel_by_stop", [False, True])
def test_cancelled_permission_prompt_cannot_start_after_late_grant(native, monkeypatch, cancel_by_stop):
    state, engine = native
    state.status = 0
    requested = threading.Event()
    finished = threading.Event()
    errors = []
    microphone = macos_voice.Microphone()
    cancel = threading.Event()

    def request(callback):
        state.requests.append(callback)
        requested.set()

    monkeypatch.setattr(macos_voice, "_request_microphone_access", request)

    def prepare():
        try:
            microphone.prepare(cancel)
        except Exception as error:
            errors.append(error)
        finally:
            finished.set()

    worker = threading.Thread(target=prepare)
    worker.start()
    assert requested.wait(1)
    if cancel_by_stop:
        microphone.stop()
    else:
        cancel.set()
    assert finished.wait(1)
    worker.join(1)
    state.requests[0](True)
    assert errors and not state.engines and engine.started == 0
    with pytest.raises(RuntimeError, match="could not start"):
        microphone.start(lambda data: None, lambda error: None)


@pytest.mark.parametrize("granted", [False, True])
def test_permission_callback_only_unblocks_prepare(native, monkeypatch, granted):
    state, engine = native
    state.status = 0
    monkeypatch.setattr(macos_voice, "_request_microphone_access", lambda callback: callback(granted))
    microphone = macos_voice.Microphone()
    if granted:
        assert microphone.prepare(threading.Event()).sample_rate == 48000
        assert state.engines == [engine] and engine.started == 0
        microphone.stop()
    else:
        with pytest.raises(RuntimeError, match="preparation failed"):
            microphone.prepare(threading.Event())
        assert not state.engines


@pytest.mark.parametrize(
    "audio_format", [Format(rate=0), Format(rate=float("nan")), Format(rate=48000.5), Format(channels=0), Format(common=2)]
)
def test_unavailable_or_unsupported_format_cleans_up(native, audio_format):
    _state, engine = native
    engine.audio_format = audio_format
    with pytest.raises(RuntimeError, match="preparation failed"):
        macos_voice.Microphone().prepare(threading.Event())
    assert engine.stopped == 1 and engine.started == 0


def test_capture_copies_first_channel_before_native_buffer_reuse(native):
    _state, engine = native
    microphone = macos_voice.Microphone()
    microphone.prepare(threading.Event())
    audio, errors = [], []
    microphone.start(audio.append, errors.append)
    buffer = Buffer()
    engine.callback(buffer, None)
    assert audio == [struct.pack("<3f", 0.1, 0.2, 0.3)]
    assert buffer.channels[0].counts == [3]  # count of floats, not byte count
    assert not buffer.channels[1].counts
    buffer.channels[0].data[:] = b"\0" * 12
    assert audio == [struct.pack("<3f", 0.1, 0.2, 0.3)]
    assert not errors
    microphone.stop()
    microphone.stop()
    engine.callback(Buffer(), None)
    assert len(audio) == 1 and engine.stopped == 1 and engine.removed == 1


def test_interleaved_first_channel_is_deinterleaved_without_resampling(native):
    _state, engine = native
    engine.audio_format = Format(interleaved=True)
    microphone = macos_voice.Microphone()
    assert microphone.prepare(threading.Event()) == AudioFormat("pcm_f32le", 48000, 1)
    audio, errors = [], []
    microphone.start(audio.append, errors.append)
    first = Channel([1, 10, 2, 20, 3, 30])
    second = Channel([10, 2, 20, 3, 30])
    engine.callback(Buffer(engine.audio_format, channels=(first, second)), None)
    assert audio == [struct.pack("<3f", 1, 2, 3)]
    assert first.counts == [5] and not second.counts and not errors
    microphone.stop()


@pytest.mark.parametrize("changed", [Format(rate=44100), Format(channels=1), Format(interleaved=True), Format(common=2)])
def test_format_change_reports_once_and_stops_delivering_audio(native, changed):
    _state, engine = native
    microphone = macos_voice.Microphone()
    microphone.prepare(threading.Event())
    audio, errors = [], []
    microphone.start(audio.append, errors.append)
    engine.callback(Buffer(changed), None)
    engine.callback(Buffer(changed), None)
    engine.callback(Buffer(), None)
    assert not audio and len(errors) == 1
    assert "format changed" in errors[0]
    microphone.stop()


def test_empty_frames_ignored_and_invalid_buffer_never_reads_memory(native):
    _state, engine = native
    microphone = macos_voice.Microphone()
    microphone.prepare(threading.Event())
    audio, errors = [], []
    microphone.start(audio.append, errors.append)
    empty = Buffer(frames=0)
    engine.callback(empty, None)
    assert not audio and not errors and not empty.channels[0].counts
    invalid = Buffer(frames=4)
    invalid.capacity = 3
    engine.callback(invalid, None)
    assert not audio and len(errors) == 1 and not invalid.channels[0].counts
    microphone.stop()


def test_start_failure_cleans_up_and_sanitizes_native_error(native):
    _state, engine = native
    microphone = macos_voice.Microphone()
    microphone.prepare(threading.Event())
    engine.start_result = False
    with pytest.raises(RuntimeError, match="could not start") as caught:
        microphone.start(lambda data: None, lambda error: None)
    assert "private" not in str(caught.value)
    assert engine.started == engine.stopped == engine.removed == 1
    microphone.stop()
    assert engine.stopped == 1


def test_cancellation_between_preparation_and_start_never_records(native):
    _state, engine = native
    microphone = macos_voice.Microphone()
    cancel = threading.Event()
    microphone.prepare(cancel)
    engine.on_prepare = cancel.set
    with pytest.raises(RuntimeError, match="could not start"):
        microphone.start(lambda data: None, lambda error: None)
    assert engine.started == 0 and engine.stopped == 1 and engine.removed == 1


def test_callback_errors_are_sanitized_and_do_not_escape(native):
    _state, engine = native
    microphone = macos_voice.Microphone()
    microphone.prepare(threading.Event())
    errors = []

    def reject_audio(data):
        raise RuntimeError("private audio or credential detail")

    microphone.start(reject_audio, errors.append)
    engine.callback(Buffer(), None)
    engine.callback(Buffer(), None)
    assert errors == ["Microphone audio could not be delivered. Start voice input again."]
    microphone.stop()


@pytest.mark.skipif(sys.platform != "darwin", reason="in-memory AVAudioPCMBuffer verifies the macOS PyObjC bridge")
@pytest.mark.parametrize("interleaved", [False, True])
def test_real_pyobjc_buffer_with_synthetic_samples(interleaved):
    """No engine/device is created; the microphone guard remains active."""
    av = macos_voice.AV
    audio_format = av.AVAudioFormat.alloc().initWithCommonFormat_sampleRate_channels_interleaved_(
        av.AVAudioPCMFormatFloat32, 48000, 2, interleaved
    )
    buffer = av.AVAudioPCMBuffer.alloc().initWithPCMFormat_frameCapacity_(audio_format, 3)
    buffer.setFrameLength_(3)
    channels = buffer.floatChannelData()
    if interleaved:
        channels[0][0:6] = [1.0, 10.0, 2.0, 20.0, 3.0, 30.0]
    else:
        channels[0][0:3] = [1.0, 2.0, 3.0]
        channels[1][0:3] = [10.0, 20.0, 30.0]
    copied = macos_voice._first_channel_bytes(buffer, (48000, 2, interleaved))
    channels[0][0] = 99.0
    assert copied == struct.pack("<3f", 1.0, 2.0, 3.0)
