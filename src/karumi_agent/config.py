"""Structured configuration.

Everything is settable through environment variables with a ``KARUMI_`` prefix and
``__`` as the nesting delimiter, e.g. ``KARUMI_LLM__MODEL=qwen3.5:4b``.

Hard rule: no setting here may be a secret, and every default must work offline
with no account anywhere. ``.env.example`` therefore ships with nothing required.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Where downloaded model weights and browser profiles live. Kept out of the repo.
DEFAULT_STATE_DIR = Path.home() / ".cache" / "karumi-agent"


class LLMSettings(BaseModel):
    """Local LLM served over an OpenAI-compatible endpoint (Ollama by default)."""

    base_url: str = "http://localhost:11434/v1"
    #: Ollama's native API. Used for schema-constrained decoding, which the
    #: OpenAI-compatible surface does not expose.
    native_url: str = "http://localhost:11434"

    #: Conversational model. Chosen by `karumi eval models`; see docs/BENCHMARKS.md.
    model: str = "qwen3:1.7b"
    #: Model for reasoning over page structure. Same model by default so only one
    #: set of weights is resident; split it on hardware with headroom.
    ui_model: str | None = None

    temperature: float = 0.2
    #: Must exceed system prompt + tool schemas + snapshot. Ollama defaults to 4k
    #: on small-VRAM machines, which silently truncates the accessibility tree.
    num_ctx: int = 8192
    #: -1 pins the model in memory. Reloading costs ~11s of cold prefill.
    keep_alive: int = -1
    request_timeout_s: float = 120.0

    #: Hard cap on spoken replies. Voice turns must stay short; on CPU every
    #: token is ~65ms of latency.
    max_reply_tokens: int = 90

    @property
    def effective_ui_model(self) -> str:
        return self.ui_model or self.model


class STTSettings(BaseModel):
    """Local speech-to-text."""

    #: ``auto`` prefers Parakeet when onnx-asr is importable, else Whisper.
    engine: Literal["auto", "parakeet", "whisper"] = "auto"
    #: Parakeet: onnx-asr model id. Int8 ONNX, ~670 MB, no torch/CTranslate2.
    parakeet_model: str = "nemo-parakeet-tdt-0.6b-v3"
    parakeet_quantization: str | None = "int8"
    #: Whisper fallback: distil model, ~336 MB, already wrapped by Pipecat.
    whisper_model: str = "Systran/faster-distil-whisper-small.en"
    whisper_compute_type: str = "int8"
    language: str = "en"


class TTSSettings(BaseModel):
    """Local text-to-speech."""

    engine: Literal["kokoro"] = "kokoro"
    #: Kokoro voice id, from voices-v1.0.bin.
    voice: str = "af_heart"
    #: Pipecat 1.7.0's Kokoro service exposes voice, language and model only.
    #: There is deliberately no speed setting here rather than one that is
    #: silently ignored.
    language: str = "en-us"
    sample_rate: int = 24000


class VoiceSettings(BaseModel):
    """Turn taking and barge-in."""

    vad_confidence: float = 0.7
    #: Lower means faster interruption detection.
    vad_start_secs: float = 0.12
    vad_stop_secs: float = 0.2
    vad_min_volume: float = 0.6
    #: Silence before the smart-turn analyser calls the turn complete.
    turn_stop_secs: float = 2.0
    #: LocalSmartTurnAnalyzerV3 is single-threaded by default; ~12ms per turn on CPU.
    turn_cpu_count: int = 4
    allow_interruptions: bool = True


class BrowserSettings(BaseModel):
    """Headed Chromium driven by Playwright."""

    #: The operator and audience must see the real browser, so headed is the default.
    headless: bool = False
    viewport_width: int = 1440
    viewport_height: int = 900
    #: Persistent profile: the operator signs in once and the session survives restarts.
    profile_dir: Path = DEFAULT_STATE_DIR / "chromium-profile"
    #: Downloads are diverted here rather than to the operator's Downloads folder.
    download_dir: Path = DEFAULT_STATE_DIR / "downloads"
    navigation_timeout_s: float = 30.0
    action_timeout_s: float = 15.0
    #: Quiet period after an action before we consider the page settled. Mirrors
    #: Playwright MCP; `networkidle` never fires on WebSocket-backed apps.
    settle_ms: int = 500
    #: Extra origins the page may load subresources from beyond the demo origin.
    #: Empty means "same origin only, plus whatever the demo config adds".
    extra_allowed_origins: list[str] = Field(default_factory=list)
    #: Add --disable-gpu. Needed on some hybrid-GPU laptops under WSLg.
    disable_gpu: bool = False


class SnapshotSettings(BaseModel):
    """Budgets for the page representation handed to the model."""

    max_nodes: int = 320
    #: Approximate; enforced by trimming lowest-priority nodes.
    max_tokens: int = 2200
    #: Nodes fully inside the viewport are kept before offscreen ones.
    prioritise_viewport: bool = True
    #: Include [box=...] geometry. Needed for highlight overlays.
    include_boxes: bool = True
    #: Refuse to act on a snapshot older than this.
    max_age_s: float = 30.0


class SafetySettings(BaseModel):
    """Guardrails. These are structural, not prompt-based."""

    #: Sensitive actions need an explicit spoken confirmation before running.
    require_confirmation: bool = True
    #: Never fill or read password fields, no matter what is asked.
    block_password_fields: bool = True
    #: Restrict navigation to the demo origin (plus extra_allowed_origins).
    restrict_to_origin: bool = True
    #: Upper bound on actions per user turn; contains a runaway loop.
    max_actions_per_turn: int = 4
    #: Upper bound on retries for a single failing action.
    max_action_retries: int = 2


class ServerSettings(BaseModel):
    """Local control page and WebRTC signalling."""

    host: str = "127.0.0.1"
    port: int = 7860


class DemoSettings(BaseModel):
    """What we are demoing."""

    url: str = "https://metric-master-suite505.apps.rebolt.ai/"
    #: Editorial config: goals, themes, approved messages. Never selectors.
    editorial_config: Path | None = Path("configs/metric-master-suite.yaml")
    autonomous: bool = False
    #: Caps on progressive site learning, so it can never dominate the machine.
    learn_max_pages: int = 12
    learn_max_depth: int = 2
    learn_budget_s: float = 120.0


class Settings(BaseSettings):
    """Top-level settings object."""

    model_config = SettingsConfigDict(
        env_prefix="KARUMI_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    llm: LLMSettings = Field(default_factory=LLMSettings)
    stt: STTSettings = Field(default_factory=STTSettings)
    tts: TTSSettings = Field(default_factory=TTSSettings)
    voice: VoiceSettings = Field(default_factory=VoiceSettings)
    browser: BrowserSettings = Field(default_factory=BrowserSettings)
    snapshot: SnapshotSettings = Field(default_factory=SnapshotSettings)
    safety: SafetySettings = Field(default_factory=SafetySettings)
    server: ServerSettings = Field(default_factory=ServerSettings)
    demo: DemoSettings = Field(default_factory=DemoSettings)

    log_level: str = "INFO"
    #: JSONL run log. This is the artifact to share when a run misbehaves.
    log_file: Path | None = DEFAULT_STATE_DIR / "logs" / "karumi.jsonl"
    state_dir: Path = DEFAULT_STATE_DIR

    @field_validator("log_level")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()


def load_settings() -> Settings:
    """Load settings from environment and .env."""
    return Settings()
