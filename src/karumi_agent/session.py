"""Wiring: the two workers, one bus, one page.

Kept in its own module so the composition is readable in one screen and the CLI
stays a thin shell. Phase 3 adds a voice worker as a third peer on the same bus;
nothing here should need to change to accommodate it, which is the point of
putting the browser behind a worker in the first place.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

from karumi_agent.browser.playwright_adapter import PlaywrightBrowserAdapter
from karumi_agent.knowledge.editorial import UNBRIEFED, ProductKnowledge, load_product
from karumi_agent.observability.logging import get_logger
from karumi_agent.settings import BrowserSettings
from karumi_agent.tools.policy import ActionPolicy
from karumi_agent.workers._pipecat import BaseWorker, WorkerRunner
from karumi_agent.workers.browser_worker import BrowserWorker
from karumi_agent.workers.ui_worker import KarumiUIWorker

log = get_logger(__name__)

BROWSER_WORKER = "browser"
UI_WORKER = "ui"
CLIENT_WORKER = "client"


class ClientWorker(BaseWorker):
    """The caller's seat on the bus.

    `WorkerRunner` owns the bus but is not a peer on it, so it cannot request
    jobs. Anything outside the worker graph that wants to ask a worker for
    something needs its own presence there — for now the CLI, in Phase 3 the
    voice pipeline. It holds no state and does no work of its own; it exists to
    have a return address.
    """


class DemoSession:
    """A running demo: a browser, an interface reasoner, and the bus between them."""

    def __init__(
        self,
        *,
        url: str,
        llm: Any,
        browser_settings: BrowserSettings,
        product: ProductKnowledge | None = None,
        policy: ActionPolicy | None = None,
    ) -> None:
        self.url = url
        self.product = product or UNBRIEFED

        self.adapter = PlaywrightBrowserAdapter(browser_settings, policy=policy)
        self.browser = BrowserWorker(BROWSER_WORKER, self.adapter, url=url)
        self.ui = KarumiUIWorker(
            UI_WORKER,
            llm=llm,
            browser_worker=BROWSER_WORKER,
            product=self.product,
        )
        self.client = ClientWorker(name=CLIENT_WORKER)
        self.runner = WorkerRunner(name="karumi")
        self._running: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Bring the workers onto the bus, start the runner, and open the page."""
        # The runner drives the bus for as long as the session lives, so it runs
        # alongside us rather than owning the foreground. `auto_end=False` keeps
        # it up after any single job finishes.
        self._running = asyncio.create_task(self.runner.run(auto_end=False))
        await asyncio.sleep(0)  # let the runner reach its first await

        await self.runner.add_workers(self.browser, self.ui, self.client)

        await self.browser.open()
        log.info("session.start", url=self.url, product=self.product.name)

    async def ask(self, query: str, *, timeout: float = 120.0) -> str:
        """Put one question to the agent and return what it says.

        Delegates through Pipecat's built-in `respond` job — the same path a
        voice worker will use in Phase 3, so text mode exercises the real
        delegation route rather than a shortcut that only exists for testing.
        """
        async with self.client.job(
            UI_WORKER, name="respond", payload={"query": query}, timeout=timeout
        ) as pending:
            pass

        response = pending.response or {}
        if isinstance(response, dict):
            answer = response.get("answer") or response.get("text") or ""
            return str(answer)
        return str(response)

    async def close(self) -> None:
        await self.browser.close()
        await self.runner.end()
        if self._running is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._running


def resolve_product(product: str | None) -> ProductKnowledge:
    """Load an editorial briefing by path or by short name, or return none.

    A missing briefing is a legitimate configuration, not an error: Stage 2 runs
    against an arbitrary URL with nothing written about it in advance.
    """
    if not product:
        return UNBRIEFED

    candidate = Path(product)
    if not candidate.exists():
        candidate = Path("config/products") / f"{product}.yaml"
    if not candidate.exists():
        raise FileNotFoundError(
            f"No product briefing at '{product}'. Expected a YAML file, or a name "
            f"matching one in config/products/."
        )
    return load_product(candidate)
