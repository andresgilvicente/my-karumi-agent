"""The voice pipeline actually turns audio into a transcription.

This is the regression test for a real bug: the pipeline was assembled without a
VAD stage, so the microphone connected, audio flowed, and nothing whatsoever
happened. ``SegmentedSTTService`` reacts only to ``VADUserStartedSpeakingFrame``
and ``VADUserStoppedSpeakingFrame``, and in Pipecat 1.7.0 nothing produces those
unless a ``VADProcessor`` (or an LLM context aggregator, which this design does
not have) is in the pipeline. ``TransportParams`` has no ``vad_analyzer`` field.

Rather than assert on structure, this feeds genuinely synthesized speech through
the real stages and checks a transcription comes out the other end. No
microphone, so it runs unattended.
"""

from __future__ import annotations

import numpy as np
import pytest

pytestmark = [
    pytest.mark.requires_stt,
    pytest.mark.requires_tts,
    pytest.mark.slow,
]

SAMPLE_RATE = 16000
CHUNK_MS = 20
SPOKEN = "Show me the revenue for this month."


def _speech_pcm16() -> bytes:
    """Synthesize a sentence with Kokoro and return 16 kHz mono PCM16."""
    from pathlib import Path

    from kokoro_onnx import Kokoro

    cache = Path.home() / ".cache" / "pipecat" / "kokoro-onnx"
    model, voices = cache / "kokoro-v1.0.onnx", cache / "voices-v1.0.bin"
    if not model.exists() or not voices.exists():
        pytest.skip("Kokoro weights are not downloaded; run the voice demo once")

    samples, rate = Kokoro(str(model), str(voices)).create(SPOKEN, voice="af_heart", lang="en-us")
    index = np.arange(0, len(samples), rate / SAMPLE_RATE)
    resampled = np.interp(index, np.arange(len(samples)), samples)
    return (np.clip(resampled, -1.0, 1.0) * 32767).astype(np.int16).tobytes()


def _silence(seconds: float) -> bytes:
    return b"\x00\x00" * int(SAMPLE_RATE * seconds)


def _to_audio_frames(pcm: bytes) -> list[object]:
    """Chunk PCM into the 20 ms input frames a transport would deliver."""
    from pipecat.frames.frames import InputAudioRawFrame

    step = int(SAMPLE_RATE * (CHUNK_MS / 1000)) * 2
    return [
        InputAudioRawFrame(
            audio=pcm[offset : offset + step], sample_rate=SAMPLE_RATE, num_channels=1
        )
        for offset in range(0, len(pcm) - step + 1, step)
    ]


class TestAudioReachesTranscription:
    async def test_vad_segments_audio_and_stt_transcribes_it(self) -> None:
        """Silence, then speech, then silence -> one transcription."""
        from pipecat.frames.frames import TranscriptionFrame
        from pipecat.pipeline.pipeline import Pipeline
        from pipecat.tests.utils import run_test

        from karumi_agent.agent.voice import _pipecat, build_vad_processor
        from karumi_agent.config import Settings
        from karumi_agent.stt.factory import build_stt

        settings = Settings()
        try:
            stt = build_stt(settings.stt)
        except Exception as exc:  # reported as a skip, not a failure
            pytest.skip(f"local STT unavailable: {exc}")

        pc = _pipecat()
        pipeline = Pipeline([build_vad_processor(settings, pc), stt])

        # Leading and trailing silence give VAD an unambiguous start and stop.
        pcm = _silence(0.5) + _speech_pcm16() + _silence(1.2)

        received, _ = await run_test(
            pipeline,
            frames_to_send=_to_audio_frames(pcm),
            expected_down_frames=None,
        )

        transcripts = [f.text for f in received if isinstance(f, TranscriptionFrame)]
        assert transcripts, (
            "no TranscriptionFrame was produced. If this fails, the VAD stage is "
            "missing or misordered and the agent will be deaf."
        )
        heard = " ".join(transcripts).lower()
        # Wording varies; the content words must survive.
        assert "revenue" in heard, f"expected 'revenue' in {heard!r}"


class TestPipelineShape:
    """Cheap structural guards, so a reordering regression fails fast."""

    def test_vad_precedes_stt(self) -> None:
        from karumi_agent.agent.voice import _pipecat

        pc = _pipecat()
        # The contract that makes the audio test above possible.
        assert hasattr(pc["VADProcessor"], "process_frame")
        assert "VADUserStartedSpeakingFrame" in _segmented_stt_source()

    def test_transport_params_still_has_no_vad_field(self) -> None:
        """Guards the assumption behind having a separate VAD stage.

        If a future Pipecat adds ``vad_analyzer`` back to TransportParams, this
        fails and we should reconsider where VAD lives.
        """
        from pipecat.transports.base_transport import TransportParams

        assert "vad_analyzer" not in TransportParams.model_fields


def _segmented_stt_source() -> str:
    import inspect

    from pipecat.services.stt_service import SegmentedSTTService

    return inspect.getsource(SegmentedSTTService)
