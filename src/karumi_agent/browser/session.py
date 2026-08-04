"""Browser lifecycle and page settling.

A single headed Chromium with a persistent profile. Headed because the audience
is meant to watch it; persistent because the operator signs in once and the
session survives restarts, which is how the login wall on the demo target is
handled without ever putting credentials in the repo.

The settling algorithm deserves a note. ``wait_for_load_state("networkidle")`` is
the obvious choice and it is wrong here: the demo target holds an open Convex
WebSocket, so the network is never idle and the wait would never resolve.
Playwright's own docs discourage it generally. Instead we track requests issued
during an action and wait for those to finish, with hard caps -- the approach
Playwright MCP settled on for the same reason.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Self

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    Request,
    async_playwright,
)

from karumi_agent.browser.guards import BrowserGuards, NavigationGuard
from karumi_agent.browser.overlay import Overlay, OverlayState
from karumi_agent.config import BrowserSettings
from karumi_agent.errors import BrowserError, NavigationBlockedError
from karumi_agent.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

log = get_logger("browser")

# Resource types worth waiting for when deciding a page has settled. Media and
# fonts can stream for a long time without affecting what the agent can read.
_SETTLE_RESOURCE_TYPES = frozenset({"document", "stylesheet", "script", "xhr", "fetch"})

_LOAD_CAP_S = 5.0
_REQUEST_CAP_S = 5.0


class BrowserSession:
    """Owns the Chromium instance the demo runs in."""

    def __init__(
        self,
        settings: BrowserSettings,
        *,
        allowed_origins: list[str] | None = None,
        restrict_to_origin: bool = True,
        strict_subresources: bool = False,
        enable_overlay: bool = True,
    ) -> None:
        self._settings = settings
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

        self.overlay = Overlay()
        if not enable_overlay:
            self.overlay.disable()

        self.navigation = NavigationGuard(
            allowed_origins=allowed_origins or [],
            restrict=restrict_to_origin,
        )
        self.guards = BrowserGuards(
            navigation=self.navigation,
            download_dir=str(settings.download_dir),
            strict_subresources=strict_subresources,
        )

    # ------------------------------------------------------------- lifecycle
    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.stop()

    async def start(self) -> None:
        """Launch Chromium and install every hook before the first navigation."""
        settings = self._settings
        settings.profile_dir.mkdir(parents=True, exist_ok=True)
        settings.download_dir.mkdir(parents=True, exist_ok=True)

        self._playwright = await async_playwright().start()

        args = [
            f"--window-size={settings.viewport_width},{settings.viewport_height}",
            "--no-default-browser-check",
            "--no-first-run",
            "--disable-features=Translate,AcceptCHFrame",
        ]
        if settings.disable_gpu:
            # Some hybrid-GPU laptops cannot open a window under WSLg without this.
            args += ["--disable-gpu", "--use-gl=swiftshader"]

        try:
            # A persistent context keeps cookies and localStorage between runs, so
            # a manual sign-in only has to happen once.
            self._context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(settings.profile_dir),
                headless=settings.headless,
                args=args,
                viewport={"width": settings.viewport_width, "height": settings.viewport_height},
                accept_downloads=True,
                # Never bypass the site's CSP: it would change how the page behaves
                # during a demo, and the overlay does not need it.
                bypass_csp=False,
            )
        except Exception as exc:
            raise BrowserError(
                "Could not launch Chromium",
                hint=(
                    "Run `uv run playwright install chromium`. On Linux you may also "
                    "need system libraries: `uv run playwright install-deps chromium` "
                    "(requires sudo) or `sudo apt-get install -y libnss3 libnspr4`."
                ),
                cause=str(exc),
            ) from exc

        await self._context.add_init_script(self.overlay.init_script)
        await self.guards.attach_context(self._context)

        pages = self._context.pages
        self._page = pages[0] if pages else await self._context.new_page()
        self.guards.attach_page(self._page)

        self._context.set_default_timeout(settings.action_timeout_s * 1000)
        self._context.set_default_navigation_timeout(settings.navigation_timeout_s * 1000)

        log.info(
            "browser started",
            headless=settings.headless,
            profile=str(settings.profile_dir),
        )

    async def stop(self) -> None:
        """Tear down, tolerating a browser the operator already closed.

        Each step is suppressed independently: a demo often ends with the operator
        closing the window, and that must not turn into a traceback on shutdown.
        """
        if self._context is not None:
            with contextlib.suppress(Exception):
                await self._context.close()
        if self._browser is not None:
            with contextlib.suppress(Exception):
                await self._browser.close()
        if self._playwright is not None:
            with contextlib.suppress(Exception):
                await self._playwright.stop()
        self._context = self._browser = self._playwright = self._page = None
        log.info("browser stopped")

    # ---------------------------------------------------------------- access
    @property
    def page(self) -> Page:
        if self._page is None:
            raise BrowserError("Browser session is not started")
        return self._page

    @property
    def context(self) -> BrowserContext:
        if self._context is None:
            raise BrowserError("Browser session is not started")
        return self._context

    @property
    def settings(self) -> BrowserSettings:
        return self._settings

    @property
    def is_running(self) -> bool:
        return self._page is not None and not self._page.is_closed()

    # ------------------------------------------------------------ navigation
    async def goto(self, url: str, *, allow_new_origin: bool = False) -> None:
        """Navigate, enforcing the origin allowlist.

        The first navigation of a session implicitly defines the demo's origin, so
        an empty allowlist adopts the target rather than refusing it.
        """
        if not self.navigation.allowed_origins or allow_new_origin:
            self.navigation.allow(url)
        elif not self.navigation.is_allowed(url):
            raise NavigationBlockedError(url, allowed=self.navigation.allowed_origins)

        page = self.page
        try:
            await page.goto(url, wait_until="domcontentloaded")
        except Exception as exc:
            raise BrowserError("Navigation failed", url=url, cause=str(exc)) from exc

        # The page is usable at domcontentloaded; the load event is a nice-to-have
        # and on a heavy SPA it can be a long time coming.
        with contextlib.suppress(Exception):
            await page.wait_for_load_state("load", timeout=_LOAD_CAP_S * 1000)
        await self.overlay.reinstall(page)
        log.info("navigated", url=page.url)

    async def go_back(self) -> bool:
        with contextlib.suppress(Exception):
            response = await self.page.go_back(wait_until="domcontentloaded")
            return response is not None
        return False

    async def go_forward(self) -> bool:
        with contextlib.suppress(Exception):
            response = await self.page.go_forward(wait_until="domcontentloaded")
            return response is not None
        return False

    # -------------------------------------------------------------- settling
    async def run_and_settle[T](self, action: Callable[[], Awaitable[T]]) -> T:
        """Run an action, then wait for the page to stop changing because of it.

        Deliberately request-driven rather than ``networkidle``: a page holding an
        open WebSocket (as the demo target does) is never idle, so an idle-based
        wait would hang until it timed out on every single action.
        """
        page = self.page
        requests: list[Request] = []

        def collect(request: Request) -> None:
            requests.append(request)

        page.on("request", collect)
        try:
            result = await action()
            await page.wait_for_timeout(self._settings.settle_ms)

            if any(r.is_navigation_request() for r in requests):
                with contextlib.suppress(Exception):
                    await page.wait_for_load_state("load", timeout=_LOAD_CAP_S * 1000)
                return result

            interesting = [r for r in requests if r.resource_type in _SETTLE_RESOURCE_TYPES]
            if interesting:
                await self._await_responses(interesting)
                await page.wait_for_timeout(self._settings.settle_ms)
            return result
        finally:
            with contextlib.suppress(Exception):
                page.remove_listener("request", collect)

    async def _await_responses(self, requests: list[Request]) -> None:
        """Wait for in-flight requests, capped so one slow call cannot stall us."""

        async def finished(request: Request) -> None:
            with contextlib.suppress(Exception):
                response = await request.response()
                if response is not None:
                    await response.finished()

        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(*(finished(r) for r in requests), return_exceptions=True),
                timeout=_REQUEST_CAP_S,
            )

    # ------------------------------------------------------------ login wall
    async def count_password_fields(self) -> int:
        """Ground truth for login-wall detection, straight from the DOM.

        Deliberately not a heuristic over the accessibility tree: this is the real
        ``input[type=password]`` count, and it is what the waiting state keys on.
        """
        with contextlib.suppress(Exception):
            return await self.page.locator("input[type=password]:visible").count()
        return 0

    async def wait_for_login(
        self,
        *,
        timeout_s: float = 300.0,
        poll_s: float = 1.0,
        on_wait: Callable[[], Awaitable[None]] | None = None,
    ) -> bool:
        """Block until a visible password field disappears.

        Returns True when the wall cleared, False on timeout. The agent says out
        loud that it is waiting; it never types a credential itself.
        """
        if await self.count_password_fields() == 0:
            return True

        await self.overlay.set_state(self.page, OverlayState.WAITING_LOGIN)
        if on_wait is not None:
            await on_wait()

        deadline = asyncio.get_running_loop().time() + timeout_s
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(poll_s)
            if not self.is_running:
                return False
            if await self.count_password_fields() == 0:
                log.info("login wall cleared")
                await self.overlay.set_state(self.page, OverlayState.IDLE)
                return True
        log.warning("timed out waiting for sign-in")
        return False

    # ------------------------------------------------------------------ misc
    async def screenshot(self, path: Path) -> bool:
        """Capture a screenshot for a diagnostic bundle."""
        with contextlib.suppress(Exception):
            path.parent.mkdir(parents=True, exist_ok=True)
            await self.page.screenshot(path=str(path))
            return True
        return False
