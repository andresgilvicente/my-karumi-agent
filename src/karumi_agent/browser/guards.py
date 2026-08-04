"""Interception hooks that contain what the page and the agent can do.

Registered before the first navigation, because several of them (dialogs,
downloads, popups) only work if they are attached before the event can fire.

A note on scope. Blocking *navigation* outside the allowed origins is the control
that matters: it is what stops the agent wandering off the site being
demonstrated. Blocking every *subresource* is a much more aggressive posture that
breaks real applications -- the demo target alone needs fonts, a Convex
WebSocket, and Cloudflare assets. So subresource filtering is available via
``strict_subresources`` but is off by default, and the honest description of the
default is "the agent cannot navigate away", not "the page is sandboxed".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from karumi_agent.logging import get_logger

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Dialog, Download, Page, Request, Route

log = get_logger("guards")


@dataclass(slots=True)
class ModalState:
    """A dialog currently blocking the page.

    Mirrors Playwright MCP: while a modal is open every other action must be
    refused rather than left to time out, so a stray ``confirm()`` cannot
    deadlock the voice loop mid-sentence.
    """

    kind: str
    message: str
    default_value: str = ""


@dataclass(slots=True)
class GuardEvents:
    """Everything the guards observed, for evidence and post-mortems."""

    blocked_navigations: list[str] = field(default_factory=list)
    blocked_requests: list[str] = field(default_factory=list)
    dialogs: list[str] = field(default_factory=list)
    downloads: list[str] = field(default_factory=list)
    popups: list[str] = field(default_factory=list)
    console_errors: list[str] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)

    def drain(self) -> dict[str, list[str]]:
        """Return and clear. Called after each action to attach fresh evidence."""
        payload = {
            "blocked_navigations": list(self.blocked_navigations),
            "blocked_requests": list(self.blocked_requests),
            "dialogs": list(self.dialogs),
            "downloads": list(self.downloads),
            "popups": list(self.popups),
            "console_errors": list(self.console_errors),
            "page_errors": list(self.page_errors),
        }
        for value in (
            self.blocked_navigations,
            self.blocked_requests,
            self.dialogs,
            self.downloads,
            self.popups,
            self.console_errors,
            self.page_errors,
        ):
            value.clear()
        return payload


def origin_of(url: str) -> str:
    """Scheme + host + port, or "" when the URL has no origin."""
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def registrable_suffix(host: str) -> str:
    """A crude eTLD+1 so ``app.example.com`` and ``example.com`` match.

    Deliberately simple: this widens an allowlist the operator already chose, it
    does not decide whether an unknown site is trustworthy.
    """
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


class NavigationGuard:
    """Decides which URLs the agent and page may navigate to."""

    def __init__(
        self,
        *,
        allowed_origins: list[str],
        restrict: bool = True,
        allow_subdomains: bool = True,
    ) -> None:
        self._restrict = restrict
        self._allow_subdomains = allow_subdomains
        self._origins: set[str] = set()
        self._suffixes: set[str] = set()
        for origin in allowed_origins:
            self.allow(origin)

    def allow(self, url: str) -> None:
        origin = origin_of(url)
        if not origin:
            return
        self._origins.add(origin)
        host = urlparse(origin).hostname or ""
        if host:
            self._suffixes.add(registrable_suffix(host))

    @property
    def allowed_origins(self) -> list[str]:
        return sorted(self._origins)

    def is_allowed(self, url: str) -> bool:
        if not self._restrict:
            return True
        # about:blank and data: have no origin; they are inert and always fine.
        if url.startswith(("about:", "data:", "chrome-error:")):
            return True
        origin = origin_of(url)
        if not origin:
            return False
        if origin in self._origins:
            return True
        if self._allow_subdomains:
            host = urlparse(origin).hostname or ""
            return bool(host) and registrable_suffix(host) in self._suffixes
        return False


class BrowserGuards:
    """Attaches every interception hook to a context and its pages."""

    def __init__(
        self,
        *,
        navigation: NavigationGuard,
        download_dir: str,
        strict_subresources: bool = False,
    ) -> None:
        self.navigation = navigation
        self.events = GuardEvents()
        self.modal: ModalState | None = None
        self._download_dir = download_dir
        self._strict = strict_subresources

    # -------------------------------------------------------------- attaching
    async def attach_context(self, context: BrowserContext) -> None:
        """Register context-level hooks. Must run before the first navigation."""
        await context.route("**/*", self._handle_route)
        context.on("page", lambda page: self.attach_page(page))

    def attach_page(self, page: Page) -> None:
        """Register page-level hooks."""
        page.on("dialog", self._handle_dialog)
        page.on("download", self._handle_download)
        page.on("popup", self._handle_popup)
        page.on("framenavigated", self._handle_frame_navigated)
        page.on("console", self._handle_console)
        page.on("pageerror", self._handle_page_error)

    # ---------------------------------------------------------------- routing
    async def _handle_route(self, route: Route) -> None:
        request: Request = route.request
        url = request.url

        # Top-level document navigation is the control that matters.
        is_top_level_nav = request.is_navigation_request() and request.frame.parent_frame is None
        if is_top_level_nav and not self.navigation.is_allowed(url):
            self.events.blocked_navigations.append(url)
            log.warning(f"blocked navigation outside allowlist: {url}")
            await route.abort("blockedbyclient")
            return

        if self._strict and not self.navigation.is_allowed(url):
            self.events.blocked_requests.append(url)
            await route.abort("blockedbyclient")
            return

        await route.continue_()

    # ---------------------------------------------------------------- dialogs
    def _handle_dialog(self, dialog: Dialog) -> None:
        """Record the dialog and dismiss it.

        beforeunload is always dismissed so a page cannot trap the demo. Other
        dialogs are recorded as modal state and dismissed too: accepting one
        would be taking an action nobody authorised.
        """
        self.modal = ModalState(
            kind=dialog.type, message=dialog.message, default_value=dialog.default_value
        )
        self.events.dialogs.append(f"{dialog.type}: {dialog.message[:200]}")
        log.info(f"dialog intercepted ({dialog.type}): {dialog.message[:120]}")

        async def dismiss() -> None:
            try:
                await dialog.dismiss()
            except Exception as exc:
                log.debug(f"dialog dismiss failed: {exc}")
            finally:
                self.modal = None

        # Playwright allows the handler to be sync; schedule the async dismissal.
        import asyncio

        asyncio.create_task(dismiss())  # noqa: RUF006

    # -------------------------------------------------------------- downloads
    def _handle_download(self, download: Download) -> None:
        """Divert downloads into a sandbox directory with a sanitised name."""
        suggested = download.suggested_filename or "download"
        # Playwright MCP's comment applies verbatim: do not trust web names.
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", suggested)[:120] or "download"
        target = f"{self._download_dir}/{safe}"
        self.events.downloads.append(safe)
        log.warning(f"download intercepted: {suggested!r} -> {target}")

        async def save() -> None:
            try:
                await download.save_as(target)
            except Exception as exc:
                log.debug(f"download save failed: {exc}")

        import asyncio

        asyncio.create_task(save())  # noqa: RUF006

    # ----------------------------------------------------------------- popups
    def _handle_popup(self, popup: Page) -> None:
        """Close new tabs rather than following them.

        A demo has one window. A popup is either an ad or a navigation we did not
        sanction; either way the audience should not lose the thread.
        """
        url = popup.url
        self.events.popups.append(url)
        log.warning(f"popup closed: {url}")

        async def close() -> None:
            try:
                await popup.close()
            except Exception as exc:
                log.debug(f"popup close failed: {exc}")

        import asyncio

        asyncio.create_task(close())  # noqa: RUF006

    # ------------------------------------------------------------- navigation
    def _handle_frame_navigated(self, frame: object) -> None:
        """Second line of defence, because route allowlists miss redirects.

        Playwright MCP documents that its own origin filtering "does not affect
        redirects". Re-checking on framenavigated catches the case where an
        allowed URL 302s somewhere else.
        """
        url = getattr(frame, "url", "") or ""
        parent = getattr(frame, "parent_frame", None)
        if parent is not None or not url:
            return
        if not self.navigation.is_allowed(url):
            self.events.blocked_navigations.append(url)
            log.error(f"navigated outside allowlist (redirect?): {url}")

    # ------------------------------------------------------------------- logs
    def _handle_console(self, message: object) -> None:
        kind = getattr(message, "type", "")
        if kind == "error":
            text = str(getattr(message, "text", ""))[:300]
            self.events.console_errors.append(text)

    def _handle_page_error(self, error: object) -> None:
        self.events.page_errors.append(str(error)[:300])
