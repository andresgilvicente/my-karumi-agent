"""Post-action verification.

The project's central promise is that the agent never claims an action worked
without evidence. These tests pin the shape of that promise: what counts as
confirmation for each action, and — more importantly — what does not.
"""

from __future__ import annotations

from karumi_agent.browser.verify import build_evidence, is_verified, verified_because
from karumi_agent.domain.models import (
    A11yNode,
    ActionKind,
    ElementState,
    PageInfo,
    Snapshot,
)


def page(url: str = "https://example.test/", title: str = "T", scroll_y: int = 0) -> PageInfo:
    return PageInfo(url=url, title=title, scroll_y=scroll_y)


def snap(page_info: PageInfo, refs: list[str], snapshot_id: str = "s1") -> Snapshot:
    return Snapshot(
        snapshot_id=snapshot_id,
        captured_at=1_700_000_000_000,
        page=page_info,
        root=A11yNode(
            ref="root",
            role="generic",
            children=[A11yNode(ref=ref, role="button") for ref in refs],
        ),
    )


def elem(**kwargs: object) -> ElementState:
    base: dict[str, object] = {
        "ref": "f0.e1",
        "tag": "button",
        "visible": True,
        "in_viewport": True,
        "rect": {"x": 0, "y": 0, "width": 100, "height": 30},
    }
    base.update(kwargs)
    return ElementState.model_validate(base)


class TestEvidence:
    def test_records_url_title_and_scroll_movement(self) -> None:
        evidence = build_evidence(
            before=snap(page(scroll_y=0), ["a"]),
            after=snap(page(url="https://example.test/next", title="N", scroll_y=800), ["a"]),
            element_before=None,
            element_after=None,
        )

        assert any("url" in c for c in evidence.changes)
        assert any("title" in c for c in evidence.changes)
        assert any("scrolled" in c for c in evidence.changes)

    def test_ignores_sub_threshold_scroll_jitter(self) -> None:
        evidence = build_evidence(
            before=snap(page(scroll_y=100), ["a"]),
            after=snap(page(scroll_y=104), ["a"]),
            element_before=None,
            element_after=None,
        )
        assert evidence.changes == []

    def test_counts_appearing_and_disappearing_elements(self) -> None:
        evidence = build_evidence(
            before=snap(page(), ["a", "b"]),
            after=snap(page(), ["b", "c", "d"]),
            element_before=None,
            element_after=None,
        )

        assert evidence.refs_added == 2
        assert evidence.refs_removed == 1


class TestVerdicts:
    def test_click_without_any_consequence_is_not_verified(self) -> None:
        """The failure this whole design exists to prevent."""
        evidence = build_evidence(
            before=snap(page(), ["a"]),
            after=snap(page(), ["a"]),
            element_before=elem(),
            element_after=elem(),
        )

        assert not is_verified(ActionKind.CLICK, evidence)
        assert "no observable change" in verified_because(ActionKind.CLICK, evidence)

    def test_click_with_a_re_render_is_verified(self) -> None:
        evidence = build_evidence(
            before=snap(page(), ["a", "b"]),
            after=snap(page(), ["c", "d"]),
            element_before=elem(),
            element_after=None,
        )
        assert is_verified(ActionKind.CLICK, evidence)

    def test_scroll_is_verified_when_the_target_reaches_the_viewport(self) -> None:
        evidence = build_evidence(
            before=snap(page(scroll_y=0), ["a"]),
            after=snap(page(scroll_y=900), ["a"]),
            element_before=elem(in_viewport=False),
            element_after=elem(in_viewport=True),
        )
        assert is_verified(ActionKind.SCROLL_TO, evidence)

    def test_scroll_to_an_already_visible_target_is_verified(self) -> None:
        """Nothing moved, but the user can see it — which is what was asked."""
        evidence = build_evidence(
            before=snap(page(), ["a"]),
            after=snap(page(), ["a"]),
            element_before=elem(in_viewport=True),
            element_after=elem(in_viewport=True),
        )

        assert is_verified(ActionKind.SCROLL_TO, evidence)
        assert "already within the viewport" in verified_because(ActionKind.SCROLL_TO, evidence)

    def test_highlight_needs_a_visible_target_not_a_page_change(self) -> None:
        visible = build_evidence(
            before=snap(page(), ["a"]),
            after=snap(page(), ["a"]),
            element_before=elem(),
            element_after=elem(),
        )
        hidden = build_evidence(
            before=snap(page(), ["a"]),
            after=snap(page(), ["a"]),
            element_before=elem(),
            element_after=elem(visible=False, in_viewport=False),
        )

        assert is_verified(ActionKind.HIGHLIGHT, visible)
        assert not is_verified(ActionKind.HIGHLIGHT, hidden)

    def test_fill_requires_the_value_to_have_actually_changed(self) -> None:
        unchanged = build_evidence(
            before=snap(page(), ["a"]),
            after=snap(page(), ["a"]),
            element_before=elem(value="old"),
            element_after=elem(value="old"),
        )
        changed = build_evidence(
            before=snap(page(), ["a"]),
            after=snap(page(), ["a"]),
            element_before=elem(value="old"),
            element_after=elem(value="new"),
        )

        assert not is_verified(ActionKind.FILL, unchanged)
        assert is_verified(ActionKind.FILL, changed)

    def test_navigation_requires_the_url_to_change(self) -> None:
        same = build_evidence(
            before=snap(page(), ["a"]),
            after=snap(page(), ["b"]),  # page re-rendered but never navigated
            element_before=None,
            element_after=None,
        )
        moved = build_evidence(
            before=snap(page(), ["a"]),
            after=snap(page(url="https://example.test/other"), ["a"]),
            element_before=None,
            element_after=None,
        )

        assert not is_verified(ActionKind.NAVIGATE, same)
        assert is_verified(ActionKind.NAVIGATE, moved)
