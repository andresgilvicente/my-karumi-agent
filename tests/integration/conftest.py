"""Fixtures for tests that drive a real browser against the hostile fixture site."""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from karumi_agent.browser.executor import ActionExecutor
from karumi_agent.browser.session import BrowserSession
from karumi_agent.browser.snapshotter import Snapshotter
from karumi_agent.config import BrowserSettings, SafetySettings, SnapshotSettings
from karumi_agent.server.fixture import FixtureServer

# Every test in this package needs Chromium.
pytestmark = pytest.mark.requires_browser


@pytest.fixture(scope="session")
def fixture_server() -> Iterator[str]:
    """Serve the hostile fixture site for the duration of the session."""
    server = FixtureServer()
    base_url = server.start()
    try:
        yield base_url
    finally:
        server.stop()


@pytest.fixture
async def session(fixture_server: str) -> AsyncIterator[BrowserSession]:
    """A browser session against a throwaway profile.

    Headless here: these tests assert on structure, not on what a human sees, and
    CI has no display. The demo itself runs headed.
    """
    profile = Path(tempfile.mkdtemp(prefix="karumi-test-profile-"))
    downloads = Path(tempfile.mkdtemp(prefix="karumi-test-downloads-"))
    settings = BrowserSettings(
        headless=True,
        profile_dir=profile,
        download_dir=downloads,
        settle_ms=150,  # the fixture is local; no need to wait 500ms every action
        action_timeout_s=10.0,
        navigation_timeout_s=15.0,
    )
    browser = BrowserSession(settings, allowed_origins=[fixture_server])
    await browser.start()
    try:
        yield browser
    finally:
        await browser.stop()
        shutil.rmtree(profile, ignore_errors=True)
        shutil.rmtree(downloads, ignore_errors=True)


@pytest.fixture
def snapshotter(session: BrowserSession) -> Snapshotter:
    return Snapshotter(session, SnapshotSettings())


@pytest.fixture
def executor(session: BrowserSession, snapshotter: Snapshotter) -> ActionExecutor:
    return ActionExecutor(session, snapshotter, SafetySettings())
