"""Wyoming Whisper streaming speech-to-text backend.

Connects to a Wyoming-protocol Whisper server (e.g. the ``wyoming-whisper``
Docker container at TCP port 10300) and transcribes audio via the Wyoming
JSON+PCM framing protocol.

Because wyoming-whisper emits ONE ``transcript`` event per
``audio-start`` … ``audio-stop`` cycle (no partial/interim results), the
stream uses a silence-detection loop to carve the continuous audio feed
into per-utterance chunks.  The design:

  * ``send(chunk)`` accumulates PCM in a ring-buffer and bumps
    ``_last_speech_ts`` whenever a chunk's RMS exceeds the configured
    threshold.
  * A background ``_watch_silence`` task fires ~10 Hz; when the buffer is
    non-empty and silence has lasted ``silence_ms`` milliseconds it
    captures the buffer, clears it, and dispatches ``_transcribe_one``
    as a detached asyncio Task.
  * ``_transcribe_one`` opens a fresh TCP connection, sends
    ``Transcribe`` + ``AudioStart`` + ``AudioChunk``s + ``AudioStop``,
    reads the ``Transcript`` response, and puts a ``FinalTranscript``
    event onto the queue.
  * ``close()`` cancels the silence watcher, waits for any in-flight
    transcription to finish, then puts ``None`` onto the queue so
    ``events()`` terminates.

One stream = one connected caller.  The backend is safe to re-open for
subsequent sessions (each call to ``open_stream`` creates a fresh
``_WyomingWhisperStream`` instance with its own queue and tasks).
"""

from __future__ import annotations

import asyncio
import audioop
import logging
import time
from collections.abc import AsyncIterator

from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.tools import ToolParameterType
from gilbert.interfaces.transcription import (
    FinalTranscript,
    StreamConfig,
    StreamingTranscriptionBackend,
    TranscriptionError,
    TranscriptionEvent,
    TranscriptionStream,
)

logger = logging.getLogger(__name__)

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 10300
_DEFAULT_SILENCE_MS = 600
_DEFAULT_SILENCE_RMS = 200

# Maximum audio bytes to accumulate before forcing a flush (prevents unbounded
# memory growth when the caller never goes quiet).  10 minutes of 16kHz mono
# s16le = 16000 * 2 * 600 = ~19 MB — set a lower practical cap.
_MAX_BUFFER_BYTES = 16_000 * 2 * 60  # 1 minute of 16kHz mono s16le


def _rms(chunk: bytes) -> int:
    """Return the RMS amplitude of a s16le PCM chunk.

    Falls back to 0 on empty or malformed input.
    """
    if len(chunk) < 2:
        return 0
    # audioop.rms(data, width) — width=2 for 16-bit samples.
    # Deprecated in Python 3.13 but still present and functional in 3.12.
    return audioop.rms(chunk, 2)  # type: ignore[attr-defined]


class _WyomingWhisperStream(TranscriptionStream):
    """Per-session streaming transcription backed by a Wyoming TCP server.

    Utterance segmentation is done locally via RMS silence detection.
    Each detected utterance is sent to the Wyoming server in a single
    ``audio-start`` / ``audio-chunk*`` / ``audio-stop`` round-trip.
    """

    def __init__(
        self,
        host: str,
        port: int,
        config: StreamConfig,
        silence_ms: int = _DEFAULT_SILENCE_MS,
        silence_rms_threshold: int = _DEFAULT_SILENCE_RMS,
    ) -> None:
        self._host = host
        self._port = port
        self._config = config
        self._silence_ms = silence_ms
        self._silence_threshold = silence_rms_threshold

        self._buffer = bytearray()
        self._last_speech_ts: float = time.monotonic()
        self._queue: asyncio.Queue[TranscriptionEvent | None] = asyncio.Queue()
        self._closed = False

        # In-flight transcription task (if any).  We keep a reference so
        # close() can await it before signalling stream end.
        self._utterance_task: asyncio.Task[None] | None = None

        # Start the background silence-watcher immediately.
        self._silence_check_task: asyncio.Task[None] = asyncio.create_task(
            self._watch_silence(), name="wyoming-silence-watcher"
        )

    # ------------------------------------------------------------------
    # TranscriptionStream interface
    # ------------------------------------------------------------------

    async def send(self, chunk: bytes) -> None:
        """Accumulate an audio chunk and update the speech timestamp."""
        if self._closed:
            return
        self._buffer.extend(chunk)
        if _rms(chunk) > self._silence_threshold:
            self._last_speech_ts = time.monotonic()
        # Safety flush — prevent unbounded memory growth.
        if len(self._buffer) >= _MAX_BUFFER_BYTES:
            logger.warning(
                "wyoming-whisper: buffer exceeded %d bytes, force-flushing utterance",
                _MAX_BUFFER_BYTES,
            )
            await self._flush_buffer()

    async def close(self) -> None:
        """Signal end-of-session.

        Cancels the silence watcher, flushes any remaining buffered audio
        as a final utterance, waits for in-flight transcription to finish,
        then terminates the ``events()`` iterator by pushing ``None``.
        """
        if self._closed:
            return
        self._closed = True

        # Stop the silence watcher first.
        self._silence_check_task.cancel()
        try:
            await self._silence_check_task
        except asyncio.CancelledError:
            pass

        # Flush any remaining audio.
        if self._buffer:
            await self._flush_buffer()

        # Wait for the most recent in-flight transcription to complete.
        if self._utterance_task is not None and not self._utterance_task.done():
            try:
                await self._utterance_task
            except Exception:  # noqa: BLE001
                pass

        # Signal the events() iterator to stop.
        await self._queue.put(None)

    def events(self) -> AsyncIterator[TranscriptionEvent]:
        return self._iter_events()

    async def _iter_events(self) -> AsyncIterator[TranscriptionEvent]:  # type: ignore[override]
        while True:
            evt = await self._queue.get()
            if evt is None:
                return
            yield evt

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _watch_silence(self) -> None:
        """Background task: flush the buffer after ``silence_ms`` of quiet."""
        while not self._closed:
            await asyncio.sleep(0.1)
            if not self._buffer:
                continue
            elapsed_ms = (time.monotonic() - self._last_speech_ts) * 1000
            if elapsed_ms >= self._silence_ms:
                await self._flush_buffer()

    async def _flush_buffer(self) -> None:
        """Grab the current buffer and dispatch a transcription round-trip."""
        if not self._buffer:
            return
        audio = bytes(self._buffer)
        self._buffer.clear()
        # Reset silence timer so the next utterance doesn't fire immediately.
        self._last_speech_ts = time.monotonic()
        # Wait for any previous utterance task to finish before starting a
        # new one — preserves ordering of TranscriptionEvent emissions.
        if self._utterance_task is not None and not self._utterance_task.done():
            try:
                await self._utterance_task
            except Exception:  # noqa: BLE001
                pass
        self._utterance_task = asyncio.create_task(
            self._transcribe_one(audio), name="wyoming-transcribe-utterance"
        )

    async def _transcribe_one(self, audio: bytes) -> None:
        """Open a fresh Wyoming TCP connection, transcribe ``audio``, enqueue result."""
        from wyoming.asr import Transcribe, Transcript
        from wyoming.audio import AudioChunk, AudioStart, AudioStop
        from wyoming.client import AsyncTcpClient

        rate = self._config.format.sample_rate
        channels = self._config.format.channels
        language = self._config.language  # None = server auto-detects

        try:
            async with AsyncTcpClient(self._host, self._port) as client:
                # Tell the server which ASR model / language we want.
                await client.write_event(Transcribe(language=language).event())

                # Open the audio stream.
                await client.write_event(
                    AudioStart(rate=rate, width=2, channels=channels).event()
                )

                # Send audio in 1 KB chunks to avoid overwhelming the socket buffer.
                chunk_size = 1024
                for offset in range(0, len(audio), chunk_size):
                    await client.write_event(
                        AudioChunk(
                            rate=rate,
                            width=2,
                            channels=channels,
                            audio=audio[offset : offset + chunk_size],
                        ).event()
                    )

                # Signal end-of-utterance.
                await client.write_event(AudioStop().event())

                # Read the server's response — expect one Transcript event.
                while True:
                    evt = await client.read_event()
                    if evt is None:
                        # Server closed without a transcript.
                        logger.warning("wyoming-whisper: server closed without a transcript")
                        break
                    if Transcript.is_type(evt.type):
                        transcript = Transcript.from_event(evt)
                        text = (transcript.text or "").strip()
                        if text:
                            # wyoming-whisper gives us no timestamps — use 0.0.
                            await self._queue.put(
                                FinalTranscript(
                                    text=text,
                                    start_seconds=0.0,
                                    end_seconds=0.0,
                                )
                            )
                        break

        except Exception as exc:  # noqa: BLE001
            logger.error("wyoming-whisper: transcription error: %s", exc)
            if not self._closed:
                await self._queue.put(TranscriptionError(message=str(exc), recoverable=True))


class WyomingWhisperBackend(StreamingTranscriptionBackend):
    """Streaming transcription via a Wyoming-protocol Whisper server.

    Each call to ``open_stream`` returns a ``_WyomingWhisperStream`` that
    accumulates audio, detects utterance boundaries via RMS silence
    detection, and transcribes each utterance with a fresh TCP connection
    to the configured Wyoming server.

    Tested against the ``rhasspy/wyoming-whisper`` Docker image exposing
    TCP port 10300.
    """

    backend_name = "wyoming-whisper"

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="host",
                type=ToolParameterType.STRING,
                description="TCP host of the Wyoming Whisper server.",
                default=_DEFAULT_HOST,
            ),
            ConfigParam(
                key="port",
                type=ToolParameterType.INTEGER,
                description="TCP port of the Wyoming Whisper server.",
                default=str(_DEFAULT_PORT),
            ),
            ConfigParam(
                key="silence_ms",
                type=ToolParameterType.INTEGER,
                description="Milliseconds of silence to treat as end-of-utterance.",
                default=str(_DEFAULT_SILENCE_MS),
            ),
            ConfigParam(
                key="silence_rms_threshold",
                type=ToolParameterType.INTEGER,
                description="PCM s16 RMS below which a chunk counts as silence.",
                default=str(_DEFAULT_SILENCE_RMS),
            ),
        ]

    def __init__(self) -> None:
        self._host = _DEFAULT_HOST
        self._port = _DEFAULT_PORT
        self._silence_ms = _DEFAULT_SILENCE_MS
        self._silence_rms_threshold = _DEFAULT_SILENCE_RMS

    async def initialize(self, config: dict[str, object]) -> None:
        self._host = str(config.get("host", _DEFAULT_HOST))
        self._port = int(config.get("port", _DEFAULT_PORT))  # type: ignore[arg-type]
        self._silence_ms = int(config.get("silence_ms", _DEFAULT_SILENCE_MS))  # type: ignore[arg-type]
        self._silence_rms_threshold = int(
            config.get("silence_rms_threshold", _DEFAULT_SILENCE_RMS)  # type: ignore[arg-type]
        )
        logger.info(
            "wyoming-whisper initialized: %s:%d (silence %dms, rms_threshold %d)",
            self._host,
            self._port,
            self._silence_ms,
            self._silence_rms_threshold,
        )

    async def close(self) -> None:
        pass

    async def open_stream(self, config: StreamConfig) -> TranscriptionStream:
        return _WyomingWhisperStream(
            host=self._host,
            port=self._port,
            config=config,
            silence_ms=self._silence_ms,
            silence_rms_threshold=self._silence_rms_threshold,
        )
