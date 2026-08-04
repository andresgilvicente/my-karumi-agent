"""Speech-to-text selection.

``auto`` prefers Parakeet when `onnx-asr` is importable and falls back to
Whisper. Both are fully local and free; the difference is accuracy per unit of
CPU (Parakeet wins) versus zero custom code (Whisper is already wrapped by
Pipecat).
"""

from __future__ import annotations

import importlib.util
from typing import TYPE_CHECKING

from karumi_agent.config import STTSettings
from karumi_agent.errors import DependencyError
from karumi_agent.logging import get_logger

if TYPE_CHECKING:
    from pipecat.services.stt_service import STTService

log = get_logger("stt")


def _available(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def resolve_engine(settings: STTSettings) -> str:
    """Decide which backend will actually be used."""
    if settings.engine != "auto":
        return settings.engine
    if _available("onnx_asr"):
        return "parakeet"
    if _available("faster_whisper"):
        return "whisper"
    raise DependencyError(
        "No local speech-to-text backend is installed",
        remedy="uv sync --extra parakeet",
    )


def build_stt(settings: STTSettings, *, use_cuda: bool = False) -> STTService:
    """Construct the configured speech-to-text service."""
    engine = resolve_engine(settings)

    if engine == "parakeet":
        from karumi_agent.stt.parakeet import ParakeetSTTService

        providers = ["CUDAExecutionProvider", "CPUExecutionProvider"] if use_cuda else None
        return ParakeetSTTService(
            model=settings.parakeet_model,
            quantization=settings.parakeet_quantization,
            providers=providers,
        )

    if engine == "whisper":
        try:
            from pipecat.services.whisper.stt import WhisperSTTService
        except ImportError as exc:
            raise DependencyError(
                "faster-whisper is not installed",
                remedy="uv sync --extra whisper",
                cause=str(exc),
            ) from exc

        log.info("stt: whisper", model=settings.whisper_model)
        return WhisperSTTService(
            device="cuda" if use_cuda else "cpu",
            compute_type="float16" if use_cuda else settings.whisper_compute_type,
            settings=WhisperSTTService.Settings(model=settings.whisper_model),
        )

    raise DependencyError(
        f"Unknown STT engine {engine!r}",
        remedy="Set KARUMI_STT__ENGINE to auto, parakeet or whisper",
    )
