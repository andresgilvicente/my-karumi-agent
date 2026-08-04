"""Text-to-speech selection.

Kokoro is the default: Apache-2.0 weights, ~337 MB, already wrapped by Pipecat,
and it yields audio in chunks so speech starts before the sentence is finished.

Piper is deliberately **not** the default despite being a common choice. Its
package is GPL-3.0-or-later, which is a real constraint on redistributing this
project, and Kokoro sounds better in English anyway. Anyone who wants it can
select it, and the licence implication is theirs to accept.

Kokoro has one non-obvious system dependency: it phonemises through `espeak-ng`,
which is an apt package, not a Python one. `karumi doctor` checks for it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from karumi_agent.config import TTSSettings
from karumi_agent.errors import DependencyError
from karumi_agent.logging import get_logger

if TYPE_CHECKING:
    from pipecat.services.tts_service import TTSService

log = get_logger("tts")


class TTSFactory(Protocol):
    """Anything that can build a Pipecat TTS service."""

    def __call__(self, settings: TTSSettings) -> TTSService: ...


def build_tts(settings: TTSSettings) -> TTSService:
    """Construct the configured text-to-speech service."""
    if settings.engine == "kokoro":
        return _build_kokoro(settings)
    raise DependencyError(
        f"Unknown TTS engine {settings.engine!r}",
        remedy="Set KARUMI_TTS__ENGINE=kokoro",
    )


def _build_kokoro(settings: TTSSettings) -> TTSService:
    try:
        from pipecat.services.kokoro.tts import KokoroTTSService
    except ImportError as exc:
        raise DependencyError(
            "Kokoro TTS is not installed",
            remedy="uv sync",
            cause=str(exc),
        ) from exc

    import shutil

    if not (shutil.which("espeak-ng") or shutil.which("espeak")):
        # Fail here with a clear message rather than deep inside kokoro-onnx,
        # where the error is an unhelpful phonemiser exception.
        raise DependencyError(
            "Kokoro needs espeak-ng to turn text into phonemes",
            remedy="sudo apt-get install -y espeak-ng",
        )

    log.info("tts: kokoro", voice=settings.voice)
    # Weights download to ~/.cache/pipecat/kokoro-onnx on first use (~337 MB).
    return KokoroTTSService(
        settings=KokoroTTSService.Settings(voice=settings.voice),
        sample_rate=settings.sample_rate,
    )
