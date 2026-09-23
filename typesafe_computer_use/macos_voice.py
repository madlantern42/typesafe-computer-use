"""Explicit, cancellable microphone capture for the macOS voice input button.

The engine and permission prompt are created only by ``prepare``. Each instance
belongs to one voice session; stopping it permanently prevents a later start.
"""

from __future__ import annotations

import math
import sys
import threading
from collections.abc import Callable
from contextlib import suppress
from typing import TYPE_CHECKING

import AVFoundation as AV

if TYPE_CHECKING:
    from .voice import AudioFormat


def _microphone_authorization() -> int:
    return AV.AVCaptureDevice.authorizationStatusForMediaType_(AV.AVMediaTypeAudio)


def _request_microphone_access(callback: Callable[[bool], None]) -> None:
    AV.AVCaptureDevice.requestAccessForMediaType_completionHandler_(AV.AVMediaTypeAudio, callback)


def _new_audio_engine():
    return AV.AVAudioEngine.alloc().init()


def _start_audio_engine(engine) -> bool:
    started, _error = engine.startAndReturnError_(None)
    return bool(started)


def _format_signature(audio_format) -> tuple[int, int, bool]:
    rate = float(audio_format.sampleRate())
    channels = int(audio_format.channelCount())
    if (
        sys.byteorder != "little"
        or audio_format.commonFormat() != AV.AVAudioPCMFormatFloat32
        or not math.isfinite(rate)
        or rate <= 0
        or not rate.is_integer()
        or channels < 1
    ):
        raise RuntimeError("The microphone does not provide a supported float32 audio format.")
    return int(rate), channels, bool(audio_format.isInterleaved())


def _first_channel_bytes(buffer, expected: tuple[int, int, bool]) -> bytes:
    if _format_signature(buffer.format()) != expected:
        raise RuntimeError("The microphone audio format changed. Start voice input again.")
    frames = int(buffer.frameLength())
    if frames < 0 or frames > int(buffer.frameCapacity()):
        raise RuntimeError("The microphone returned an invalid audio buffer.")
    if not frames:
        return b""
    stride = int(buffer.stride())
    if stride != (expected[1] if expected[2] else 1):
        raise RuntimeError("The microphone returned an unsupported audio layout.")
    channels = buffer.floatChannelData()
    if not channels or len(channels) != expected[1]:
        raise RuntimeError("The microphone returned no float32 audio data.")
    # PyObjC's manual floatChannelData binding returns one varlist(float) per
    # channel. as_buffer counts FLOATS, not bytes. Copy while this tap owns the
    # AVAudioPCMBuffer; a queued memoryview would outlive its native storage.
    view = channels[0].as_buffer((frames - 1) * stride + 1)
    if stride == 1:
        return bytes(view)
    return memoryview(view).cast("B").cast("f")[::stride].tobytes()


class Microphone:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._error_lock = threading.Lock()
        self._stopped = threading.Event()
        self._active = threading.Event()
        self._failed = False
        self._cancel: threading.Event | None = None
        self._engine = None
        self._node = None
        self._format = None
        self._signature: tuple[int, int, bool] | None = None
        self._tap_installed = False

    def _check_cancelled(self) -> None:
        if self._stopped.is_set() or (self._cancel is not None and self._cancel.is_set()):
            raise RuntimeError("Microphone input was cancelled.")

    def prepare(self, cancel_event: threading.Event) -> AudioFormat:
        from .voice import AudioFormat

        with self._lock:
            self._check_cancelled()
            if self._cancel is not None:
                raise RuntimeError("This microphone session has already been prepared.")
            self._cancel = cancel_event
            self._check_cancelled()

        try:
            status = _microphone_authorization()
            if status == AV.AVAuthorizationStatusNotDetermined:
                completed = threading.Event()
                granted = False

                def permission_result(allowed: bool) -> None:
                    nonlocal granted
                    granted = bool(allowed)
                    completed.set()

                self._check_cancelled()
                _request_microphone_access(permission_result)
                while not completed.wait(0.05):
                    self._check_cancelled()
                self._check_cancelled()
                if not granted:
                    raise RuntimeError("Microphone permission was not granted.")
            elif status != AV.AVAuthorizationStatusAuthorized:
                raise RuntimeError("Microphone access is disabled in System Settings.")

            with self._lock:
                self._check_cancelled()
                self._engine = _new_audio_engine()
                self._node = self._engine.inputNode()
                self._format = self._node.outputFormatForBus_(0)
                self._signature = _format_signature(self._format)
                self._check_cancelled()
                return AudioFormat(audio_format="pcm_f32le", sample_rate=self._signature[0], num_channels=1)
        except Exception:
            self.stop()
            raise RuntimeError("Microphone preparation failed. Check microphone access and the selected input device.") from None

    def start(self, on_audio: Callable[[bytes], None], on_error: Callable[[str], None]) -> None:
        def fail(message: str) -> None:
            with self._error_lock:
                if self._failed or not self._active.is_set():
                    return
                self._failed = True
                self._active.clear()
            # The consumer handles cleanup on its worker, never on the audio tap.
            with suppress(Exception):
                on_error(message)

        def audio_tap(buffer, _when) -> None:
            if not self._active.is_set() or (self._cancel is not None and self._cancel.is_set()):
                return
            try:
                data = _first_channel_bytes(buffer, self._signature)
            except Exception:
                fail("The microphone audio format changed or audio capture became unavailable. Start voice input again.")
                return
            if (
                data
                and self._active.is_set()
                and not self._stopped.is_set()
                and not (self._cancel is not None and self._cancel.is_set())
            ):
                try:
                    on_audio(data)
                except Exception:
                    fail("Microphone audio could not be delivered. Start voice input again.")

        try:
            with self._lock:
                self._check_cancelled()
                if self._engine is None or self._signature is None:
                    raise RuntimeError("Prepare the microphone before starting it.")
                if self._tap_installed:
                    raise RuntimeError("The microphone is already running.")
                self._active.set()
                self._node.installTapOnBus_bufferSize_format_block_(0, 1024, self._format, audio_tap)
                self._tap_installed = True
                self._engine.prepare()
                self._check_cancelled()
                if not _start_audio_engine(self._engine):
                    raise RuntimeError("The microphone could not be started.")
                self._check_cancelled()
        except Exception:
            self.stop()
            raise RuntimeError("Microphone capture could not start. Check the selected input device.") from None

    def stop(self) -> None:
        self._stopped.set()
        self._active.clear()
        with self._lock:
            engine, node, had_tap = self._engine, self._node, self._tap_installed
            self._engine = self._node = self._format = self._signature = None
            self._tap_installed = False
            if engine is not None:
                with suppress(Exception):
                    engine.stop()
            if node is not None and had_tap:
                with suppress(Exception):
                    node.removeTapOnBus_(0)
