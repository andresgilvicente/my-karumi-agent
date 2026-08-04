"""Subsystem probes for the diagnostic console.

Two strictly separated tiers, and the separation is the point:

* **Cheap checks** (:func:`cheap_checks`) answer "is this configured and
  present?" using file existence, ``PATH`` lookups, ``/proc`` reads and one
  small HTTP GET. They must never load a model, launch a browser, initialise a
  speech engine, or generate anything. They back ``GET /api/diag``, which the
  console polls, so the console stays responsive and starts instantly.

* **Expensive probes** (:func:`probe_*`) actually exercise a subsystem and only
  ever run when the operator clicks its Test button.

Every probe returns a :class:`ProbeResult` rather than raising, because the
console's job is to make failure *visible*. A probe that throws produces a
stack trace nobody sees; a probe that returns ``status=fail`` with a remedy
produces a row on screen and a line in the terminal.
"""

from __future__ import annotations

import os
import platform
import sys
import time
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Any

import numpy as np

from karumi_agent.audio_wav import DEFAULT_SAMPLE_RATE, decode_wav, encode_wav, resample
from karumi_agent.config import Settings
from karumi_agent.domain.safety import redact
from karumi_agent.hardware import Check, CheckStatus, detect_hardware, run_all_checks
from karumi_agent.logging import get_logger

log = get_logger("diagnostics")

#: Spoken by the text-to-speech probe. Fixed, English, and long enough to judge
#: quality by ear rather than merely confirming that bytes arrived.
TTS_PROBE_SENTENCE = (
    "Speech synthesis is working. This is the Karumi agent speaking from your local machine."
)

#: A port nothing listens on, used to prove the failure path deterministically.
UNREACHABLE_URL = "http://127.0.0.1:1"

# Loaded models are cached so a second Test click is fast. Populated only by the
# expensive probes; a cheap check never touches these.
_stt_model: Any = None
_tts_engine: Any = None


@dataclass(slots=True)
class ProbeResult:
    """The outcome of one expensive probe."""

    name: str
    status: CheckStatus
    detail: str
    remedy: str | None = None
    elapsed_ms: float = 0.0
    #: Extra structured facts (model name, version, transcript, ...).
    data: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "status": self.status.value,
            "detail": self.detail,
            "elapsed_ms": round(self.elapsed_ms, 1),
        }
        if self.remedy:
            payload["remedy"] = self.remedy
        if self.data:
            payload["data"] = self.data
        return payload

    @classmethod
    def failed(
        cls, name: str, exc: BaseException, *, remedy: str | None = None, elapsed_ms: float = 0.0
    ) -> ProbeResult:
        return cls(
            name=name,
            status=CheckStatus.FAIL,
            detail=f"{type(exc).__name__}: {exc}"[:400],
            remedy=remedy,
            elapsed_ms=elapsed_ms,
        )


# ---------------------------------------------------------------------------
# Cheap checks
# ---------------------------------------------------------------------------
def cheap_checks(settings: Settings) -> list[Check]:
    """Availability and configuration only.

    Everything here is a filesystem or environment read, plus one small GET to
    Ollama's ``/api/tags``. Nothing is loaded, launched or generated.
    """
    profile = detect_hardware(settings.state_dir)
    models = sorted({settings.llm.model, settings.llm.effective_ui_model})
    checks = run_all_checks(
        profile,
        ollama_url=settings.llm.native_url,
        models=models,
        stt_engine=settings.stt.engine,
    )
    checks.append(_check_speech_weights())
    checks.append(
        Check(
            "page",
            CheckStatus.WARN,
            "no website opened yet (milestone 1)",
            remedy="Opening a site is not part of milestone 0.",
        )
    )
    return checks


def _check_speech_weights() -> Check:
    """Are the speech model files already on disk?

    Reports *only* what exists. Downloading is the probe's job, not this one's,
    and the distinction matters: the first Test STT click can take minutes.
    """
    kokoro = Path.home() / ".cache" / "pipecat" / "kokoro-onnx"
    has_kokoro = (kokoro / "kokoro-v1.0.onnx").exists() and (kokoro / "voices-v1.0.bin").exists()
    hub = Path.home() / ".cache" / "huggingface" / "hub"
    has_parakeet = any(hub.glob("models--*parakeet*")) if hub.exists() else False

    if has_kokoro and has_parakeet:
        return Check("speech weights", CheckStatus.OK, "Kokoro and Parakeet are on disk")
    missing = [
        name
        for name, present in (
            ("Kokoro (~337 MB)", has_kokoro),
            ("Parakeet (~670 MB)", has_parakeet),
        )
        if not present
    ]
    return Check(
        "speech weights",
        CheckStatus.WARN,
        f"not downloaded yet: {', '.join(missing)}",
        remedy="Click Test TTS / Test STT once; the first click downloads them.",
    )


# ---------------------------------------------------------------------------
# Expensive probes
# ---------------------------------------------------------------------------
async def probe_browser(settings: Settings) -> ProbeResult:
    """Launch a headed Chromium, read its version, close it.

    Isolates the single most environment-sensitive dependency: missing system
    libraries, or WSLg failing to provide a display.
    """
    started = time.perf_counter()
    remedy = (
        "Run `uv run playwright install chromium`. On Linux you may also need "
        "`sudo apt-get install -y libnss3 libnspr4`. If the window cannot open, "
        "try KARUMI_BROWSER__DISABLE_GPU=true."
    )
    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            args = ["--no-first-run", "--no-default-browser-check"]
            if settings.browser.disable_gpu:
                args += ["--disable-gpu", "--use-gl=swiftshader"]
            browser = await pw.chromium.launch(headless=settings.browser.headless, args=args)
            try:
                version = browser.version
                page = await browser.new_page()
                await page.goto("about:blank")
                await page.close()
            finally:
                await browser.close()
    except Exception as exc:  # surfaced on screen, never raised
        elapsed = (time.perf_counter() - started) * 1000
        log.warning(f"browser probe failed: {type(exc).__name__}: {exc}")
        return ProbeResult.failed("browser", exc, remedy=remedy, elapsed_ms=elapsed)

    elapsed = (time.perf_counter() - started) * 1000
    mode = "headless" if settings.browser.headless else "headed"
    log.info("browser probe ok", version=version, mode=mode)
    return ProbeResult(
        name="browser",
        status=CheckStatus.OK,
        detail=f"Chromium {version} launched {mode} and closed cleanly",
        elapsed_ms=elapsed,
        data={"version": version, "mode": mode},
    )


async def probe_llm(settings: Settings, *, simulate_unreachable: bool = False) -> ProbeResult:
    """Generate a few tokens and report time-to-first-token.

    ``simulate_unreachable`` points the client at a dead port so the failure
    path can be demonstrated deterministically. Stopping Ollama is not a
    reliable substitute: ``ollama stop`` only unloads the model, and the next
    request simply reloads it.
    """
    from karumi_agent.llm.ollama import OllamaClient

    started = time.perf_counter()
    llm_settings = settings.llm
    if simulate_unreachable:
        llm_settings = settings.llm.model_copy(update={"native_url": UNREACHABLE_URL})

    remedy = (
        "Install Ollama from https://ollama.com, run `ollama serve`, then "
        f"`ollama pull {settings.llm.effective_ui_model}`."
    )
    client = OllamaClient(llm_settings)
    try:
        schema = {
            "type": "object",
            "properties": {"reply": {"type": "string"}},
            "required": ["reply"],
        }
        response = await client.structured(
            model=llm_settings.effective_ui_model,
            system="You are a diagnostic. Answer in one short sentence.",
            user="Say that the local language model is working.",
            schema=schema,
        )
    except Exception as exc:
        elapsed = (time.perf_counter() - started) * 1000
        log.warning(f"llm probe failed: {type(exc).__name__}: {exc}")
        return ProbeResult.failed("llm", exc, remedy=remedy, elapsed_ms=elapsed)
    finally:
        await client.aclose()

    elapsed = (time.perf_counter() - started) * 1000
    reply = str(response.data.get("reply", ""))[:200]
    ttft = response.timing.ttft_s
    log.info("llm probe ok", model=llm_settings.effective_ui_model, ttft=ttft)
    return ProbeResult(
        name="llm",
        status=CheckStatus.OK,
        detail=f"{llm_settings.effective_ui_model} replied: {reply}",
        elapsed_ms=elapsed,
        data={
            "model": llm_settings.effective_ui_model,
            "reply": reply,
            "ttft_s": round(ttft, 3) if ttft is not None else None,
            "tokens_per_s": round(response.timing.tokens_per_s, 1),
        },
    )


def probe_tts(settings: Settings, *, text: str = TTS_PROBE_SENTENCE) -> tuple[ProbeResult, bytes]:
    """Synthesize a fixed English sentence and return it as a WAV.

    Uses the product factory first so real dependency errors (a missing
    ``espeak-ng``, absent weights) surface exactly as they would in the demo,
    then drives the underlying Kokoro engine directly. Pipecat's service needs a
    running pipeline's task manager, which a one-shot diagnostic has no business
    constructing.
    """
    global _tts_engine
    started = time.perf_counter()
    remedy = (
        "Install espeak-ng (`sudo apt-get install -y espeak-ng`). The first run "
        "also downloads ~337 MB of Kokoro weights."
    )
    try:
        from kokoro_onnx import Kokoro

        from karumi_agent.tts.factory import build_tts

        cache = Path.home() / ".cache" / "pipecat" / "kokoro-onnx"
        model_file, voices_file = cache / "kokoro-v1.0.onnx", cache / "voices-v1.0.bin"
        if not (model_file.exists() and voices_file.exists()):
            # Constructing the service performs the download and validates deps.
            build_tts(settings.tts)
        if _tts_engine is None:
            _tts_engine = Kokoro(str(model_file), str(voices_file))

        samples, rate = _tts_engine.create(
            text, voice=settings.tts.voice, lang=settings.tts.language
        )
        wav = encode_wav(np.asarray(samples, dtype=np.float32), rate)
    except Exception as exc:
        elapsed = (time.perf_counter() - started) * 1000
        log.warning(f"tts probe failed: {type(exc).__name__}: {exc}")
        return ProbeResult.failed("tts", exc, remedy=remedy, elapsed_ms=elapsed), b""

    elapsed = (time.perf_counter() - started) * 1000
    seconds = len(samples) / rate
    log.info("tts probe ok", voice=settings.tts.voice, seconds=round(seconds, 2))
    return (
        ProbeResult(
            name="tts",
            status=CheckStatus.OK,
            detail=f"synthesized {seconds:.1f}s of audio with voice {settings.tts.voice!r}",
            elapsed_ms=elapsed,
            data={
                "voice": settings.tts.voice,
                "seconds": round(seconds, 2),
                "sample_rate": rate,
                "text": text,
            },
        ),
        wav,
    )


def probe_stt(settings: Settings, wav_bytes: bytes) -> ProbeResult:
    """Transcribe a WAV recorded from the operator's real microphone."""
    global _stt_model
    started = time.perf_counter()
    remedy = "Run `uv sync --extra parakeet`. The first run downloads ~670 MB of Parakeet weights."
    try:
        from karumi_agent.stt.factory import resolve_engine

        samples, rate = decode_wav(wav_bytes)
        if samples.size == 0:
            return ProbeResult(
                name="stt",
                status=CheckStatus.FAIL,
                detail="the recording was empty",
                remedy="Check the microphone is not muted, then record again.",
                elapsed_ms=(time.perf_counter() - started) * 1000,
            )
        samples = resample(samples, rate, DEFAULT_SAMPLE_RATE)

        engine = resolve_engine(settings.stt)
        if _stt_model is None:
            _stt_model = _load_stt_model(settings, engine)
        text = _transcribe(_stt_model, engine, samples)
    except Exception as exc:
        elapsed = (time.perf_counter() - started) * 1000
        log.warning(f"stt probe failed: {type(exc).__name__}: {exc}")
        return ProbeResult.failed("stt", exc, remedy=remedy, elapsed_ms=elapsed)

    elapsed = (time.perf_counter() - started) * 1000
    audio_seconds = len(samples) / DEFAULT_SAMPLE_RATE
    transcript = (text or "").strip()
    log.info("stt probe", transcript=transcript[:120], seconds=round(audio_seconds, 2))

    if not transcript:
        return ProbeResult(
            name="stt",
            status=CheckStatus.WARN,
            detail=f"heard {audio_seconds:.1f}s of audio but produced no words",
            remedy="Speak closer to the microphone and record again.",
            elapsed_ms=elapsed,
            data={"audio_seconds": round(audio_seconds, 2)},
        )
    return ProbeResult(
        name="stt",
        status=CheckStatus.OK,
        detail=f"heard: {transcript}",
        elapsed_ms=elapsed,
        data={
            "transcript": transcript,
            "audio_seconds": round(audio_seconds, 2),
            "engine": resolve_engine(settings.stt),
        },
    )


def _load_stt_model(settings: Settings, engine: str) -> Any:
    """Load the recognition model directly, without Pipecat's service wrapper."""
    if engine == "parakeet":
        import onnx_asr

        return onnx_asr.load_model(
            settings.stt.parakeet_model, quantization=settings.stt.parakeet_quantization
        )
    from faster_whisper import WhisperModel

    return WhisperModel(
        settings.stt.whisper_model, device="cpu", compute_type=settings.stt.whisper_compute_type
    )


def _transcribe(model: Any, engine: str, samples: np.ndarray) -> str:
    if engine == "parakeet":
        return str(model.recognize(samples, sample_rate=DEFAULT_SAMPLE_RATE))
    segments, _ = model.transcribe(samples, language="en")
    return " ".join(segment.text for segment in segments)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
def _package_versions() -> dict[str, str]:
    names = ("pipecat-ai", "playwright", "onnx-asr", "kokoro-onnx", "numpy", "fastapi")
    versions: dict[str, str] = {}
    for name in names:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = "not installed"
    return versions


def _safe_config(settings: Settings) -> dict[str, Any]:
    """Configuration worth sharing.

    The project has no secrets by design, but this is passed through the same
    redaction used for page text so a future setting cannot leak by accident.
    """
    config = {
        "llm_model": settings.llm.effective_ui_model,
        "llm_url": settings.llm.native_url,
        "stt_engine": settings.stt.engine,
        "tts_voice": settings.tts.voice,
        "browser_headless": settings.browser.headless,
        "server_port": settings.server.port,
    }
    return {
        key: redact(value) if isinstance(value, str) else value for key, value in config.items()
    }


def build_export(settings: Settings, probes: dict[str, ProbeResult]) -> dict[str, Any]:
    """Everything needed to diagnose this machine, in one shareable object."""
    profile = detect_hardware(settings.state_dir)
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "milestone": 0,
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "is_wsl": profile.is_wsl,
            "display": bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")),
            "packages": _package_versions(),
        },
        "hardware": profile.as_dict(),
        "config": _safe_config(settings),
        "checks": [check.as_dict() for check in cheap_checks(settings)],
        "probes": {name: result.as_dict() for name, result in probes.items()},
    }
