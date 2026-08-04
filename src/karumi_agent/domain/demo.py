"""Editorial configuration, and the rules about what it may not contain.

Product config is allowed to say *what a demo is about*: goals, themes worth
covering, approved messaging, facts the operator has supplied, claims to avoid.

It is **not** allowed to say *how to operate the page*. No CSS selectors, no
element ids, no coordinates, no hardcoded routes, no ordered click scripts. The
agent has to find things by observing the live page, which is the whole point --
a config that could name `#export-btn` would be a demo that breaks the moment the
site ships a redesign, and it would make the agent a puppet rather than an
observer.

The interesting part is that this is **enforced, not documented**. The loader
rejects a config that smells like a selector, with a test asserting the rejection.
A rule that only lives in a README is a rule that erodes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from karumi_agent.errors import EditorialConfigError

# Patterns that indicate someone is trying to encode page mechanics into config.
_FORBIDDEN: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^\s*[#.][A-Za-z_][\w-]*\s*$"), "a CSS id or class selector"),
    (re.compile(r"\[[\w-]+\s*[~^|*$]?=\s*[\"'][^\"']*[\"']\]"), "a CSS attribute selector"),
    (
        re.compile(r"\b(?:div|span|button|input|a|ul|li|table|section)\s*[>+~]\s*\w"),
        "a CSS combinator",
    ),
    (re.compile(r"\bdata-testid\b|\baria-label\s*="), "a test-id or attribute hook"),
    (re.compile(r"\bnth-child\b|\bquerySelector\b|\bgetElementById\b"), "a DOM query"),
    (re.compile(r"\bxpath\s*[:=]|^\s*//\*?\["), "an XPath expression"),
    (re.compile(r"\b\d{2,4}\s*,\s*\d{2,4}\b"), "a coordinate pair"),
    (re.compile(r"^\s*/[a-z0-9][\w/-]*\s*$", re.IGNORECASE), "a hardcoded route path"),
    (
        re.compile(r"\bthen click\b|\bclick .* then\b|\bstep \d+\s*[:.]", re.IGNORECASE),
        "a click script",
    ),
)

_LIST_FIELDS = (
    "demo_goals",
    "themes",
    "approved_messages",
    "explicit_facts",
    "claims_to_avoid",
)


@dataclass(slots=True)
class EditorialConfig:
    """What the operator wants the demo to be about."""

    product_name: str | None = None
    #: What the demo should achieve. Guidance, never a script.
    demo_goals: list[str] = field(default_factory=list)
    #: Subjects worth covering, if the live page turns out to have them.
    themes: list[str] = field(default_factory=list)
    #: Messaging the operator has cleared for use.
    approved_messages: list[str] = field(default_factory=list)
    #: Facts supplied by the operator that may not be visible on the page.
    explicit_facts: list[str] = field(default_factory=list)
    #: Things the agent must not assert.
    claims_to_avoid: list[str] = field(default_factory=list)

    def as_prompt_block(self) -> str:
        """Render as guidance for the model.

        Framed as preferences, not instructions to click: the live page decides
        what is actually possible, and the model must not treat a theme that is
        absent from the page as licence to invent it.
        """
        sections: list[str] = []
        if self.product_name:
            sections.append(f"This demo is of: {self.product_name} (verify against the page).")
        if self.demo_goals:
            sections.append("Goals: " + "; ".join(self.demo_goals))
        if self.themes:
            sections.append("Worth covering if present on the page: " + "; ".join(self.themes))
        if self.approved_messages:
            sections.append("Approved things to say: " + "; ".join(self.approved_messages))
        if self.explicit_facts:
            sections.append("Facts you may state: " + "; ".join(self.explicit_facts))
        if self.claims_to_avoid:
            sections.append("Never claim: " + "; ".join(self.claims_to_avoid))
        if not sections:
            return ""
        return "<demo_guidance>\n" + "\n".join(sections) + "\n</demo_guidance>"


def _check_value(value: str, *, where: str) -> None:
    for pattern, description in _FORBIDDEN:
        if pattern.search(value):
            raise EditorialConfigError(
                f"Editorial config may not contain {description}",
                field=where,
                value=value[:120],
                hint=(
                    "Describe what the demo is about, not how to operate the page. "
                    "The agent locates elements by observing the live site."
                ),
            )


def parse_editorial_config(payload: dict[str, Any], *, source: str = "<memory>") -> EditorialConfig:
    """Validate and build an editorial config from a parsed mapping."""
    if not isinstance(payload, dict):
        raise EditorialConfigError("Editorial config must be a mapping", source=source)

    unknown = set(payload) - {"product_name", *_LIST_FIELDS}
    if unknown:
        raise EditorialConfigError(
            "Unknown editorial config keys",
            source=source,
            keys=sorted(unknown),
            hint="Allowed keys: product_name, " + ", ".join(_LIST_FIELDS),
        )

    product_name = payload.get("product_name")
    if product_name is not None:
        if not isinstance(product_name, str):
            raise EditorialConfigError("product_name must be a string", source=source)
        _check_value(product_name, where="product_name")

    values: dict[str, list[str]] = {}
    for key in _LIST_FIELDS:
        raw = payload.get(key, [])
        if raw is None:
            raw = []
        if not isinstance(raw, list):
            raise EditorialConfigError(f"{key} must be a list", source=source, field=key)
        entries: list[str] = []
        for item in raw:
            if not isinstance(item, str):
                raise EditorialConfigError(
                    f"{key} entries must be strings", source=source, field=key
                )
            _check_value(item, where=key)
            entries.append(item.strip())
        values[key] = entries

    return EditorialConfig(product_name=product_name, **values)


def load_editorial_config(path: Path | None) -> EditorialConfig | None:
    """Load and validate a YAML editorial config, if one is configured."""
    if path is None:
        return None
    if not path.exists():
        # A missing editorial file is not an error: an arbitrary URL has none.
        return None
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise EditorialConfigError(
            "Editorial config is not valid YAML", source=str(path), cause=str(exc)
        ) from exc
    return parse_editorial_config(payload, source=str(path))
