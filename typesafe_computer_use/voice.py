"""Bounded, single-use Soniox dictation sessions. No audio or transcript is persisted."""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Literal, Protocol

import websocket

SONIOX_URL = "wss://stt-rt.soniox.com/transcribe-websocket"
SONIOX_MODEL = "stt-rt-v5"
CONNECT_TIMEOUT = 10.0
IO_TIMEOUT = 2.0
FINAL_TIMEOUT = 10.0
MAX_RECORDING_SECONDS = 120.0
MAX_QUEUED_SECONDS = 2.0
MAX_QUEUED_CHUNKS = 128
TERMINAL_PHASES = {"done", "cancelled", "error"}
Phase = Literal["connecting", "listening", "finishing", "cancelling", "done", "cancelled", "error"]


@dataclass(frozen=True)
class AudioFormat:
    audio_format: str
    sample_rate: int
    num_channels: int

    @property
    def bytes_per_second(self) -> int:
        widths = {"pcm_s8": 1, "pcm_u8": 1, "mulaw": 1, "alaw": 1}
        for kind, bits in (("s", 16), ("s", 24), ("s", 32), ("u", 16), ("u", 24), ("u", 32), ("f", 32), ("f", 64)):
            for endian in ("le", "be"):
                widths[f"pcm_{kind}{bits}{endian}"] = bits // 8
        if self.audio_format not in widths or self.sample_rate <= 0 or self.num_channels <= 0:
            raise ValueError("Unsupported microphone audio format.")
        return widths[self.audio_format] * self.sample_rate * self.num_channels


class Microphone(Protocol):
    def prepare(self, cancel_event: threading.Event) -> AudioFormat: ...

    def start(self, on_audio: Callable[[bytes], None], on_error: Callable[[str], None]) -> None: ...

    def stop(self) -> None: ...


@dataclass(frozen=True)
class VoiceSnapshot:
    phase: Phase
    text: str = ""
    message: str = ""

    @property
    def busy(self) -> bool:
        return self.phase not in TERMINAL_PHASES


class VoiceSession:
    """One explicit recording: start, then finish or cancel. Construct a new session to retry.

    The microphone callback only enqueues bounded audio; a worker sends it while a separate
    receiver collects tokens. Final tokens append once, provisional tokens replace the last
    guess. Endpoint markers finalize utterances without ending the recording.
    """

    def __init__(self, api_key: str, microphone: Microphone, connect: Callable | None = None):
        self._api_key = api_key
        self._microphone = microphone
        self._connect = connect or websocket.create_connection
        self._condition = threading.Condition()
        self._microphone_lock = threading.Lock()
        self._cancelled = threading.Event()
        self._halt = threading.Event()
        self._finished = threading.Event()
        self._worker_done = threading.Event()
        self._finish_requested = False
        self._started = False
        self._accepting_audio = False
        self._socket = None
        self._audio: deque[bytes] = deque()
        self._queued_bytes = 0
        self._byte_limit = 0
        self._confirmed = ""
        self._provisional_pending = False
        self._pending_terminal: VoiceSnapshot | None = None
        self._snapshot = VoiceSnapshot("cancelled")

    @property
    def busy(self) -> bool:
        return self.snapshot().busy

    def snapshot(self) -> VoiceSnapshot:
        with self._condition:
            return self._snapshot

    def start(self) -> None:
        with self._condition:
            if self._started or self._cancelled.is_set():
                return
            self._started = True
            self._snapshot = VoiceSnapshot("connecting", message="Connecting to voice transcription…")
        threading.Thread(target=self._run, name="typesafe-voice", daemon=True).start()

    def finish(self) -> None:
        with self._condition:
            if not self._snapshot.busy or self._finish_requested or self._halt.is_set():
                return
            self._finish_requested = True
            self._accepting_audio = False
            self._snapshot = VoiceSnapshot("finishing", self._snapshot.text, "Finishing transcription…")
            self._condition.notify_all()
        self._stop_microphone()

    def cancel(self) -> None:
        with self._condition:
            self._cancelled.set()
            self._halt.set()
            self._accepting_audio = False
            self._confirmed = ""
            self._clear_audio()
            self._pending_terminal = VoiceSnapshot("cancelled", message="Voice input cancelled.")
            self._snapshot = (
                self._pending_terminal
                if not self._started or self._worker_done.is_set()
                else VoiceSnapshot("cancelling", message="Cancelling voice input…")
            )
            self._condition.notify_all()
        self._stop_microphone()
        self._close_socket()

    def _clear_audio(self) -> None:
        self._audio.clear()
        self._queued_bytes = 0

    def _fail(self, message: str) -> None:
        with self._condition:
            if self._halt.is_set() or self._snapshot.phase in TERMINAL_PHASES:
                return
            self._halt.set()
            self._accepting_audio = False
            self._confirmed = ""
            self._clear_audio()
            self._pending_terminal = VoiceSnapshot("error", message=message)
            self._snapshot = VoiceSnapshot("finishing", message=message)
            self._condition.notify_all()

    def _on_audio(self, data: bytes) -> None:
        if not data:
            return  # An empty frame is reserved for the explicit end-of-stream message.
        with self._condition:
            if not self._accepting_audio or self._halt.is_set():
                return
            if self._queued_bytes + len(data) > self._byte_limit or len(self._audio) >= MAX_QUEUED_CHUNKS:
                self._fail("The voice connection could not keep up with the microphone. Please try again.")
                return
            self._audio.append(bytes(data))
            self._queued_bytes += len(data)
            self._condition.notify_all()

    def _on_microphone_error(self, _message: str) -> None:
        self._fail("The microphone stopped unexpectedly. Check the input device and try again.")

    def _stop_microphone(self) -> None:
        with self._microphone_lock, suppress(Exception):
            self._microphone.stop()

    def _close_socket(self) -> None:
        with self._condition:
            connection, self._socket = self._socket, None
        if connection is not None:
            with suppress(Exception):
                # shutdown() closes immediately and wakes a blocked recv without a close handshake.
                connection.shutdown()

    def _run(self) -> None:
        receiver = None
        try:
            if not self._api_key.strip():
                self._fail("Add a Soniox API key to enable voice input.")
                return
            try:
                audio_format = self._microphone.prepare(self._cancelled)
                self._byte_limit = int(audio_format.bytes_per_second * MAX_QUEUED_SECONDS)
            except Exception:
                self._fail("The microphone is unavailable. Check microphone permission and the input device.")
                return
            if self._halt.is_set():
                return
            connection = self._connect(SONIOX_URL, timeout=CONNECT_TIMEOUT, enable_multithread=True)
            with self._condition:
                self._socket = connection
            if self._halt.is_set():
                return
            connection.settimeout(IO_TIMEOUT)
            connection.send(
                json.dumps(
                    {
                        "api_key": self._api_key,
                        "model": SONIOX_MODEL,
                        "audio_format": audio_format.audio_format,
                        "sample_rate": audio_format.sample_rate,
                        "num_channels": audio_format.num_channels,
                        "enable_endpoint_detection": True,
                    }
                )
            )
            receiver = threading.Thread(target=self._receive, args=(connection,), name="typesafe-voice-results", daemon=True)
            receiver.start()
            with self._microphone_lock:
                with self._condition:
                    if self._halt.is_set():
                        return
                    self._accepting_audio = not self._finish_requested
                    if self._accepting_audio:
                        self._snapshot = VoiceSnapshot("listening", message="Listening…")
                if self._accepting_audio:
                    try:
                        self._microphone.start(self._on_audio, self._on_microphone_error)
                    except Exception:
                        self._fail("The microphone could not start. Check the input device and try again.")
                        return
            self._send_audio(connection)
        except Exception:
            # Transport errors may contain headers, authentication, or provider text. Never expose them.
            self._fail("Voice transcription could not connect or was interrupted. Please try again.")
        finally:
            self._stop_microphone()
            self._close_socket()
            if receiver is not None:
                receiver.join(timeout=IO_TIMEOUT + 0.5)
            with self._condition:
                self._clear_audio()
                self._api_key = ""
                self._worker_done.set()
                if self._pending_terminal is not None:
                    self._snapshot = self._pending_terminal

    def _send_audio(self, connection) -> None:
        deadline = time.monotonic() + MAX_RECORDING_SECONDS
        while not self._halt.is_set():
            reached_limit = False
            with self._condition:
                if not self._finish_requested and time.monotonic() >= deadline:
                    self._finish_requested = True
                    self._accepting_audio = False
                    self._snapshot = VoiceSnapshot("finishing", self._snapshot.text, "Two-minute limit reached; finishing…")
                    reached_limit = True
                if self._audio:
                    data = self._audio.popleft()
                    self._queued_bytes -= len(data)
                elif self._finish_requested:
                    break
                else:
                    self._condition.wait(timeout=min(0.2, max(0.0, deadline - time.monotonic())))
                    continue
            if reached_limit:
                self._stop_microphone()
            if not self._halt.is_set():
                connection.send(data, opcode=websocket.ABNF.OPCODE_BINARY)
        if self._halt.is_set():
            return
        self._stop_microphone()
        connection.send("")
        deadline = time.monotonic() + FINAL_TIMEOUT
        with self._condition:
            while not self._halt.is_set() and not self._finished.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._fail("Voice transcription timed out while finishing. Please try again.")
                    return
                self._condition.wait(timeout=remaining)
            if not self._halt.is_set():
                self._pending_terminal = VoiceSnapshot("done", self._confirmed.strip(), "Voice input is ready to review.")

    def _receive(self, connection) -> None:
        try:
            while not self._halt.is_set():
                try:
                    raw = connection.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                if not raw:
                    self._fail("The voice connection closed before transcription finished. Please try again.")
                    return
                response = json.loads(raw)
                if not isinstance(response, dict):
                    raise ValueError("Invalid voice response.")
                if response.get("error_code") or response.get("error_type"):
                    self._fail("The voice service rejected this session. Check the Soniox key and account, then try again.")
                    return
                tokens = response.get("tokens", [])
                if not isinstance(tokens, list):
                    raise ValueError("Invalid voice tokens.")
                confirmed, provisional = [], []
                for token in tokens:
                    if not isinstance(token, dict) or not isinstance(token.get("text"), str):
                        raise ValueError("Invalid voice token.")
                    text = token["text"].replace("<end>", "").replace("<fin>", "")
                    (confirmed if token.get("is_final") is True else provisional).append(text)
                with self._condition:
                    if self._halt.is_set():
                        return
                    self._confirmed += "".join(confirmed)
                    if any(provisional):
                        self._provisional_pending = True
                    elif any(confirmed):
                        self._provisional_pending = False
                    self._snapshot = VoiceSnapshot(
                        self._snapshot.phase, self._confirmed + "".join(provisional), self._snapshot.message
                    )
                    if response.get("finished"):
                        if self._provisional_pending:
                            self._fail("Voice transcription ended before all speech was finalized. Please try again.")
                        elif not self._finish_requested:
                            self._fail("The voice service ended the recording early. Please try again.")
                        else:
                            self._finished.set()
                        self._condition.notify_all()
                        return
        except Exception:
            self._fail("Voice transcription was interrupted or returned an invalid response. Please try again.")
