"""Capturing page observations and enforcing their freshness.

``SnapshotStore`` is the seam between the async browser and the synchronous
``UIWorker.render_ui_state()`` hook: a background capture keeps the store fresh,
and the render call reads whatever is current without needing to await.

Freshness is enforced in one place, deliberately. A ref like ``e12`` means
"the twelfth interactable element *in that observation*"; resolving it against a
later page state is how an agent ends up clicking whatever slid into that
position. So an action must name its snapshot, and the store refuses if that
snapshot is no longer current.
"""

from __future__ import annotations

import contextlib
import time
from typing import TYPE_CHECKING

from karumi_agent.browser.session import BrowserSession
from karumi_agent.config import SnapshotSettings
from karumi_agent.domain.snapshot import PageSnapshot, Viewport, parse_aria_snapshot
from karumi_agent.errors import SnapshotError, StaleSnapshotError
from karumi_agent.logging import get_logger

if TYPE_CHECKING:
    pass

log = get_logger("snapshot")


class SnapshotStore:
    """Holds the most recent observation."""

    def __init__(self, *, max_age_s: float = 30.0) -> None:
        self._current: PageSnapshot | None = None
        self._previous: PageSnapshot | None = None
        self._max_age_s = max_age_s

    @property
    def current(self) -> PageSnapshot | None:
        return self._current

    @property
    def previous(self) -> PageSnapshot | None:
        return self._previous

    @property
    def current_id(self) -> str | None:
        return self._current.snapshot_id if self._current else None

    def set(self, snapshot: PageSnapshot) -> None:
        self._previous = self._current
        self._current = snapshot

    def require(self, snapshot_id: str, *, now: float | None = None) -> PageSnapshot:
        """Return the named snapshot, or refuse.

        Two ways to be stale: naming an observation that is no longer current, or
        naming one that is current but too old to trust. Both raise, because the
        correct response is identical -- re-observe, then decide again.
        """
        current = self._current
        if current is None:
            raise StaleSnapshotError(requested=snapshot_id, current=None)
        if snapshot_id != current.snapshot_id:
            raise StaleSnapshotError(requested=snapshot_id, current=current.snapshot_id)
        if current.is_expired(self._max_age_s, now=now):
            raise StaleSnapshotError(requested=snapshot_id, current=current.snapshot_id)
        return current

    def clear(self) -> None:
        self._current = self._previous = None


class Snapshotter:
    """Turns the live page into a typed, budgeted observation."""

    def __init__(
        self,
        session: BrowserSession,
        settings: SnapshotSettings,
        *,
        store: SnapshotStore | None = None,
    ) -> None:
        self._session = session
        self._settings = settings
        self.store = store or SnapshotStore(max_age_s=settings.max_age_s)
        self._counter = 0

    def _next_id(self) -> str:
        self._counter += 1
        # Readable in logs and unique within a session, which is all a snapshot id
        # needs to be.
        return f"s{self._counter:04d}"

    async def capture(self) -> PageSnapshot:
        """Observe the page now and make it the current snapshot."""
        page = self._session.page
        started = time.perf_counter()

        try:
            raw = await page.aria_snapshot(
                mode="ai",
                boxes=self._settings.include_boxes,
            )
        except Exception as exc:
            raise SnapshotError(
                "Could not capture an accessibility snapshot", cause=str(exc)
            ) from exc

        url = page.url
        title = ""
        with contextlib.suppress(Exception):
            title = await page.title()

        viewport_size = page.viewport_size or {
            "width": self._session.settings.viewport_width,
            "height": self._session.settings.viewport_height,
        }

        snapshot = parse_aria_snapshot(
            raw,
            url=url,
            title=title,
            viewport=Viewport(int(viewport_size["width"]), int(viewport_size["height"])),
            snapshot_id=self._next_id(),
        )

        # The accessibility tree is a heuristic for password fields; the DOM is
        # authoritative, and this flag gates the login-wall behaviour.
        password_count = await self._session.count_password_fields()
        snapshot.has_password_field = snapshot.has_password_field or password_count > 0

        self.store.set(snapshot)
        log.debug(
            "snapshot captured",
            snapshot_id=snapshot.snapshot_id,
            nodes=snapshot.node_count,
            refs=len(snapshot.refs()),
            url=url,
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
        )
        return snapshot

    async def capture_after_action(self) -> PageSnapshot:
        """Re-observe once the page has settled following an action."""
        return await self._session.run_and_settle(self.capture)

    def render_current(self) -> str:
        """Render the current observation for the model. Synchronous by design.

        ``UIWorker.render_ui_state()`` is a sync hook, so this must not await.
        """
        snapshot = self.store.current
        if snapshot is None:
            return ""
        return snapshot.render_ui_state(
            max_nodes=self._settings.max_nodes,
            max_tokens=self._settings.max_tokens,
            include_boxes=False,  # geometry is for the overlay, not the model
            prioritise_viewport=self._settings.prioritise_viewport,
        )
