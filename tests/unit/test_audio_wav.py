"""WAV encoding, decoding and resampling.

These exist because the console encodes WAV in JavaScript and the server decodes
it in Python, and a mismatch between the two would present as "speech
recognition heard nothing" -- indistinguishable from a broken microphone.
"""

from __future__ import annotations

import struct

import numpy as np
import pytest

from karumi_agent.audio_wav import (
    DEFAULT_SAMPLE_RATE,
    decode_wav,
    duration_seconds,
    encode_wav,
    resample,
)


def _tone(seconds: float, hz: float = 440.0, rate: int = DEFAULT_SAMPLE_RATE) -> np.ndarray:
    t = np.linspace(0, seconds, int(rate * seconds), endpoint=False)
    return (0.5 * np.sin(2 * np.pi * hz * t)).astype(np.float32)


class TestRoundTrip:
    def test_float_survives_encode_decode(self) -> None:
        original = _tone(0.25)
        samples, rate = decode_wav(encode_wav(original))
        assert rate == DEFAULT_SAMPLE_RATE
        assert len(samples) == len(original)
        # 16-bit quantisation is the only loss.
        assert np.max(np.abs(samples - original)) < 1e-3

    def test_int16_input_is_accepted(self) -> None:
        pcm = (_tone(0.1) * 32767).astype(np.int16)
        samples, rate = decode_wav(encode_wav(pcm))
        assert rate == DEFAULT_SAMPLE_RATE
        assert len(samples) == len(pcm)

    def test_values_are_clipped_not_wrapped(self) -> None:
        """A sample above 1.0 must saturate, not wrap to a loud negative."""
        loud = np.array([2.0, -2.0, 0.0], dtype=np.float32)
        samples, _ = decode_wav(encode_wav(loud))
        assert samples[0] > 0.99
        assert samples[1] < -0.99


class TestHeader:
    def test_output_is_a_riff_wave(self) -> None:
        data = encode_wav(_tone(0.1))
        assert data[:4] == b"RIFF"
        assert data[8:12] == b"WAVE"

    def test_declares_mono_16_bit(self) -> None:
        data = encode_wav(_tone(0.1))
        channels = struct.unpack_from("<H", data, 22)[0]
        bits = struct.unpack_from("<H", data, 34)[0]
        assert channels == 1
        assert bits == 16

    def test_duration_matches(self) -> None:
        assert duration_seconds(encode_wav(_tone(0.5))) == pytest.approx(0.5, abs=0.01)

    def test_duration_of_garbage_is_zero_not_an_exception(self) -> None:
        assert duration_seconds(b"not a wav") == 0.0


class TestDecodeFailures:
    def test_garbage_raises_a_clear_error(self) -> None:
        with pytest.raises(ValueError, match="not a readable WAV"):
            decode_wav(b"definitely not audio")

    def test_stereo_is_averaged_rather_than_refused(self) -> None:
        """Some browsers hand back stereo despite a mono constraint."""
        import io
        import wave

        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as handle:
            handle.setnchannels(2)
            handle.setsampwidth(2)
            handle.setframerate(DEFAULT_SAMPLE_RATE)
            frames = np.zeros(200, dtype=np.int16)
            frames[::2] = 16000  # left loud, right silent
            handle.writeframes(frames.tobytes())

        samples, rate = decode_wav(buffer.getvalue())
        assert rate == DEFAULT_SAMPLE_RATE
        assert len(samples) == 100


class TestResample:
    def test_same_rate_is_a_no_op(self) -> None:
        original = _tone(0.1)
        assert resample(original, 16000, 16000) is original

    def test_downsampling_shortens_proportionally(self) -> None:
        original = _tone(1.0, rate=48000)
        out = resample(original, 48000, 16000)
        assert len(out) == pytest.approx(len(original) / 3, rel=0.01)

    def test_empty_input_is_safe(self) -> None:
        assert resample(np.array([], dtype=np.float32), 48000, 16000).size == 0

    def test_browser_rate_reaches_the_model_rate(self) -> None:
        """48 kHz is what a browser AudioContext typically gives us."""
        out = resample(_tone(0.5, rate=48000), 48000, DEFAULT_SAMPLE_RATE)
        assert len(out) == pytest.approx(DEFAULT_SAMPLE_RATE * 0.5, rel=0.01)
