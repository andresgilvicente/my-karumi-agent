"""Configuration. Environment-driven, no secrets in the repo.

Every knob has a defensible default so `karumi spike --url ...` works with an
empty `.env`; only the voice providers genuinely require credentials, and those
are not read until Phase 3.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Chromium locations to try when Playwright's own managed download is absent.
#: Preprovisioned CI images (including this project's) ship a browser that does
#: not match the Playwright build number, which would otherwise be a hard fail.
_FALLBACK_CHROMIUM = (
    "/opt/pw-browsers/chromium",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
)


def _detect_chromium() -> str | None:
    for candidate in _FALLBACK_CHROMIUM:
        path = Path(candidate)
        if path.exists() and os.access(path, os.X_OK):
            return str(path)
    return None


class BrowserSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KARUMI_BROWSER_", extra="ignore")

    headless: bool = False
    """The demo surface is a real, visible browser. Headless is for CI."""

    executable: str | None = None
    """Explicit Chromium path. Auto-detected when unset."""

    viewport_width: int = 1440
    viewport_height: int = 900

    user_data_dir: str | None = None
    """Persistent profile, so a pre-authenticated session can be reused.
    Credentials are never handled by the agent itself."""

    nav_timeout_ms: int = 30_000
    action_timeout_ms: int = 8_000

    snapshot_debounce_ms: int = 250
    track_viewport: bool = True

    settle_ms: int = 350
    """How long to wait for the page to react before verifying an action.
    Long enough for an SPA re-render, short enough to stay off the speech path."""

    max_nodes: int = 1200
    max_depth: int = 22
    """Snapshot budget. Exceeding it prunes offscreen material, never visible."""

    allow_cross_origin_navigation: bool = False
    """When false, navigation is confined to the origin the demo started on."""

    def resolved_executable(self) -> str | None:
        return self.executable or _detect_chromium()


class LogSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KARUMI_LOG_", extra="ignore")

    level: str = "INFO"
    dir: str = "logs"
    console: bool = True
    """Human-readable console output alongside the JSONL event log."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    browser: BrowserSettings = Field(default_factory=BrowserSettings)
    log: LogSettings = Field(default_factory=LogSettings)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
