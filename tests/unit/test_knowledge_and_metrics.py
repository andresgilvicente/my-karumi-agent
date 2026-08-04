"""Site learning and latency reporting."""

from __future__ import annotations

import pytest

from karumi_agent.agent.knowledge import (
    PageKnowledge,
    SiteKnowledge,
    _clean_title,
    _is_followable,
    _TextExtractor,
)
from karumi_agent.metrics.timeline import Timeline, percentiles

HTML = """
<!doctype html><html><head>
<title>Karumi AI: The Agentic Demo Platform</title>
<meta name="description" content="The agentic demo platform for instant demos.">
<script>var secret = "should not be extracted";</script>
<style>body { color: red; }</style>
</head><body>
<h1>Instant product demos</h1>
<h2>How it works</h2>
<p>Real content that should be extracted.</p>
<a href="/pricing">Pricing</a>
<a href="https://other-site.example/x">Off site</a>
</body></html>
"""


class TestTextExtraction:
    def test_extracts_title_description_and_headings(self) -> None:
        parser = _TextExtractor()
        parser.feed(HTML)
        assert "Karumi AI" in parser.title
        assert parser.description.startswith("The agentic demo platform")
        assert "Instant product demos" in parser.headings
        assert "How it works" in parser.headings

    def test_ignores_script_and_style_content(self) -> None:
        """Script bodies are not page copy, and must not become 'knowledge'."""
        parser = _TextExtractor()
        parser.feed(HTML)
        assert "should not be extracted" not in parser.text
        assert "color: red" not in parser.text
        assert "Real content" in parser.text

    def test_survives_malformed_html(self) -> None:
        parser = _TextExtractor()
        parser.feed("<html><body><h1>Unclosed<div><p>text")
        assert parser.text


class TestLinkFollowing:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://example.com/about", True),
            ("https://blog.example.com/post", True),  # same registrable domain
            ("https://other.test/page", False),
            ("https://example.com/file.pdf", False),
            ("https://example.com/app.js", False),
            ("mailto:someone@example.com", False),
            ("javascript:alert(1)", False),
        ],
    )
    def test_only_follows_same_site_documents(self, url: str, expected: bool) -> None:
        assert _is_followable(url, "https://example.com") is expected


class TestTitleCleaning:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Karumi AI | Demos", "Karumi AI"),
            ("Product — Company", "Product"),
            ("Plain Title", "Plain Title"),
        ],
    )
    def test_strips_site_suffixes(self, raw: str, expected: str) -> None:
        assert _clean_title(raw) == expected


class TestSiteKnowledge:
    def test_first_page_supplies_the_product_name(self) -> None:
        site = SiteKnowledge(root_url="https://example.com")
        site.add(PageKnowledge(url="https://example.com", title="Acme | Home"))
        assert site.product_name == "Acme"

    def test_prompt_block_is_sourced_and_bounded(self) -> None:
        site = SiteKnowledge(root_url="https://example.com")
        for i in range(20):
            site.add(
                PageKnowledge(
                    url=f"https://example.com/{i}", title=f"P{i}", headings=[f"Heading {i}"]
                )
            )
        block = site.as_prompt_block()
        assert "<site_knowledge>" in block
        # Bounded so background learning cannot flood the prompt.
        assert block.count("- https://example.com/") <= 6

    def test_empty_knowledge_renders_nothing(self) -> None:
        assert SiteKnowledge(root_url="https://example.com").as_prompt_block() == ""


class TestPercentiles:
    def test_reports_observed_values_not_interpolated_ones(self) -> None:
        """Nearest-rank: with few samples an interpolated P95 invents a number."""
        result = percentiles([1.0, 2.0, 3.0, 4.0, 100.0])
        assert result is not None
        assert result.p50 in {2.0, 3.0}
        assert result.maximum == 100.0
        assert result.p95 == 100.0

    def test_empty_sample_returns_none(self) -> None:
        assert percentiles([]) is None

    def test_single_value(self) -> None:
        result = percentiles([2.5])
        assert result is not None
        assert result.p50 == result.p95 == 2.5


class TestTimeline:
    def test_groups_and_reports_by_stage(self) -> None:
        timeline = Timeline()
        for value in (1.0, 2.0, 3.0):
            timeline.record("llm_ttft", value)
        timeline.record("snapshot", 0.02)

        report = timeline.report()
        assert report["llm_ttft"]["count"] == 3
        assert report["snapshot"]["count"] == 1

    def test_stages_appear_in_canonical_order(self) -> None:
        timeline = Timeline()
        timeline.record("turn_total", 3.0)
        timeline.record("stt", 0.3)
        assert list(timeline.report()) == ["stt", "turn_total"]

    def test_measure_context_manager_records(self) -> None:
        timeline = Timeline()
        with timeline.measure("snapshot"):
            pass
        assert timeline.report()["snapshot"]["count"] == 1

    def test_writes_jsonl_when_given_a_path(self, tmp_path: object) -> None:
        from pathlib import Path

        assert isinstance(tmp_path, Path)
        path = tmp_path / "nested" / "latency.jsonl"
        timeline = Timeline(path=path)
        timeline.record("llm_ttft", 0.9, turn_id="t1")
        assert path.exists()
        assert "llm_ttft" in path.read_text()
