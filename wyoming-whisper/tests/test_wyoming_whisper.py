"""Tests for the Wyoming Whisper streaming transcription backend."""

from __future__ import annotations

import asyncio
import struct
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gilbert.interfaces.transcription import (
    AudioEncoding,
    AudioFormat,
    FinalTranscript,
    StreamConfig,
    StreamingTranscriptionBackend,
    TranscriptionError,
    TranscriptionStream,
)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_backend_is_registered():
    from gilbert_plugin_wyoming_whisper import wyoming_whisper  # noqa: F401

    assert "wyoming-whisper" in StreamingTranscriptionBackend.registered_backends()


# ---------------------------------------------------------------------------
# Backend configuration
# ---------------------------------------------------------------------------


@pytest.fixture
def backend():
    from gilbert_plugin_wyoming_whisper.wyoming_whisper import WyomingWhisperBackend

    return WyomingWhisperBackend()


def test_config_params_keys(backend):
    keys = {p.key for p in backend.backend_config_params()}
    assert keys == {"host", "port", "silence_ms", "silence_rms_threshold"}


@pytest.mark.asyncio
async def test_initialize_sets_fields(backend):
    await backend.initialize({
        "host": "10.0.0.1",
        "port": 10301,
        "silence_ms": 800,
        "silence_rms_threshold": 150,
    })
    assert backend._host == "10.0.0.1"
    assert backend._port == 10301
    assert backend._silence_ms == 800
    assert backend._silence_rms_threshold == 150


@pytest.mark.asyncio
async def test_initialize_uses_defaults_on_empty(backend):
    await backend.initialize({})
    assert backend._host == "127.0.0.1"
    assert backend._port == 10300
    assert backend._silence_ms == 600
    assert backend._silence_rms_threshold == 200


# ---------------------------------------------------------------------------
# Stream construction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_stream_returns_transcription_stream(backend):
    await backend.initialize({})
    config = StreamConfig(
        format=AudioFormat(AudioEncoding.PCM_S16LE, sample_rate=16000, channels=1),
    )
    stream = await backend.open_stream(config)
    assert isinstance(stream, TranscriptionStream)
    # Clean up — cancel the background task so pytest doesn't warn.
    await stream.close()
    # Drain the sentinel so the queue is empty.
    await stream._queue.get()


# ---------------------------------------------------------------------------
# Silence detection and utterance flushing (mocked Wyoming client)
# ---------------------------------------------------------------------------


def _make_pcm_chunk(amplitude: int, num_samples: int = 160) -> bytes:
    """Build a s16le PCM chunk of ``num_samples`` all set to ``amplitude``."""
    return struct.pack(f"<{num_samples}h", *([amplitude] * num_samples))


def _make_silence_chunk(num_samples: int = 160) -> bytes:
    return _make_pcm_chunk(0, num_samples)


def _build_fake_client(transcript_text: str = "hello world") -> Any:
    """Return a mock AsyncTcpClient context manager that emits one Transcript."""
    # Build a mock Event that looks like a wyoming Transcript event.
    mock_event = MagicMock()
    mock_event.type = "transcript"

    # mock Transcript.is_type always returns True for our fake event.
    # mock Transcript.from_event returns a dataclass-like with .text set.
    mock_transcript = MagicMock()
    mock_transcript.text = transcript_text

    mock_client = AsyncMock()
    mock_client.write_event = AsyncMock()
    mock_client.read_event = AsyncMock(side_effect=[mock_event, None])

    # Context manager protocol.
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_client)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    return mock_cm, mock_client, mock_event, mock_transcript


@pytest.mark.asyncio
async def test_silence_triggers_transcription():
    """Sending speech then silence causes _transcribe_one to run and emit a FinalTranscript."""
    from gilbert_plugin_wyoming_whisper.wyoming_whisper import _WyomingWhisperStream

    mock_cm, mock_client, mock_event, mock_transcript = _build_fake_client("hey there")

    stream = _WyomingWhisperStream(
        host="127.0.0.1",
        port=10300,
        config=StreamConfig(
            format=AudioFormat(AudioEncoding.PCM_S16LE, sample_rate=16000, channels=1),
        ),
        silence_ms=100,       # short window so the test is fast
        silence_rms_threshold=200,
    )

    with (
        patch(
            "gilbert_plugin_wyoming_whisper.wyoming_whisper._WyomingWhisperStream"
            "._transcribe_one",
        ) as mock_transcribe,
    ):
        # Make the mock actually put a FinalTranscript on the queue.
        async def _fake_transcribe(audio: bytes) -> None:
            await stream._queue.put(
                FinalTranscript(text="hey there", start_seconds=0.0, end_seconds=0.0)
            )

        mock_transcribe.side_effect = _fake_transcribe

        # Send loud speech chunks.
        speech_chunk = _make_pcm_chunk(5000)  # well above threshold=200
        for _ in range(5):
            await stream.send(speech_chunk)

        # Now wait longer than silence_ms so _watch_silence fires.
        await asyncio.sleep(0.25)

        # At this point _transcribe_one should have been called and put an event.
        assert not stream._queue.empty()
        evt = stream._queue.get_nowait()
        assert isinstance(evt, FinalTranscript)
        assert evt.text == "hey there"

    # Clean up.
    stream._closed = True
    stream._silence_check_task.cancel()
    try:
        await stream._silence_check_task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_close_drains_remaining_buffer():
    """close() flushes any buffered audio before terminating the stream."""
    from gilbert_plugin_wyoming_whisper.wyoming_whisper import _WyomingWhisperStream

    flushed_audio: list[bytes] = []

    stream = _WyomingWhisperStream(
        host="127.0.0.1",
        port=10300,
        config=StreamConfig(
            format=AudioFormat(AudioEncoding.PCM_S16LE, sample_rate=16000, channels=1),
        ),
        silence_ms=60_000,    # very long — won't fire during test
        silence_rms_threshold=200,
    )

    original_transcribe = stream._transcribe_one

    async def _capturing_transcribe(audio: bytes) -> None:
        flushed_audio.append(audio)
        await stream._queue.put(
            FinalTranscript(text="captured", start_seconds=0.0, end_seconds=0.0)
        )

    stream._transcribe_one = _capturing_transcribe  # type: ignore[method-assign]

    # Push some speech.
    speech = _make_pcm_chunk(5000, 160)
    await stream.send(speech)

    # close() should flush the buffer and wait for the transcription.
    await stream.close()

    # The buffer was flushed with the speech we sent.
    assert len(flushed_audio) == 1
    assert len(flushed_audio[0]) == len(speech)

    # The sentinel None is on the queue (from close()), possibly preceded by
    # the FinalTranscript from the flush.
    items: list = []
    while not stream._queue.empty():
        items.append(stream._queue.get_nowait())
    # None must be the last item.
    assert items[-1] is None


@pytest.mark.asyncio
async def test_events_iterator_terminates_after_close():
    """events() yields all queued events then stops after close() puts None."""
    from gilbert_plugin_wyoming_whisper.wyoming_whisper import _WyomingWhisperStream

    stream = _WyomingWhisperStream(
        host="127.0.0.1",
        port=10300,
        config=StreamConfig(
            format=AudioFormat(AudioEncoding.PCM_S16LE, sample_rate=16000, channels=1),
        ),
        silence_ms=60_000,
        silence_rms_threshold=200,
    )
    # Cancel the silence watcher — we drive the queue manually.
    stream._silence_check_task.cancel()

    # Manually push two events then the sentinel.
    await stream._queue.put(FinalTranscript(text="one", start_seconds=0.0, end_seconds=0.0))
    await stream._queue.put(FinalTranscript(text="two", start_seconds=0.0, end_seconds=0.0))
    await stream._queue.put(None)

    collected = []
    async for evt in stream.events():
        collected.append(evt)

    assert len(collected) == 2
    assert collected[0].text == "one"
    assert collected[1].text == "two"


@pytest.mark.asyncio
async def test_send_after_close_is_noop():
    """Calling send() after close() should not raise or buffer data."""
    from gilbert_plugin_wyoming_whisper.wyoming_whisper import _WyomingWhisperStream

    stream = _WyomingWhisperStream(
        host="127.0.0.1",
        port=10300,
        config=StreamConfig(
            format=AudioFormat(AudioEncoding.PCM_S16LE, sample_rate=16000, channels=1),
        ),
        silence_ms=60_000,
        silence_rms_threshold=200,
    )
    stream._silence_check_task.cancel()
    stream._closed = True

    await stream.send(_make_pcm_chunk(5000))
    assert len(stream._buffer) == 0


@pytest.mark.asyncio
async def test_transcription_error_enqueued_on_connection_failure():
    """When the Wyoming server is unreachable, a TranscriptionError is emitted."""
    from gilbert_plugin_wyoming_whisper.wyoming_whisper import _WyomingWhisperStream

    stream = _WyomingWhisperStream(
        host="127.0.0.1",
        port=10300,
        config=StreamConfig(
            format=AudioFormat(AudioEncoding.PCM_S16LE, sample_rate=16000, channels=1),
        ),
        silence_ms=60_000,
        silence_rms_threshold=200,
    )
    # Cancel the silence watcher — we drive things manually.
    stream._silence_check_task.cancel()
    try:
        await stream._silence_check_task
    except asyncio.CancelledError:
        pass

    # Call _transcribe_one directly with a TCP host that refuses connections.
    # Patch AsyncTcpClient.__aenter__ to raise ConnectionRefusedError.
    with patch(
        "gilbert_plugin_wyoming_whisper.wyoming_whisper._WyomingWhisperStream"
        "._transcribe_one",
        new=None,
    ):
        pass  # just using the real _transcribe_one below

    # Patch the wyoming AsyncTcpClient used inside _transcribe_one.
    with patch(
        "wyoming.client.AsyncTcpClient.__aenter__",
        side_effect=ConnectionRefusedError("Connection refused"),
    ):
        await stream._transcribe_one(b"\x00" * 320)

    # A TranscriptionError should now be on the queue.
    assert not stream._queue.empty()
    evt = stream._queue.get_nowait()
    assert isinstance(evt, TranscriptionError)
    assert evt.recoverable is True

    stream._closed = True


# ---------------------------------------------------------------------------
# Smoke / integration test (skipped unless a real Wyoming server is running)
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.asyncio
async def test_live_transcription_smoke():
    """Connect to a real Wyoming Whisper server at 127.0.0.1:10300.

    Skipped by default.  Run with ``pytest -m slow`` to include.
    Requires a Wyoming Whisper server listening on 127.0.0.1:10300.
    """
    import socket

    # Probe before attempting.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        try:
            sock.connect(("127.0.0.1", 10300))
        except (ConnectionRefusedError, TimeoutError, OSError):
            pytest.skip("No Wyoming Whisper server running at 127.0.0.1:10300")

    from gilbert_plugin_wyoming_whisper.wyoming_whisper import WyomingWhisperBackend

    backend = WyomingWhisperBackend()
    await backend.initialize({"silence_ms": 400, "silence_rms_threshold": 100})

    config = StreamConfig(
        format=AudioFormat(AudioEncoding.PCM_S16LE, sample_rate=16000, channels=1),
        language="en",
    )
    stream = await backend.open_stream(config)

    # Send 0.5 s of mild speech-amplitude sine-like pattern (~16000 Hz, 16-bit).
    import math

    pcm_samples = [
        int(8000 * math.sin(2 * math.pi * 440 * i / 16000)) for i in range(8000)
    ]
    pcm_bytes = struct.pack(f"<{len(pcm_samples)}h", *pcm_samples)
    chunk_size = 1600  # 0.1 s per chunk
    for i in range(0, len(pcm_bytes), chunk_size):
        await stream.send(pcm_bytes[i : i + chunk_size])

    await asyncio.sleep(0.6)  # let silence watcher fire

    events_received: list = []

    async def _collect():
        async for evt in stream.events():
            events_received.append(evt)

    collect_task = asyncio.create_task(_collect())
    await stream.close()
    await asyncio.wait_for(collect_task, timeout=5.0)

    # We got at least a FinalTranscript (or a TranscriptionError if Whisper
    # couldn't decode the tone, which is fine for a smoke test).
    assert len(events_received) >= 1
