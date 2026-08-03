"""Shared fixtures for tests that need a real browser and a real page.

The fixture site is served over HTTP rather than `file://` so that iframes,
origins and navigation behave the way they do in production. It binds to an
ephemeral port so parallel runs do not collide.
"""

from __future__ import annotations

import functools
import http.server
import socketserver
import threading
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio

from karumi_agent.browser.playwright_adapter import BUNDLE_PATH, PlaywrightBrowserAdapter
from karumi_agent.settings import BrowserSettings

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "site"


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture(scope="session")
def fixture_site() -> Iterator[str]:
    handler = functools.partial(_QuietHandler, directory=str(FIXTURE_DIR))
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", 0), handler) as httpd:
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{port}"
        finally:
            httpd.shutdown()


@pytest.fixture(scope="session")
def browser_settings() -> BrowserSettings:
    if not BUNDLE_PATH.exists():
        pytest.skip(
            "injected page bundle not built; run "
            "`npm --prefix src/karumi_agent/browser/injected run build`"
        )
    # Headless here; the product default is a visible window, which is the
    # whole point of the demo but useless in CI.
    return BrowserSettings(headless=True)


@pytest_asyncio.fixture
async def adapter(
    browser_settings: BrowserSettings, fixture_site: str
) -> AsyncIterator[PlaywrightBrowserAdapter]:
    instance = PlaywrightBrowserAdapter(browser_settings)
    await instance.start(fixture_site)
    try:
        yield instance
    finally:
        await instance.close()
