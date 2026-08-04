"""Snapshot parsing, budgeting and rendering.

The parser's input comes from a page we do not control, so these tests care as
much about *not crashing* on malformed input as about correctness on good input.
"""

from __future__ import annotations

from karumi_agent.domain.snapshot import Box, PageSnapshot, Viewport, parse_aria_snapshot


def _parse(text: str) -> PageSnapshot:
    return parse_aria_snapshot(
        text,
        url="https://example.test/",
        title="Test",
        viewport=Viewport(1440, 900),
    )


class TestParsingRealOutput:
    """Against the snapshot captured from the live demo target."""

    def test_extracts_refs(self, login_snapshot: PageSnapshot) -> None:
        refs = login_snapshot.refs()
        assert "e12" in refs  # email textbox
        assert "e15" in refs  # password textbox
        assert "e21" in refs  # sign-up button

    def test_disabled_control_has_no_ref(self, login_snapshot: PageSnapshot) -> None:
        """Playwright only refs elements that receive pointer events.

        The disabled "Sign In" button therefore has no ref at all, which means the
        model cannot express clicking it. This is a load-bearing property: a
        disabled control is unreachable by construction, not because we check.
        """
        nodes = [n for n in login_snapshot.root.walk() if n.name == "Sign In"]
        assert len(nodes) == 1
        assert nodes[0].ref is None
        assert "disabled" in nodes[0].states

    def test_parses_name_level_and_value(self, login_snapshot: PageSnapshot) -> None:
        heading = login_snapshot.resolve("e6")
        assert heading is not None
        assert heading.role == "heading"
        assert heading.name == "Welcome back"
        assert heading.level == 3

        paragraph = login_snapshot.resolve("e7")
        assert paragraph is not None
        assert paragraph.value == "Sign in to access the chat application"

    def test_parses_placeholder_as_property_not_child(self, login_snapshot: PageSnapshot) -> None:
        email = login_snapshot.resolve("e12")
        assert email is not None
        assert email.placeholder == "Enter your email"
        # The /placeholder line must not become a node of its own.
        assert all(child.role != "/placeholder" for child in email.children)

    def test_parses_box_geometry(self, login_snapshot: PageSnapshot) -> None:
        email = login_snapshot.resolve("e12")
        assert email is not None
        assert email.box == Box(521, 405, 398, 32)

    def test_detects_login_wall(self, login_snapshot: PageSnapshot) -> None:
        """The demo target's landing route is a sign-in wall."""
        assert login_snapshot.has_password_field is True

    def test_parses_url_property(self, dashboard_snapshot: PageSnapshot) -> None:
        link = dashboard_snapshot.resolve("e4")
        assert link is not None
        assert link.url == "/analytics"
        assert link.name == "Analytics"
        assert link.cursor_pointer is True


class TestRobustness:
    """A page we do not control must never be able to crash the parser."""

    def test_empty_input(self) -> None:
        snap = _parse("")
        assert snap.node_count == 0
        assert snap.refs() == []

    def test_malformed_lines_become_text_nodes(self) -> None:
        snap = _parse('- \n- [[[garbage\n   not a list item at all\n- button "OK" [ref=e1]')
        # Nothing raised, and the good line still parsed.
        assert "e1" in snap.refs()

    def test_unterminated_quote_does_not_hang(self) -> None:
        snap = _parse('- button "unterminated [ref=e1]')
        assert snap.node_count >= 1

    def test_absurd_indentation(self) -> None:
        snap = _parse("- a [ref=e1]\n" + " " * 200 + "- b [ref=e2]\n- c [ref=e3]")
        assert set(snap.refs()) == {"e1", "e2", "e3"}

    def test_very_long_accessible_name_is_kept_verbatim(self) -> None:
        long_name = "x" * 900
        snap = _parse(f'- button "{long_name}" [ref=e1]')
        node = snap.resolve("e1")
        assert node is not None
        assert node.name == long_name

    def test_brackets_inside_quoted_name(self) -> None:
        snap = _parse('- button "Delete [all] items" [ref=e1]')
        node = snap.resolve("e1")
        assert node is not None
        assert node.name == "Delete [all] items"
        assert node.ref == "e1"


class TestBudgets:
    """A large page must be trimmed, and must say that it was."""

    def test_node_budget_trims_and_flags(self, dashboard_snapshot: PageSnapshot) -> None:
        text = dashboard_snapshot.render(max_nodes=5, max_tokens=10_000)
        assert dashboard_snapshot.truncated is True
        assert len(text.splitlines()) <= dashboard_snapshot.node_count

    def test_token_budget_shrinks_output(self) -> None:
        big = "\n".join(f'- button "Item number {i}" [ref=e{i}]' for i in range(400))
        snap = _parse(big)
        text = snap.render(max_nodes=400, max_tokens=100)
        assert len(text) // 4 <= 400  # generous slack around the 100-token target
        assert snap.truncated is True

    def test_viewport_priority_keeps_visible_content_first(
        self, dashboard_snapshot: PageSnapshot
    ) -> None:
        """Content below the fold should be dropped before content on screen."""
        text = dashboard_snapshot.render(max_nodes=8, max_tokens=10_000, prioritise_viewport=True)
        assert "Overview" in text
        # The footer note sits at y=1400, far outside a 900px viewport.
        assert "Footer note" not in text

    def test_ui_state_block_is_fenced_and_identifies_the_page(
        self, dashboard_snapshot: PageSnapshot
    ) -> None:
        block = dashboard_snapshot.render_ui_state()
        assert block.startswith("<ui_state ")
        assert 'url="https://example.test/dashboard"' in block
        assert block.rstrip().endswith("</ui_state>")

    def test_ui_state_omits_the_snapshot_id(self, dashboard_snapshot: PageSnapshot) -> None:
        """The id must not reach the prompt: it changes every observation and
        would invalidate the KV cache prefix on every single turn."""
        assert "snap-dash" not in dashboard_snapshot.render_ui_state()

    def test_unchanged_page_renders_byte_identically(self) -> None:
        """The property the prefix cache depends on."""
        from karumi_agent.domain.snapshot import Viewport, parse_aria_snapshot

        text = '- button "Go" [ref=e1]\n- heading "Title" [level=1] [ref=e2]'
        first = parse_aria_snapshot(
            text, url="u", title="t", viewport=Viewport(1440, 900), snapshot_id="s1"
        )
        second = parse_aria_snapshot(
            text, url="u", title="t", viewport=Viewport(1440, 900), snapshot_id="s2"
        )
        assert first.render_ui_state() == second.render_ui_state()

    def test_truncation_is_disclosed_in_the_block(self, dashboard_snapshot: PageSnapshot) -> None:
        block = dashboard_snapshot.render_ui_state(max_nodes=4, max_tokens=10_000)
        assert "<note>" in block


class TestFind:
    """Cheap targeted lookup instead of dumping the whole page."""

    def test_finds_by_name(self, dashboard_snapshot: PageSnapshot) -> None:
        result = dashboard_snapshot.find("Revenue")
        assert "Revenue" in result
        assert "ref=e13" in result

    def test_reports_no_match_without_inventing_one(self, dashboard_snapshot: PageSnapshot) -> None:
        result = dashboard_snapshot.find("nonexistent-widget")
        assert "no matches" in result

    def test_is_much_smaller_than_a_full_render(self, dashboard_snapshot: PageSnapshot) -> None:
        full = dashboard_snapshot.render()
        found = dashboard_snapshot.find("Revenue")
        assert len(found) < len(full)


class TestStaleness:
    def test_age_and_expiry(self, dashboard_snapshot: PageSnapshot) -> None:
        assert dashboard_snapshot.age_s(now=1_000_005.0) == 5.0
        assert dashboard_snapshot.is_expired(3.0, now=1_000_005.0) is True
        assert dashboard_snapshot.is_expired(30.0, now=1_000_005.0) is False


class TestDiff:
    def test_diff_reports_only_new_lines(self) -> None:
        before = _parse('- button "A" [ref=e1]')
        after = _parse('- button "A" [ref=e1]\n- button "B" [ref=e2]')
        delta = after.diff(before)
        assert "B" in delta
        assert "A" not in delta

    def test_diff_against_none_returns_everything(self, dashboard_snapshot: PageSnapshot) -> None:
        delta = dashboard_snapshot.diff(None)
        assert "Overview" in delta
