"""The browser worker on a real Pipecat bus, driving a real browser.

This is the Phase 2 equivalent of the Phase 1 spike: it proves the claim the
architecture rests on. Pipecat's `UIWorker` is documented to take snapshots from
its React client over RTVI; the whole design here depends on it accepting them
from a worker we wrote instead, because `on_bus_message` routes on the event
name and not on the sender.

If that were wrong, no amount of downstream code would help — so it is tested
directly, against the real bus and the real browser, with no LLM in the way.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from karumi_agent.browser.playwright_adapter import PlaywrightBrowserAdapter
from karumi_agent.domain import query
from karumi_agent.domain.models import Action, ActionKind, ErrorCode
from karumi_agent.session import ClientWorker
from karumi_agent.workers._pipecat import UI_SNAPSHOT_EVENT, BusUIEventMessage, WorkerRunner
from karumi_agent.workers.browser_worker import BrowserWorker

pytestmark = pytest.mark.browser


class SnapshotSpy(ClientWorker):
    """A bus peer that records every `__ui_snapshot` it sees.

    Stands in for `UIWorker` so this test covers the bus contract without
    needing an LLM: it receives exactly what the real UI worker would.
    """

    def __init__(self, name: str = "spy") -> None:
        super().__init__(name=name)
        self.snapshots: list[dict] = []

    async def on_bus_message(self, message) -> None:  # type: ignore[no-untyped-def]
        if (
            isinstance(message, BusUIEventMessage)
            and message.event_name == UI_SNAPSHOT_EVENT
            and isinstance(message.payload, dict)
        ):
            self.snapshots.append(message.payload)
        await super().on_bus_message(message)


@pytest_asyncio.fixture
async def bus_session(browser_settings, fixture_site: str) -> AsyncIterator[tuple]:
    """A runner with a browser worker, a client and a snapshot spy on the bus."""
    adapter = PlaywrightBrowserAdapter(browser_settings)
    browser = BrowserWorker("browser", adapter, url=fixture_site)
    client = ClientWorker(name="client")
    spy = SnapshotSpy()
    runner = WorkerRunner(name="test")
    running = asyncio.create_task(runner.run(auto_end=False))
    await asyncio.sleep(0)
    await runner.add_workers(browser, client, spy)
    await browser.open()

    try:
        yield browser, client, spy
    finally:
        await browser.close()
        await runner.end()
        running.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await running


class TestSnapshotDelivery:
    async def test_snapshots_reach_the_bus_in_the_shape_uiworker_expects(
        self, bus_session
    ) -> None:
        """`UIWorker.render_ui_state` reads `root` and `selection`; both must be there."""
        _, _, spy = bus_session
        await asyncio.sleep(0.3)

        assert spy.snapshots, "no snapshot reached the bus"
        payload = spy.snapshots[-1]

        assert isinstance(payload.get("root"), dict)
        assert isinstance(payload["root"].get("role"), str)
        assert isinstance(payload.get("captured_at"), int)
        # Our own addition, ignored by Pipecat's renderer, used to stamp actions.
        assert isinstance(payload.get("snapshot_id"), str)

    async def test_page_changes_produce_new_snapshots(self, bus_session) -> None:
        """The fixture mutates on a timer; the bus should keep hearing about it."""
        _, _, spy = bus_session
        await asyncio.sleep(0.3)
        before = len(spy.snapshots)

        await asyncio.sleep(1.2)

        assert len(spy.snapshots) > before


class TestObserveJob:
    async def test_returns_a_description_of_the_current_page(self, bus_session) -> None:
        _, client, _ = bus_session

        async with client.job("browser", name="observe", timeout=30) as pending:
            pass

        response = pending.response
        assert isinstance(response, dict)
        assert response["nodes"] > 20
        assert response["snapshot_id"].startswith("s")
        assert "127.0.0.1" in response["url"]


class TestActJob:
    async def _current_snapshot_id(self, browser: BrowserWorker) -> str:
        snapshot = await browser.adapter.observe(force=True)
        await browser.publish_snapshot(snapshot)
        return snapshot.snapshot_id

    async def test_verified_action_round_trips(self, bus_session) -> None:
        browser, client, _ = bus_session
        snapshot = await browser.adapter.observe(force=True)
        await browser.publish_snapshot(snapshot)
        target = query.offscreen(snapshot, "heading")[0]

        action = Action(
            action_id="j1",
            kind=ActionKind.SCROLL_TO,
            snapshot_id=snapshot.snapshot_id,
            ref=target.ref,
        )
        async with client.job(
            "browser", name="act", payload=action.model_dump(mode="json"), timeout=30
        ) as pending:
            pass

        result = pending.response
        assert isinstance(result, dict)
        assert result["ok"] and result["verified"], result
        assert result["changes"]
        assert result["timings_ms"]["verify"] > 0

    async def test_policy_refusal_comes_back_as_a_result_not_an_exception(
        self, bus_session
    ) -> None:
        """A refusal the agent can explain is worth more than one that crashes it."""
        browser, client, _ = bus_session
        snapshot = await browser.adapter.observe(force=True)
        await browser.publish_snapshot(snapshot)
        destructive = query.by_name(snapshot, r"\bdelete\b", roles={"button"})[0]

        action = Action(
            action_id="j2",
            kind=ActionKind.CLICK,
            snapshot_id=snapshot.snapshot_id,
            ref=destructive.ref,
        )
        async with client.job(
            "browser", name="act", payload=action.model_dump(mode="json"), timeout=30
        ) as pending:
            pass

        result = pending.response
        assert isinstance(result, dict)
        assert result["ok"] is False
        assert result["error_code"] == ErrorCode.NEEDS_CONFIRMATION
        assert "commits or changes data" in result["summary"]

    async def test_stale_snapshot_is_refused_over_the_bus(self, bus_session) -> None:
        _, client, _ = bus_session

        action = Action(
            action_id="j3",
            kind=ActionKind.HIGHLIGHT,
            snapshot_id="s-never-issued",
            ref="f0.e1",
        )
        async with client.job(
            "browser", name="act", payload=action.model_dump(mode="json"), timeout=30
        ) as pending:
            pass

        result = pending.response
        assert isinstance(result, dict)
        assert result["error_code"] == ErrorCode.STALE_SNAPSHOT

    async def test_malformed_action_is_reported_not_raised(self, bus_session) -> None:
        """A bad tool call must not take the worker down mid-demo."""
        _, client, _ = bus_session

        async with client.job(
            "browser", name="act", payload={"kind": "not_a_real_action"}, timeout=30
        ) as pending:
            pass

        result = pending.response
        assert isinstance(result, dict)
        assert result["ok"] is False
        assert result["error_code"] == ErrorCode.INVALID_ACTION

    async def test_acting_republishes_so_the_model_sees_its_own_effect(
        self, bus_session
    ) -> None:
        """Without this the next turn reasons about the page before the action."""
        browser, client, spy = bus_session
        snapshot = await browser.adapter.observe(force=True)
        await browser.publish_snapshot(snapshot)

        target = next(
            node
            for node in query.navigation_controls(snapshot)
            if node.name and node.name.lower() not in snapshot.page.title.lower()
        )
        spy.snapshots.clear()

        action = Action(
            action_id="j4",
            kind=ActionKind.CLICK,
            snapshot_id=snapshot.snapshot_id,
            ref=target.ref,
        )
        async with client.job(
            "browser", name="act", payload=action.model_dump(mode="json"), timeout=30
        ) as pending:
            pass

        assert pending.response["verified"]
        assert spy.snapshots, "no snapshot published after the action"
        assert spy.snapshots[-1]["snapshot_id"] != snapshot.snapshot_id
