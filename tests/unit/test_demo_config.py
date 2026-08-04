"""Editorial config validation.

The brief forbids selectors, routes, coordinates and click scripts in product
configuration. These tests are what turn that from a promise into an invariant:
if someone later adds `#export-button` to a YAML file, the loader refuses and a
test fails.
"""

from __future__ import annotations

import pytest

from karumi_agent.domain.demo import (
    EditorialConfig,
    load_editorial_config,
    parse_editorial_config,
)
from karumi_agent.errors import EditorialConfigError


class TestForbiddenContent:
    @pytest.mark.parametrize(
        ("value", "why"),
        [
            ("#export-button", "CSS id selector"),
            (".metric-card", "CSS class selector"),
            ('[data-testid="revenue"]', "attribute selector"),
            ("div > button", "CSS combinator"),
            ("document.querySelector('.kpi')", "DOM query"),
            ("//*[@id='root']", "XPath"),
            ("click at 420, 380", "coordinates"),
            ("/analytics", "hardcoded route"),
            ("Step 1: open the dashboard", "click script"),
            ("Open the menu then click Export", "click script"),
        ],
    )
    def test_rejects_page_mechanics(self, value: str, why: str) -> None:
        with pytest.raises(EditorialConfigError):
            parse_editorial_config({"themes": [value]})

    def test_rejects_mechanics_in_any_field(self) -> None:
        for field in (
            "demo_goals",
            "themes",
            "approved_messages",
            "explicit_facts",
            "claims_to_avoid",
        ):
            with pytest.raises(EditorialConfigError):
                parse_editorial_config({field: ["#some-id"]})

    def test_rejects_unknown_keys(self) -> None:
        with pytest.raises(EditorialConfigError, match="Unknown"):
            parse_editorial_config({"click_path": ["a", "b"]})

    def test_rejects_wrong_types(self) -> None:
        with pytest.raises(EditorialConfigError):
            parse_editorial_config({"themes": "not a list"})
        with pytest.raises(EditorialConfigError):
            parse_editorial_config({"themes": [123]})


class TestAcceptedContent:
    def test_accepts_ordinary_editorial_guidance(self) -> None:
        config = parse_editorial_config(
            {
                "product_name": "Metric Master Suite",
                "demo_goals": ["Show how quickly a team can read their numbers"],
                "themes": ["Revenue trends", "Team management", "Admin controls"],
                "approved_messages": ["Everything updates in real time"],
                "explicit_facts": ["The product is used by analytics teams"],
                "claims_to_avoid": ["Do not quote pricing", "Do not name customers"],
            }
        )
        assert config.product_name == "Metric Master Suite"
        assert len(config.themes) == 3

    def test_prose_mentioning_a_button_is_fine(self) -> None:
        """We forbid selectors, not the English language."""
        config = parse_editorial_config(
            {"themes": ["The export button is worth showing if you find one"]}
        )
        assert config.themes

    def test_empty_config_is_valid(self) -> None:
        assert parse_editorial_config({}).themes == []


class TestPromptRendering:
    def test_renders_guidance_as_preferences(self) -> None:
        block = EditorialConfig(
            product_name="Acme",
            themes=["Revenue"],
            claims_to_avoid=["pricing"],
        ).as_prompt_block()
        assert "<demo_guidance>" in block
        # Framed conditionally: the live page decides what is actually there.
        assert "if present" in block
        assert "Never claim" in block

    def test_empty_config_renders_nothing(self) -> None:
        assert EditorialConfig().as_prompt_block() == ""


class TestLoading:
    def test_missing_file_is_not_an_error(self, tmp_path: object) -> None:
        """An arbitrary URL has no editorial config, and that is normal."""
        from pathlib import Path

        assert load_editorial_config(Path("/nonexistent/whatever.yaml")) is None

    def test_none_path_returns_none(self) -> None:
        assert load_editorial_config(None) is None

    def test_shipped_config_is_valid(self) -> None:
        """The config we ship must satisfy our own rules."""
        from pathlib import Path

        path = Path("configs/metric-master-suite.yaml")
        if not path.exists():
            pytest.skip("shipped config not present in this checkout")
        config = load_editorial_config(path)
        assert config is not None
        assert config.themes
