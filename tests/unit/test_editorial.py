"""Editorial knowledge — the only channel the agent is allowed to trust.

Two things matter here. The prompt has to carry the operator's intent, and it
has to keep the precedence rule explicit: the screen outranks the briefing.
The third test is the important one — it checks the shipped briefing does not
smuggle in the coupling the whole project is built to avoid.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from karumi_agent.knowledge.editorial import UNBRIEFED, ProductKnowledge, load_product

REPO_ROOT = Path(__file__).resolve().parents[2]
METRIC_MASTER = REPO_ROOT / "config" / "products" / "metric_master_suite.yaml"


class TestPromptRendering:
    def test_includes_every_section_that_was_filled_in(self) -> None:
        prompt = ProductKnowledge(
            name="Acme Analytics",
            url="https://acme.test/",
            summary="Dashboards for people who hate dashboards.",
            goals=["Show the product working"],
            highlights=["The main dashboard"],
            value_messages=["Numbers are visible without setup"],
            authorised_facts=["Free for teams under five people"],
            avoid_claims=["Pricing beyond the free tier"],
        ).as_prompt()

        assert "Acme Analytics" in prompt
        assert "Dashboards for people who hate dashboards." in prompt
        assert "Show the product working" in prompt
        assert "The main dashboard" in prompt
        assert "Numbers are visible without setup" in prompt
        assert "Free for teams under five people" in prompt
        assert "Pricing beyond the free tier" in prompt

    def test_marks_itself_as_operator_authored(self) -> None:
        """The model must be able to tell this apart from page content."""
        assert "trusted" in ProductKnowledge(name="X").as_prompt().lower()

    def test_states_that_the_screen_outranks_the_briefing(self) -> None:
        prompt = ProductKnowledge(name="X").as_prompt()
        assert "the screen is right" in prompt

    def test_empty_sections_are_omitted_rather_than_left_dangling(self) -> None:
        prompt = ProductKnowledge(name="X").as_prompt()
        assert "Goals for this demo" not in prompt
        assert "Never claim" not in prompt

    def test_unbriefed_still_renders(self) -> None:
        """Running against an arbitrary URL with no briefing is a valid mode."""
        assert UNBRIEFED.as_prompt()


class TestLoading:
    def test_rejects_unknown_keys(self) -> None:
        """A typo in a briefing should fail loudly, not silently do nothing."""
        with pytest.raises(ValidationError):
            ProductKnowledge.model_validate({"name": "X", "gaols": ["typo"]})

    def test_loads_the_shipped_briefing(self) -> None:
        product = load_product(METRIC_MASTER)

        assert product.name == "Metric Master Suite"
        assert product.goals
        assert product.avoid_claims

    def test_shipped_briefing_contains_no_implementation_coupling(self) -> None:
        """Editorial config describes intent. The moment it names a selector, an
        id or a route, the agent stops adapting to the live page and starts
        depending on a particular build of it.

        Checked against the authored fields rather than the rendered prompt, so
        our own markdown scaffolding cannot trip it.
        """
        product = load_product(METRIC_MASTER)
        authored = " ".join(
            [
                product.summary,
                *product.goals,
                *product.highlights,
                *product.value_messages,
                *product.authorised_facts,
                *product.avoid_claims,
            ]
        ).lower()

        for smell in (
            "css",
            "selector",
            "xpath",
            "data-testid",
            "class=",
            "<div",
            "#",
            "querySelector".lower(),
        ):
            assert smell not in authored, f"briefing leaks implementation detail: {smell!r}"
