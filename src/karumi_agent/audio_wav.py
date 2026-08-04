"""Minimal WAV encoding and decoding, standard library only.

Deliberately avoids ffmpeg. It is not installed on the target machine, and
``MediaRecorder`` in the browser produces webm/opus, which cannot be decoded
without it. So the console captures raw PCM through the Web Audio API and
encodes a WAV in JavaScript before uploading, and speech synthesis hands back a
WAV the browser can play directly. No new system dependency, and the microphone
diagnostic exercises real hardware rather than a file.

Everything here is 16-bit mono PCM, which is what the local speech models want.
"""

from __future__ import annotations

import io
import wave

import numpy as np

#: Local speech models are trained at this rate.
DEFAULT_SAMPLE_RATE = 16000


def encode_wav(samples: np.ndarray, sample_rate: int = DEFAULT_SAMPLE_RATE) -> bytes:
    """Encode float32 samples in [-1, 1] (or int16 samples) as a mono WAV."""
    if samples.dtype != np.int16:
        clipped = np.clip(samples.astype(np.float32), -1.0, 1.0)
        samples = (clipped * 32767).astype(np.int16)

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(samples.tobytes())
    return buffer.getvalue()


def decode_wav(data: bytes) -> tuple[np.ndarray, int]:
    """Decode a WAV into float32 samples in [-1, 1], plus its sample rate.

    Raises:
        ValueError: If the payload is not a mono 16-bit PCM WAV. The console
            produces exactly that, so anything else is a bug worth surfacing
            rather than silently coercing.
    """
    try:
        with wave.open(io.BytesIO(data), "rb") as handle:
            channels = handle.getnchannels()
            width = handle.getsampwidth()
            rate = handle.getframerate()
            frames = handle.readframes(handle.getnframes())
    except wave.Error as exc:
        raise ValueError(f"not a readable WAV file: {exc}") from exc

    if width != 2:
        raise ValueError(f"expected 16-bit samples, got {width * 8}-bit")

    samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        # Average the channels rather than refusing: some browsers hand back
        # stereo even when a mono constraint was requested.
        samples = samples.reshape(-1, channels).mean(axis=1)
    return samples, rate


def resample(samples: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    """Linear resampling.

    Good enough for speech recognition and it avoids pulling in scipy or soxr
    just for a diagnostic.
    """
    if source_rate == target_rate or samples.size == 0:
        return samples
    index = np.arange(0, len(samples), source_rate / target_rate)
    resampled: np.ndarray = np.interp(index, np.arange(len(samples)), samples)
    return resampled.astype(np.float32)


def duration_seconds(data: bytes) -> float:
    """Length of a WAV in seconds, or 0.0 if it cannot be read."""
    try:
        with wave.open(io.BytesIO(data), "rb") as handle:
            rate = handle.getframerate()
            return handle.getnframes() / rate if rate else 0.0
    except wave.Error:
        return 0.0
