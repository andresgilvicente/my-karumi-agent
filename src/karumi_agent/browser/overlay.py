"""Python side of the status overlay.

The overlay is deliberately presentation-only. Nothing the agent decides depends
on it, and the authoritative record of what happened is the JSONL run log. That
matters because the overlay lives in the page's main world: a determined hostile
page could find and call its API. Keeping it non-authoritative means the worst a
page can do is draw a misleading chip, not change what the agent believes.
"""

from __future__ import annotations

import secrets
from enum import StrEnum
from importlib import resources
from typing import TYPE_CHECKING

from karumi_agent.logging import get_logger

if TYPE_CHECKING:
    from playwright.async_api import Locator, Page

log = get_logger("overlay")


class OverlayState(StrEnum):
    """The states the operator and audience can see.

    Generic by design: no product knowledge, no per-site vocabulary.
    """

    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    LEARNING = "learning"
    EXECUTING = "executing"
    VERIFIED = "verified"
    WAITING_LOGIN = "waiting_login"
    ERROR = "error"


def _load_script(key: str) -> str:
    source = resources.files("karumi_agent.browser").joinpath("overlay.js").read_text("utf-8")
    return source.replace("__KARUMI_KEY__", key)


class Overlay:
    """Installs and drives the in-page glass pane."""

    def __init__(self) -> None:
        # A per-session name makes the API harder for a page to stumble upon.
        # This is friction, not a security boundary -- see the module docstring.
        self._key = f"__karumi_{secrets.token_hex(8)}"
        self._script = _load_script(self._key)
        self._enabled = True

    @property
    def init_script(self) -> str:
        """The script to register with ``context.add_init_script``."""
        return self._script

    async def _call(self, page: Page, expression: str, arg: object = None) -> object | None:
        """Run an overlay call, swallowing failures.

        The overlay must never be able to break a demo: if the page navigated
        mid-call or the context was torn down, we log and carry on.
        """
        if not self._enabled:
            return None
        try:
            result: object = await page.evaluate(expression, arg)
            return result
        except Exception as exc:
            log.debug(f"overlay call failed: {type(exc).__name__}: {exc}")
            return None

    async def set_state(self, page: Page, state: OverlayState) -> None:
        await self._call(
            page,
            f"(s) => window['{self._key}'] && window['{self._key}'].setState(s)",
            state.value,
        )

    async def set_caption(self, page: Page, text: str) -> None:
        await self._call(
            page,
            f"(t) => window['{self._key}'] && window['{self._key}'].setCaption(t)",
            text,
        )

    async def clear_highlights(self, page: Page) -> None:
        await self._call(
            page,
            f"() => window['{self._key}'] && window['{self._key}'].clearHighlights()",
        )

    async def highlight(
        self,
        page: Page,
        locator: Locator,
        *,
        label: str | None = None,
        verified: bool = False,
    ) -> bool:
        """Draw a box around an element.

        The element is passed as a live handle rather than a selector, so the
        overlay never re-queries the DOM and cannot be pointed at a different
        element than the one the agent acted on.
        """
        if not self._enabled:
            return False
        try:
            result = await locator.evaluate(
                f"(el, a) => window['{self._key}'] "
                f"&& window['{self._key}'].highlight(el, a.label, a.verified)",
                {"label": label, "verified": verified},
            )
            return bool(result)
        except Exception as exc:
            log.debug(f"overlay highlight failed: {type(exc).__name__}: {exc}")
            return False

    async def reinstall(self, page: Page) -> None:
        """Re-attach after a hard navigation replaced the document."""
        await self._call(
            page,
            f"() => window['{self._key}'] && window['{self._key}'].reinstall()",
        )

    def disable(self) -> None:
        """Turn the overlay off, e.g. for headless test runs."""
        self._enabled = False
