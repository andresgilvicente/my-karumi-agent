"""The browser port.

Everything above this line talks to `BrowserAdapter`, never to Playwright. That
keeps the workers, tools and policy testable against a fake, and leaves room to
swap the driver (CDP directly, an extension, a remote browser) without touching
the agent. The Playwright implementation lives in `playwright_adapter.py`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from karumi_agent.domain.models import Action, ActionResult, ElementState, Snapshot

SnapshotListener = Callable[[Snapshot], None]


class BrowserAdapter(Protocol):
    """A live browser showing a page the agent can observe and act on."""

    @property
    def current(self) -> Snapshot | None:
        """The most recent snapshot, or None before the first observation."""
        ...

    async def start(self, url: str) -> Snapshot:
        """Launch the browser, open `url`, and take the first snapshot."""
        ...

    async def close(self) -> None: ...

    async def observe(self, *, force: bool = False) -> Snapshot:
        """Return the current page state.

        With `force`, re-walks the page instead of returning the last streamed
        snapshot — used after an action, where staleness would defeat the point.
        """
        ...

    async def describe(self, ref: str) -> ElementState | None:
        """Inspect a single element. Returns None if the ref no longer resolves."""
        ...

    async def perform(self, action: Action) -> ActionResult:
        """Execute an action and verify it against the page.

        Never raises for expected failures — a rejected or unverifiable action
        comes back as an `ActionResult` carrying an `ErrorCode`, because the
        agent has to be able to tell the user what did not happen.
        """
        ...

    def subscribe(self, listener: SnapshotListener) -> None:
        """Register a callback invoked on every new snapshot."""
        ...
