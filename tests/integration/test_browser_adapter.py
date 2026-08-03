"""The browser adapter against a real Chromium and a real page.

Every assertion here is about a property the agent depends on and that cannot
be established without a browser: ref stability under mutation, cross-frame ref
namespacing, verified actions, and refusal to act on a page the model no longer
has grounds to trust.
"""

from __future__ import annotations

import pytest

from karumi_agent.browser.playwright_adapter import PlaywrightBrowserAdapter
from karumi_agent.domain import query
from karumi_agent.domain.models import Action, ActionKind, Capability, ErrorCode
from karumi_agent.tools.policy import ActionPolicy

pytestmark = pytest.mark.browser


def action(kind: ActionKind, snapshot_id: str, **kwargs: object) -> Action:
    return Action(action_id="t1", kind=kind, snapshot_id=snapshot_id, **kwargs)  # type: ignore[arg-type]


class TestObservation:
    async def test_produces_a_semantic_tree(self, adapter: PlaywrightBrowserAdapter) -> None:
        snapshot = await adapter.observe(force=True)

        assert snapshot.node_count() > 20
        assert query.headings(snapshot)
        assert query.navigation_controls(snapshot)

    async def test_reads_a_canvas_through_its_accessible_name(
        self, adapter: PlaywrightBrowserAdapter
    ) -> None:
        """A chart with no text is legible only via ARIA — the realistic case."""
        snapshot = await adapter.observe(force=True)
        images = query.by_role(snapshot, "img")

        assert any("Q1" in (node.name or "") for node in images)

    async def test_tags_content_below_the_fold_as_offscreen(
        self, adapter: PlaywrightBrowserAdapter
    ) -> None:
        snapshot = await adapter.observe(force=True)

        assert query.offscreen(snapshot, "heading")
        assert not all(node.offscreen for node in query.headings(snapshot))

    async def test_namespaces_refs_across_frames(
        self, adapter: PlaywrightBrowserAdapter
    ) -> None:
        """Both realms hand out `e1`; the host must keep them apart."""
        snapshot = await adapter.observe(force=True)
        prefixes = {node.ref.split(".")[0] for node in snapshot.root.walk() if node.ref}

        assert {"f0", "f1"} <= prefixes
        assert snapshot.frame_count >= 2
        assert len(snapshot.index()) == len([n for n in snapshot.root.walk() if n.ref])

    async def test_refs_survive_continuous_dom_mutation(
        self, adapter: PlaywrightBrowserAdapter
    ) -> None:
        """The assumption the whole architecture rests on."""
        first = await adapter.observe(force=True)
        tracked = {
            node.ref: node.name
            for node in query.interactive(first)
            if node.ref and node.name
        }
        assert tracked

        for _ in range(10):
            await adapter.page.wait_for_timeout(60)
            later = (await adapter.observe(force=True)).index()
            for ref, name in tracked.items():
                assert ref in later, f"{ref} vanished"
                assert later[ref].name == name


class TestVerifiedActions:
    async def test_scroll_brings_an_offscreen_element_into_view(
        self, adapter: PlaywrightBrowserAdapter
    ) -> None:
        snapshot = await adapter.observe(force=True)
        target = query.offscreen(snapshot, "heading")[0]

        result = await adapter.perform(
            action(ActionKind.SCROLL_TO, snapshot.snapshot_id, ref=target.ref)
        )

        assert result.ok and result.verified
        assert result.evidence is not None
        assert result.evidence.element_after is not None
        assert result.evidence.element_after.in_viewport

    async def test_click_on_navigation_is_confirmed_by_a_re_render(
        self, adapter: PlaywrightBrowserAdapter
    ) -> None:
        snapshot = await adapter.observe(force=True)
        title = snapshot.page.title.lower()
        target = next(
            node
            for node in query.navigation_controls(snapshot)
            if node.name and node.name.lower() not in title
        )

        result = await adapter.perform(
            action(ActionKind.CLICK, snapshot.snapshot_id, ref=target.ref)
        )

        assert result.verified, result.summary()
        assert result.evidence is not None
        assert result.evidence.refs_added > 0

    async def test_action_on_a_vanished_element_is_rejected(
        self, adapter: PlaywrightBrowserAdapter
    ) -> None:
        """Navigating away destroys the old view's elements; refs must not resolve."""
        snapshot = await adapter.observe(force=True)
        doomed = next(
            node
            for node in query.by_role(snapshot, "img", "table", "paragraph")
            if node.ref and node.ref.startswith("f0")
        )

        nav = next(
            node
            for node in query.navigation_controls(snapshot)
            if node.name and node.name.lower() not in snapshot.page.title.lower()
        )
        await adapter.perform(action(ActionKind.CLICK, snapshot.snapshot_id, ref=nav.ref))

        current = adapter.current
        assert current is not None
        result = await adapter.perform(
            action(ActionKind.HIGHLIGHT, current.snapshot_id, ref=doomed.ref)
        )

        assert not result.ok
        assert result.error_code is ErrorCode.REF_NOT_FOUND

    async def test_timings_are_always_reported(
        self, adapter: PlaywrightBrowserAdapter
    ) -> None:
        snapshot = await adapter.observe(force=True)
        target = query.headings(snapshot)[0]

        result = await adapter.perform(
            action(ActionKind.HIGHLIGHT, snapshot.snapshot_id, ref=target.ref)
        )

        assert result.timings.execute_ms >= 0
        assert result.timings.verify_ms > 0
        assert result.timings.total_ms > 0


class TestStaleness:
    async def test_unknown_snapshot_id_is_refused(
        self, adapter: PlaywrightBrowserAdapter
    ) -> None:
        result = await adapter.perform(
            action(ActionKind.HIGHLIGHT, "s-never-issued", ref="f0.e1")
        )

        assert not result.ok
        assert result.error_code is ErrorCode.STALE_SNAPSHOT

    async def test_snapshot_from_before_a_navigation_is_refused(
        self, adapter: PlaywrightBrowserAdapter, fixture_site: str
    ) -> None:
        before = await adapter.observe(force=True)
        target = query.headings(before)[0]

        moved = await adapter.perform(
            action(ActionKind.NAVIGATE, before.snapshot_id, url=f"{fixture_site}/embed.html")
        )
        assert moved.verified

        replay = await adapter.perform(
            action(ActionKind.HIGHLIGHT, before.snapshot_id, ref=target.ref)
        )

        assert not replay.ok
        assert replay.error_code is ErrorCode.STALE_SNAPSHOT

    async def test_a_current_snapshot_is_accepted(
        self, adapter: PlaywrightBrowserAdapter
    ) -> None:
        """Freshness must not be so strict that ordinary use is impossible.

        Snapshots stream every few hundred milliseconds; if each one invalidated
        the last, no action would ever be allowed to run.
        """
        snapshot = await adapter.observe(force=True)
        target = query.headings(snapshot)[0]

        await adapter.page.wait_for_timeout(700)  # several stream ticks
        await adapter.observe(force=True)

        result = await adapter.perform(
            action(ActionKind.HIGHLIGHT, snapshot.snapshot_id, ref=target.ref)
        )

        assert result.ok, result.message


class TestPolicyEnforcement:
    async def test_destructive_control_requires_confirmation(
        self, browser_settings, fixture_site: str
    ) -> None:
        adapter = PlaywrightBrowserAdapter(browser_settings)
        await adapter.start(fixture_site)
        try:
            snapshot = await adapter.observe(force=True)
            target = query.by_name(snapshot, r"\bdelete\b", roles={"button"})[0]

            result = await adapter.perform(
                action(ActionKind.CLICK, snapshot.snapshot_id, ref=target.ref)
            )

            assert not result.ok
            assert result.error_code is ErrorCode.NEEDS_CONFIRMATION
        finally:
            await adapter.close()

    async def test_disabled_control_is_refused(
        self, adapter: PlaywrightBrowserAdapter
    ) -> None:
        snapshot = await adapter.observe(force=True)
        target = next(node for node in snapshot.root.walk() if "disabled" in node.state)

        result = await adapter.perform(
            action(ActionKind.CLICK, snapshot.snapshot_id, ref=target.ref)
        )

        assert result.error_code is ErrorCode.ELEMENT_DISABLED

    async def test_off_origin_navigation_is_blocked(
        self, adapter: PlaywrightBrowserAdapter
    ) -> None:
        snapshot = await adapter.observe(force=True)

        result = await adapter.perform(
            action(ActionKind.NAVIGATE, snapshot.snapshot_id, url="https://example.com/")
        )

        assert not result.ok
        assert result.error_code is ErrorCode.NAVIGATION_BLOCKED

    async def test_password_field_is_never_written_to(
        self, browser_settings, fixture_site: str
    ) -> None:
        policy = ActionPolicy(
            capabilities=frozenset({Capability.CLICK, Capability.FILL, Capability.READ})
        )
        adapter = PlaywrightBrowserAdapter(browser_settings, policy=policy)
        await adapter.start(fixture_site)
        try:
            snapshot = await adapter.observe(force=True)
            settings_tab = query.by_name(snapshot, r"settings", roles={"button"})[0]
            await adapter.perform(
                action(ActionKind.CLICK, snapshot.snapshot_id, ref=settings_tab.ref)
            )

            snapshot = await adapter.observe(force=True)
            password_ref = None
            for node in query.text_fields(snapshot):
                state = await adapter.describe(node.ref)
                if state and (state.type or "").lower() == "password":
                    password_ref = node.ref
                    break
            assert password_ref is not None, "fixture should expose a password field"

            result = await adapter.perform(
                action(
                    ActionKind.FILL,
                    snapshot.snapshot_id,
                    ref=password_ref,
                    value="must-not-be-typed",
                )
            )

            assert not result.ok
            assert result.error_code is ErrorCode.BLOCKED_BY_POLICY

            # And the page really was left alone.
            value = await adapter.page.evaluate(
                "() => document.querySelector('input[type=password]').value"
            )
            assert value == "hunter2"
        finally:
            await adapter.close()
