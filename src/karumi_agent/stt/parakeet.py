"""Parakeet speech-to-text via onnx-asr.

Pipecat ships no Parakeet service, so this is a ~60-line ``SegmentedSTTService``
subclass. It is worth writing because of what it avoids: `onnx-asr` runs
Parakeet on plain ONNX Runtime with numpy, and needs neither PyTorch, nor NeMo,
nor CTranslate2. That last one matters beyond dependency weight -- CTranslate2
(which `faster-whisper` uses) is still on CUDA 12 while the rest of a modern GPU
stack has moved to CUDA 13, and the documented failure mode there is a *silent*
fallback to CPU. Staying on ONNX Runtime keeps one coherent stack on both the
CPU-only machine this was built on and a future GPU one.

Segmented, not streaming: Pipecat gates on voice activity and hands us complete
utterances, and no local STT service in Pipecat 1.7.0 emits partials either.
"""

from __future__ import annotations

import functools
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

import numpy as np

from karumi_agent.errors import DependencyError
from karumi_agent.logging import get_logger

if TYPE_CHECKING:
    from pipecat.frames.frames import Frame

log = get_logger("stt")

#: Pipecat delivers 16 kHz mono PCM to STT services.
_SAMPLE_RATE = 16000


def _load_pipecat_base() -> Any:
    from pipecat.services.stt_service import SegmentedSTTService

    return SegmentedSTTService


_Base: Any = _load_pipecat_base()


class ParakeetSTTService(_Base):  # type: ignore[misc]
    """NVIDIA Parakeet TDT via onnx-asr, on CPU or CUDA."""

    def __init__(
        self,
        *,
        model: str = "nemo-parakeet-tdt-0.6b-v3",
        quantization: str | None = "int8",
        providers: list[str] | None = None,
        language: str | None = "en",
        **kwargs: Any,
    ) -> None:
        # Pipecat warns loudly if a service leaves settings fields NOT_GIVEN, so
        # declare them explicitly even though the model is fixed at load time.
        from pipecat.services.settings import STTSettings

        kwargs.setdefault("settings", STTSettings(model=model, language=language))
        super().__init__(**kwargs)
        try:
            import onnx_asr
        except ImportError as exc:
            raise DependencyError(
                "onnx-asr is not installed",
                remedy="uv sync --extra parakeet",
                cause=str(exc),
            ) from exc

        self._model_name = model
        log.info("loading parakeet", model=model, quantization=quantization)
        try:
            self._model = onnx_asr.load_model(
                model,
                quantization=quantization,
                providers=providers,
            )
        except Exception as exc:
            raise DependencyError(
                f"Could not load Parakeet model {model!r}",
                remedy=(
                    "First use downloads ~670 MB from Hugging Face; check "
                    "connectivity, or set KARUMI_STT__ENGINE=whisper"
                ),
                cause=str(exc),
            ) from exc
        log.info("parakeet ready", model=model)

    def can_generate_metrics(self) -> bool:
        return True

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame | None, None]:
        """Transcribe one complete utterance."""
        from pipecat.frames.frames import ErrorFrame, TranscriptionFrame
        from pipecat.utils.time import time_now_iso8601

        if not audio:
            yield None
            return

        await self.start_processing_metrics()
        await self.start_ttfb_metrics()
        try:
            # int16 PCM -> float32 in [-1, 1], which is what onnx-asr expects.
            samples = np.frombuffer(audio, dtype=np.int16).astype(np.float32) / 32768.0
            # sample_rate is keyword-only on onnx-asr's recognize(); passing it
            # positionally raises "takes 2 positional arguments but 3 were given"
            # and silently turns every utterance into an error frame.
            recognize = functools.partial(self._model.recognize, samples, sample_rate=_SAMPLE_RATE)
            text = await self.run_in_executor(recognize)
        except Exception as exc:  # surfaced as a frame, never fatal
            log.warning(f"parakeet failed: {type(exc).__name__}: {exc}")
            yield ErrorFrame(f"Speech recognition failed: {exc}")
            return
        finally:
            await self.stop_ttfb_metrics()
            await self.stop_processing_metrics()

        transcript = (text or "").strip()
        if not transcript:
            yield None
            return

        log.debug("transcribed", text=transcript[:120])
        yield TranscriptionFrame(transcript, "", time_now_iso8601())

    async def run_in_executor(self, fn: Any) -> Any:
        """Run blocking inference off the event loop.

        Takes a zero-argument callable so keyword arguments survive; bind them
        with ``functools.partial`` at the call site.
        """
        import asyncio

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, fn)
