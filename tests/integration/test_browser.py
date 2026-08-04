"""End-to-end browser behaviour against the hostile fixture.

These are the tests that make the safety and verification claims real. Each one
corresponds to a property the agent is supposed to have, exercised against a page
built specifically to break it.
"""

from __future__ import annotations

import pytest

from karumi_agent.browser.executor import ActionExecutor
from karumi_agent.browser.session import BrowserSession
from karumi_agent.browser.snapshotter import Snapshotter
from karumi_agent.domain.actions import ActionKind, ActionRequest, ActionStatus
from karumi_agent.errors import NavigationBlockedError, StaleSnapshotError

pytestmark = pytest.mark.requires_browser


async def _goto_and_snapshot(session: BrowserSession, snapshotter: Snapshotter, url: str) -> object:
    await session.goto(url)
    return await snapshotter.capture()


async def _visible_text(session: BrowserSession) -> str:
    """Rendered text only, so inline script sources do not count as page content."""
    return str(await session.page.evaluate("() => document.body.innerText"))


def _ref_for(snapshot: object, *, name: str) -> str:
    """Find a ref by accessible name, the way the model would."""
    for node in snapshot.root.walk():  # type: ignore[attr-defined]
        if node.name == name and node.ref:
            return str(node.ref)
    raise AssertionError(f"no ref found for {name!r}")


class TestObservation:
    async def test_captures_a_usable_snapshot(
        self, session: BrowserSession, snapshotter: Snapshotter, fixture_server: str
    ) -> None:
        snap = await _goto_and_snapshot(session, snapshotter, fixture_server)
        assert snap.node_count > 10  # type: ignore[attr-defined]
        assert snap.refs()  # type: ignore[attr-defined]
        rendered = snapshotter.render_current()
        assert "<ui_state" in rendered
        assert "Overview" in rendered

    async def test_labelled_canvas_chart_is_described(
        self, session: BrowserSession, snapshotter: Snapshotter, fixture_server: str
    ) -> None:
        """A canvas is opaque unless labelled. This one is, so we can be honest.

        The real demo target has no chart library and unlabelled visuals, which is
        exactly why this distinction is worth pinning down.
        """
        await _goto_and_snapshot(session, snapshotter, fixture_server)
        found = snapshotter.store.current.find("Revenue trend")  # type: ignore[union-attr]
        assert "128,430" in found

    async def test_same_origin_iframe_is_stitched_in(
        self, session: BrowserSession, snapshotter: Snapshotter, fixture_server: str
    ) -> None:
        await session.goto(fixture_server)
        await session.page.get_by_role("link", name="Analytics").click()
        snap = await snapshotter.capture_after_action()
        # Playwright prefixes refs from child frames, e.g. f1e2.
        assert any(ref.startswith("f") for ref in snap.refs())
        assert "Storage used" in snap.render()

    async def test_offscreen_content_is_deprioritised_not_lost(
        self, session: BrowserSession, snapshotter: Snapshotter, fixture_server: str
    ) -> None:
        snap = await _goto_and_snapshot(session, snapshotter, fixture_server)
        # Present in the tree...
        assert snap.find("Footer note").count("no matches") == 0  # type: ignore[attr-defined]
        # ...but not competing with on-screen content under a tight budget.
        tight = snap.render(max_nodes=12, max_tokens=10_000)  # type: ignore[attr-defined]
        assert "Overview" in tight


class TestRefStability:
    async def test_refs_survive_client_side_navigation(
        self, session: BrowserSession, snapshotter: Snapshotter, fixture_server: str
    ) -> None:
        """Same-document routing must not invalidate refs.

        This is the property that makes a snapshot-per-route affordable instead of
        a snapshot-per-interaction.
        """
        await _goto_and_snapshot(session, snapshotter, fixture_server)
        nav_ref = _ref_for(snapshotter.store.current, name="Analytics")

        await session.page.get_by_role("link", name="Analytics").click()
        after = await snapshotter.capture_after_action()

        assert after.resolve(nav_ref) is not None
        assert after.resolve(nav_ref).name == "Analytics"  # type: ignore[union-attr]

    async def test_refs_survive_constant_dom_mutation(
        self, session: BrowserSession, snapshotter: Snapshotter, fixture_server: str
    ) -> None:
        """The fixture mutates a counter every 700ms; stable elements keep refs."""
        await _goto_and_snapshot(session, snapshotter, fixture_server)
        ref = _ref_for(snapshotter.store.current, name="Show details")
        await session.page.wait_for_timeout(1600)  # several mutation ticks
        after = await snapshotter.capture()
        assert after.resolve(ref) is not None


class TestStaleness:
    async def test_acting_on_a_superseded_snapshot_is_refused(
        self,
        session: BrowserSession,
        snapshotter: Snapshotter,
        executor: ActionExecutor,
        fixture_server: str,
    ) -> None:
        first = await _goto_and_snapshot(session, snapshotter, fixture_server)
        ref = _ref_for(first, name="Show details")

        await snapshotter.capture()  # supersedes `first`

        result = await executor.execute(
            ActionRequest(
                kind=ActionKind.HIGHLIGHT,
                ref=ref,
                source_snapshot_id=first.snapshot_id,  # type: ignore[attr-defined]
            )
        )
        assert result.status is ActionStatus.STALE
        assert result.verified is False
        assert result.error is not None
        assert result.error["code"] == "stale_snapshot"

    async def test_expired_snapshot_is_refused(
        self,
        session: BrowserSession,
        snapshotter: Snapshotter,
        fixture_server: str,
    ) -> None:
        snap = await _goto_and_snapshot(session, snapshotter, fixture_server)
        snapshotter.store._max_age_s = 0.0
        with pytest.raises(StaleSnapshotError):
            snapshotter.store.require(snap.snapshot_id)  # type: ignore[attr-defined]


class TestSafety:
    async def test_destructive_click_requires_confirmation(
        self,
        session: BrowserSession,
        snapshotter: Snapshotter,
        executor: ActionExecutor,
        fixture_server: str,
    ) -> None:
        snap = await _goto_and_snapshot(session, snapshotter, fixture_server)
        ref = _ref_for(snap, name="Delete workspace")

        result = await executor.execute(
            ActionRequest(kind=ActionKind.CLICK, ref=ref, source_snapshot_id=snap.snapshot_id)  # type: ignore[attr-defined]
        )
        assert result.status is ActionStatus.NEEDS_CONFIRMATION
        assert result.confirmation_prompt is not None
        assert "Delete workspace" in result.confirmation_prompt

        # And the page really was left alone.
        # Assert on rendered text, not page.content(): the fixture's own <script>
        # source contains the string, so a raw HTML check would always fail.
        assert "Workspace deleted" not in await _visible_text(session)

    async def test_highlighting_a_destructive_control_is_allowed(
        self,
        session: BrowserSession,
        snapshotter: Snapshotter,
        executor: ActionExecutor,
        fixture_server: str,
    ) -> None:
        """Talking about a dangerous button is the whole point of a demo."""
        snap = await _goto_and_snapshot(session, snapshotter, fixture_server)
        ref = _ref_for(snap, name="Delete workspace")
        result = await executor.execute(
            ActionRequest(kind=ActionKind.HIGHLIGHT, ref=ref, source_snapshot_id=snap.snapshot_id)  # type: ignore[attr-defined]
        )
        assert result.status is ActionStatus.SUCCEEDED

    async def test_filling_a_password_field_is_blocked_outright(
        self,
        session: BrowserSession,
        snapshotter: Snapshotter,
        executor: ActionExecutor,
        fixture_server: str,
    ) -> None:
        await session.goto(fixture_server)
        await session.page.get_by_role("link", name="Settings").click()
        snap = await snapshotter.capture_after_action()

        pw_ref = next(
            (n.ref for n in snap.root.walk() if n.ref and "password" in (n.name or "").lower()),
            None,
        )
        assert pw_ref is not None, "fixture should expose a password field"

        result = await executor.execute(
            ActionRequest(
                kind=ActionKind.FILL,
                ref=pw_ref,
                text="hunter2",
                source_snapshot_id=snap.snapshot_id,
            )
        )
        assert result.status is ActionStatus.REFUSED
        # Confirmation cannot unlock it: blocked means blocked.
        executor.confirm(result.action_id)
        again = await executor.execute(
            ActionRequest(
                kind=ActionKind.FILL,
                ref=pw_ref,
                text="hunter2",
                source_snapshot_id=snap.snapshot_id,
            )
        )
        assert again.status is ActionStatus.REFUSED

    async def test_navigation_outside_the_allowlist_is_blocked(
        self, session: BrowserSession, fixture_server: str
    ) -> None:
        await session.goto(fixture_server)
        with pytest.raises(NavigationBlockedError):
            await session.goto("https://example.com/")

    async def test_typed_text_is_redacted_from_logs(self) -> None:
        request = ActionRequest(kind=ActionKind.FILL, ref="e1", text="hunter2")
        assert "hunter2" not in str(request.as_dict())


class TestVerification:
    async def test_click_that_changes_the_page_is_verified(
        self,
        session: BrowserSession,
        snapshotter: Snapshotter,
        executor: ActionExecutor,
        fixture_server: str,
    ) -> None:
        snap = await _goto_and_snapshot(session, snapshotter, fixture_server)
        ref = _ref_for(snap, name="Show details")
        result = await executor.execute(
            ActionRequest(kind=ActionKind.CLICK, ref=ref, source_snapshot_id=snap.snapshot_id)  # type: ignore[attr-defined]
        )
        assert result.status is ActionStatus.SUCCEEDED
        assert result.verified is True
        assert result.result_snapshot_id is not None
        assert result.evidence

    async def test_navigation_is_verified_by_url_change(
        self,
        session: BrowserSession,
        snapshotter: Snapshotter,
        executor: ActionExecutor,
        fixture_server: str,
    ) -> None:
        snap = await _goto_and_snapshot(session, snapshotter, fixture_server)
        result = await executor.execute(
            ActionRequest(
                kind=ActionKind.NAVIGATE,
                url=f"{fixture_server}/analytics",
                source_snapshot_id=snap.snapshot_id,  # type: ignore[attr-defined]
            )
        )
        assert result.verified is True
        assert "url changed" in result.verification_detail

    async def test_scroll_is_verified_by_viewport_membership(
        self,
        session: BrowserSession,
        snapshotter: Snapshotter,
        executor: ActionExecutor,
        fixture_server: str,
    ) -> None:
        snap = await _goto_and_snapshot(session, snapshotter, fixture_server)
        below = next(
            (n.ref for n in snap.root.walk() if n.ref and "Footer note" in (n.value or "")),  # type: ignore[attr-defined]
            None,
        )
        if below is None:
            pytest.skip("fixture footer did not receive a ref in this render")
        result = await executor.execute(
            ActionRequest(kind=ActionKind.SCROLL, ref=below, source_snapshot_id=snap.snapshot_id)  # type: ignore[attr-defined]
        )
        assert result.verified is True
        assert "viewport" in result.verification_detail

    async def test_unknown_ref_fails_without_guessing(
        self,
        session: BrowserSession,
        snapshotter: Snapshotter,
        executor: ActionExecutor,
        fixture_server: str,
    ) -> None:
        snap = await _goto_and_snapshot(session, snapshotter, fixture_server)
        result = await executor.execute(
            ActionRequest(
                kind=ActionKind.CLICK,
                ref="e99999",
                source_snapshot_id=snap.snapshot_id,  # type: ignore[attr-defined]
            )
        )
        assert result.status is ActionStatus.FAILED
        assert result.verified is False
        assert result.error is not None
        assert result.error["code"] == "ref_not_found"


class TestGuards:
    async def test_dialog_is_dismissed_not_accepted(
        self, session: BrowserSession, snapshotter: Snapshotter, fixture_server: str
    ) -> None:
        """A confirm() must never be auto-accepted: that is taking an action."""
        await session.goto(fixture_server)
        await session.page.get_by_role("button", name="Delete workspace").click()
        await session.page.wait_for_timeout(600)
        # Assert on rendered text, not page.content(): the fixture's own <script>
        # source contains the string, so a raw HTML check would always fail.
        assert "Workspace deleted" not in await _visible_text(session)
        assert session.guards.events.dialogs

    async def test_download_is_diverted_to_the_sandbox(
        self, session: BrowserSession, fixture_server: str
    ) -> None:
        await session.goto(fixture_server)
        await session.page.get_by_role("button", name="Export report").click()
        await session.page.wait_for_timeout(1200)
        assert session.guards.events.downloads

    async def test_overlay_survives_client_side_navigation(
        self, session: BrowserSession, fixture_server: str
    ) -> None:
        await session.goto(fixture_server)
        await session.page.get_by_role("link", name="Team").click()
        await session.page.wait_for_timeout(300)
        present = await session.page.evaluate("() => !!document.querySelector('karumi-glass')")
        assert present is True

    async def test_overlay_is_invisible_to_the_snapshot(
        self, session: BrowserSession, snapshotter: Snapshotter, fixture_server: str
    ) -> None:
        """pointer-events:none means Playwright never refs our own overlay."""
        snap = await _goto_and_snapshot(session, snapshotter, fixture_server)
        assert "karumi-glass" not in snap.render()  # type: ignore[attr-defined]


class TestLoginWall:
    async def test_detects_a_visible_password_field(
        self, session: BrowserSession, fixture_server: str
    ) -> None:
        await session.goto(fixture_server)
        assert await session.count_password_fields() == 0
        await session.page.get_by_role("link", name="Settings").click()
        await session.page.wait_for_timeout(300)
        assert await session.count_password_fields() == 1
