"""Hardware and dependency detection.

Everything here is best-effort and never raises: the point is to *report* what is
available so the agent can pick a profile and ``karumi doctor`` can tell the
operator exactly what to install. Nothing is assumed — CUDA in particular is
probed, never inferred from the presence of a package.
"""

from __future__ import annotations

import importlib.util
import os
import platform
import shutil
import subprocess
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import httpx


class CheckStatus(StrEnum):
    """Outcome of a single doctor check."""

    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class Check:
    """One diagnostic result."""

    name: str
    status: CheckStatus
    detail: str
    remedy: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload = {"name": self.name, "status": self.status.value, "detail": self.detail}
        if self.remedy:
            payload["remedy"] = self.remedy
        return payload


@dataclass(frozen=True, slots=True)
class GPUInfo:
    """A CUDA device, if one is genuinely usable."""

    name: str
    total_memory_mb: int
    driver_version: str | None = None
    compute_capability: str | None = None


@dataclass(slots=True)
class HardwareProfile:
    """What this machine can actually do."""

    platform: str
    is_wsl: bool
    cpu_model: str
    physical_cores: int
    logical_cores: int
    total_ram_mb: int
    available_ram_mb: int
    free_disk_gb: float
    gpus: list[GPUInfo] = field(default_factory=list)
    has_display: bool = False

    @property
    def has_cuda(self) -> bool:
        return bool(self.gpus)

    @property
    def profile_name(self) -> str:
        """Which tuning profile applies."""
        return "cuda" if self.has_cuda else "cpu"

    def as_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "is_wsl": self.is_wsl,
            "cpu_model": self.cpu_model,
            "physical_cores": self.physical_cores,
            "logical_cores": self.logical_cores,
            "total_ram_mb": self.total_ram_mb,
            "available_ram_mb": self.available_ram_mb,
            "free_disk_gb": round(self.free_disk_gb, 1),
            "gpus": [{"name": g.name, "vram_mb": g.total_memory_mb} for g in self.gpus],
            "has_display": self.has_display,
            "profile": self.profile_name,
        }


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------
def _read_cpu_model() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "unknown"


def _read_physical_cores() -> int:
    """Count physical cores, falling back to logical when we cannot tell."""
    try:
        text = Path("/proc/cpuinfo").read_text(encoding="utf-8")
    except OSError:
        return os.cpu_count() or 1
    # (physical id, core id) pairs uniquely identify a physical core.
    seen: set[tuple[str, str]] = set()
    physical_id = core_id = ""
    for line in text.splitlines():
        if line.startswith("physical id"):
            physical_id = line.split(":", 1)[1].strip()
        elif line.startswith("core id"):
            core_id = line.split(":", 1)[1].strip()
            seen.add((physical_id, core_id))
    return len(seen) or os.cpu_count() or 1


def _read_meminfo() -> tuple[int, int]:
    """Return (total_mb, available_mb)."""
    total = available = 0
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                total = int(line.split()[1]) // 1024
            elif line.startswith("MemAvailable:"):
                available = int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return total, available


def _detect_wsl() -> bool:
    if "WSL_DISTRO_NAME" in os.environ or "WSL_INTEROP" in os.environ:
        return True
    try:
        return "microsoft" in Path("/proc/version").read_text(encoding="utf-8").lower()
    except OSError:
        return False


def _detect_display() -> bool:
    """Can we open a window? Under WSL this means WSLg is up."""
    if os.environ.get("WAYLAND_DISPLAY"):
        return True
    if os.environ.get("DISPLAY"):
        # DISPLAY can be set without a live server; check the socket too.
        return Path("/tmp/.X11-unix").exists() or Path("/mnt/wslg").exists()  # noqa: S108
    return False


def _detect_gpus() -> list[GPUInfo]:
    """Probe for CUDA devices via nvidia-smi.

    We deliberately shell out rather than importing torch: torch may be absent, and
    its presence would not prove a *working* driver anyway.
    """
    binary = shutil.which("nvidia-smi")
    if binary is None:
        return []
    try:
        result = subprocess.run(  # noqa: S603
            [
                binary,
                "--query-gpu=name,memory.total,driver_version,compute_cap",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return []
    if result.returncode != 0:
        return []

    gpus: list[GPUInfo] = []
    for line in result.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            continue
        try:
            memory = int(float(parts[1]))
        except ValueError:
            continue
        gpus.append(
            GPUInfo(
                name=parts[0],
                total_memory_mb=memory,
                driver_version=parts[2] if len(parts) > 2 else None,
                compute_capability=parts[3] if len(parts) > 3 else None,
            )
        )
    return gpus


def detect_hardware(state_dir: Path | None = None) -> HardwareProfile:
    """Build a profile of this machine."""
    total_ram, available_ram = _read_meminfo()
    disk_target = state_dir if state_dir and state_dir.exists() else Path.home()
    try:
        usage = shutil.disk_usage(disk_target)
        free_gb = usage.free / (1024**3)
    except OSError:
        free_gb = 0.0

    return HardwareProfile(
        platform=f"{platform.system()} {platform.release()}",
        is_wsl=_detect_wsl(),
        cpu_model=_read_cpu_model(),
        physical_cores=_read_physical_cores(),
        logical_cores=os.cpu_count() or 1,
        total_ram_mb=total_ram,
        available_ram_mb=available_ram,
        free_disk_gb=free_gb,
        gpus=_detect_gpus(),
        has_display=_detect_display(),
    )


# ---------------------------------------------------------------------------
# Dependency checks
# ---------------------------------------------------------------------------
def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def check_display(profile: HardwareProfile) -> Check:
    if profile.has_display:
        server = "WSLg" if profile.is_wsl else "X11/Wayland"
        return Check("display", CheckStatus.OK, f"{server} available; headed Chromium will show")
    return Check(
        "display",
        CheckStatus.FAIL,
        "No DISPLAY or WAYLAND_DISPLAY; a headed browser cannot open",
        remedy=(
            "On WSL2, update Windows and WSL (`wsl --update`) so WSLg works. "
            "Otherwise run with KARUMI_BROWSER__HEADLESS=true (the audience will "
            "not see the browser)."
        ),
    )


def check_chromium() -> Check:
    if not _module_available("playwright"):
        return Check(
            "chromium",
            CheckStatus.FAIL,
            "playwright is not installed",
            remedy="uv sync",
        )
    cache = Path(
        os.environ.get("PLAYWRIGHT_BROWSERS_PATH", Path.home() / ".cache" / "ms-playwright")
    )
    if cache.exists() and any(cache.glob("chromium-*")):
        found = sorted(p.name for p in cache.glob("chromium-*"))
        return Check("chromium", CheckStatus.OK, f"installed: {', '.join(found)}")
    return Check(
        "chromium",
        CheckStatus.FAIL,
        "Playwright's Chromium is not downloaded",
        remedy="uv run playwright install chromium",
    )


def check_espeak() -> Check:
    """Kokoro phonemises through espeak-ng, which is a system package."""
    if shutil.which("espeak-ng") or shutil.which("espeak"):
        return Check("espeak-ng", CheckStatus.OK, "found on PATH")
    return Check(
        "espeak-ng",
        CheckStatus.FAIL,
        "espeak-ng missing; Kokoro TTS cannot phonemise text",
        remedy="sudo apt-get install -y espeak-ng",
    )


def check_ollama(base_url: str, expected_models: list[str]) -> list[Check]:
    """Check the Ollama daemon is up and the models we need are pulled."""
    checks: list[Check] = []
    tags_url = base_url.rstrip("/") + "/api/tags"
    try:
        response = httpx.get(tags_url, timeout=5.0)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        checks.append(
            Check(
                "ollama",
                CheckStatus.FAIL,
                f"Ollama is not reachable at {base_url} ({type(exc).__name__})",
                remedy="Install from https://ollama.com, then run `ollama serve`",
            )
        )
        return checks

    installed = {model.get("name", "") for model in payload.get("models", [])}
    checks.append(
        Check("ollama", CheckStatus.OK, f"reachable; {len(installed)} model(s) installed")
    )

    for wanted in expected_models:
        # Ollama reports "name:tag"; a bare name means the :latest tag.
        candidates = {wanted, f"{wanted}:latest"}
        if candidates & installed:
            checks.append(Check(f"model:{wanted}", CheckStatus.OK, "pulled"))
        else:
            checks.append(
                Check(
                    f"model:{wanted}",
                    CheckStatus.FAIL,
                    "not pulled",
                    remedy=f"ollama pull {wanted}",
                )
            )
    return checks


def check_stt(engine: str) -> Check:
    """Report which STT backend will actually be used."""
    parakeet = _module_available("onnx_asr")
    whisper = _module_available("faster_whisper")

    if engine == "parakeet" and not parakeet:
        return Check(
            "stt",
            CheckStatus.FAIL,
            "engine=parakeet but onnx-asr is not installed",
            remedy="uv sync --extra parakeet",
        )
    if engine == "whisper" and not whisper:
        return Check(
            "stt",
            CheckStatus.FAIL,
            "engine=whisper but faster-whisper is not installed",
            remedy="uv sync --extra whisper",
        )
    if engine == "auto":
        if parakeet:
            return Check("stt", CheckStatus.OK, "auto -> parakeet (onnx-asr)")
        if whisper:
            return Check("stt", CheckStatus.OK, "auto -> whisper (faster-whisper)")
        return Check(
            "stt",
            CheckStatus.FAIL,
            "no local STT backend installed",
            remedy="uv sync --extra parakeet",
        )
    return Check("stt", CheckStatus.OK, f"engine={engine} available")


def check_memory(profile: HardwareProfile, *, needed_mb: int = 6000) -> Check:
    """Warn when RAM is tight for models plus a browser."""
    if profile.available_ram_mb >= needed_mb:
        return Check(
            "memory",
            CheckStatus.OK,
            f"{profile.available_ram_mb} MB available of {profile.total_ram_mb} MB",
        )
    return Check(
        "memory",
        CheckStatus.WARN,
        f"only {profile.available_ram_mb} MB available; models plus Chromium want ~{needed_mb} MB",
        remedy="Close other applications, or `ollama stop <model>` to unload unused models",
    )


def check_disk(profile: HardwareProfile, *, needed_gb: float = 4.0) -> Check:
    if profile.free_disk_gb >= needed_gb:
        return Check("disk", CheckStatus.OK, f"{profile.free_disk_gb:.1f} GB free")
    return Check(
        "disk",
        CheckStatus.WARN,
        f"{profile.free_disk_gb:.1f} GB free; model downloads need about {needed_gb} GB",
    )


def check_cuda(profile: HardwareProfile) -> Check:
    """Report CUDA honestly: present and probed, or absent."""
    if not profile.gpus:
        return Check(
            "cuda",
            CheckStatus.WARN,
            "No CUDA device detected; running the CPU profile (slower but fully functional)",
        )
    gpu = profile.gpus[0]
    return Check(
        "cuda",
        CheckStatus.OK,
        f"{gpu.name}, {gpu.total_memory_mb} MB VRAM, driver {gpu.driver_version or 'unknown'}",
    )


def run_all_checks(
    profile: HardwareProfile,
    *,
    ollama_url: str,
    models: list[str],
    stt_engine: str,
) -> list[Check]:
    """Run the full diagnostic suite."""
    checks = [
        check_display(profile),
        check_chromium(),
        check_espeak(),
        check_stt(stt_engine),
        check_cuda(profile),
        check_memory(profile),
        check_disk(profile),
    ]
    checks.extend(check_ollama(ollama_url, models))
    return checks
