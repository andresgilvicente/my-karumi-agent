"""Rendering and budgeting of `<ui_state>`.

The rendering tests exist to catch drift from Pipecat's own renderer. If
upstream changes the wire format, these fail here rather than showing up as a
model that has quietly stopped understanding its own context.
"""

from __future__ import annotations

from karumi_agent.domain.models import A11yNode, PageInfo, Selection, Snapshot
from karumi_agent.domain.ui_state import COLLAPSED, prune, render_ui_state


def node(ref: str, role: str, **kwargs: object) -> A11yNode:
    return A11yNode(ref=ref, role=role, **kwargs)  # type: ignore[arg-type]


def snapshot(root: A11yNode, selection: Selection | None = None) -> Snapshot:
    return Snapshot(
        snapshot_id="s1",
        captured_at=1_700_000_000_000,
        page=PageInfo(url="https://example.test/", title="Example"),
        root=root,
        selection=selection,
    )


class TestRender:
    def test_matches_pipecat_line_format(self) -> None:
        root = node(
            "e1",
            "generic",
            children=[
                node("e2", "heading", name="Revenue", level=2),
                node("e3", "button", name="Export", state=["focused"]),
            ],
        )

        assert render_ui_state(snapshot(root)) == (
            "<ui_state>\n"
            "- generic [ref=e1]:\n"
            '  - heading "Revenue" [level=2] [ref=e2]\n'
            '  - button "Export" [focused] [ref=e3]\n'
            "</ui_state>"
        )

    def test_value_grid_and_multiple_states(self) -> None:
        root = node(
            "e1",
            "grid",
            colcount=8,
            rowcount=2,
            children=[
                node(
                    "e2",
                    "textbox",
                    name="Email",
                    value="a@b.c",
                    state=["focused", "disabled"],
                )
            ],
        )
        rendered = render_ui_state(snapshot(root))

        assert "- grid [cols=8] [rows=2] [ref=e1]:" in rendered
        assert '  - textbox "Email" = "a@b.c" [focused] [disabled] [ref=e2]' in rendered

    def test_trailing_colon_only_when_children(self) -> None:
        rendered = render_ui_state(snapshot(node("e1", "generic")))
        assert "- generic [ref=e1]\n" in rendered + "\n"
        assert "- generic [ref=e1]:" not in rendered

    def test_selection_block_is_appended_inside_ui_state(self) -> None:
        rendered = render_ui_state(
            snapshot(
                node("e1", "generic", children=[node("e2", "paragraph")]),
                selection=Selection(ref="e2", text="the selected words"),
            )
        )
        assert rendered.endswith(
            '<selection ref="e2">\nthe selected words\n</selection>\n</ui_state>'
        )

    def test_selection_omitted_when_empty(self) -> None:
        rendered = render_ui_state(
            snapshot(node("e1", "generic"), selection=Selection(ref="e2", text=""))
        )
        assert "<selection" not in rendered


class TestPrune:
    def _wide(self, count: int, *, offscreen: bool) -> A11yNode:
        state = ["offscreen"] if offscreen else []
        return node(
            "e1",
            "generic",
            children=[
                node(
                    f"c{i}",
                    "region",
                    state=list(state),
                    children=[node(f"c{i}-{j}", "text") for j in range(9)],
                )
                for i in range(count)
            ],
        )

    def test_within_budget_is_untouched(self) -> None:
        snap = snapshot(self._wide(3, offscreen=True))
        before = render_ui_state(snap)

        stats = prune(snap, max_nodes=1000, max_depth=20)

        assert stats.nodes_before == stats.nodes_after
        assert stats.collapsed_subtrees == 0
        assert render_ui_state(snap) == before

    def test_collapses_offscreen_subtrees_first(self) -> None:
        snap = snapshot(self._wide(10, offscreen=True))
        stats = prune(snap, max_nodes=40, max_depth=20)

        assert stats.nodes_after <= 40
        assert stats.collapsed_subtrees > 0
        assert COLLAPSED in render_ui_state(snap)

    def test_never_collapses_visible_content(self) -> None:
        """Visible material is what the demo is about; it is never elided."""
        snap = snapshot(self._wide(10, offscreen=False))
        prune(snap, max_nodes=20, max_depth=20)

        collapsed = [n for n in snap.root.walk() if COLLAPSED in n.state and not n.offscreen]
        assert collapsed == []

    def test_depth_cap_is_the_last_resort(self) -> None:
        deep: A11yNode = node("leaf", "text")
        for i in range(30):
            deep = node(f"n{i}", "generic", children=[deep])

        snap = snapshot(deep)
        stats = prune(snap, max_nodes=5, max_depth=4)

        assert stats.depth_capped
        assert stats.nodes_after < stats.nodes_before

    def test_long_names_are_truncated(self) -> None:
        snap = snapshot(node("e1", "generic", children=[node("e2", "text", name="x" * 400)]))
        prune(snap, max_nodes=1000, max_depth=20)

        name = snap.root.children[0].name
        assert name is not None
        assert len(name) <= 100
        assert name.endswith("…")
