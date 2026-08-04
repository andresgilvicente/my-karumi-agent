"""Editorial knowledge: what the operator wants said about a product.

This is the second of the three knowledge channels, and the only trusted one.

* **Observed** — `<ui_state>`. Comes from a page we do not control. Untrusted.
* **Editorial** — this module. Written by a human, version-controlled, reviewed.
* **Verified results** — `ActionResult`. Produced only by observing consequences.

Keeping them apart is a requirement, not a stylistic preference. Editorial
content carries authority precisely because a web page cannot write it; if page
text could reach the same channel, a hostile site could grant itself facts to
assert. So editorial config is loaded from disk into the system prompt, page
text only ever appears inside `<ui_state>` as a labelled developer message, and
nothing merges the two.

What editorial config must *not* contain: selectors, element ids, routes, or a
fixed running order. Those would re-introduce the coupling to a specific build
of the site that this whole project exists to avoid. It describes intent —
what is worth showing and what must not be claimed — and the agent works out
how to honour it against whatever is actually on screen.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field


class ProductKnowledge(BaseModel):
    """Operator-authored guidance for demoing one product."""

    model_config = ConfigDict(extra="forbid")

    name: str
    url: str = ""
    summary: str = ""
    """One or two sentences the agent may use to introduce the product."""

    goals: list[str] = Field(default_factory=list)
    """What a good demo achieves. Shapes the route, never dictates it."""

    highlights: list[str] = Field(default_factory=list)
    """Areas worth reaching, described by intent rather than by location."""

    value_messages: list[str] = Field(default_factory=list)
    """Points worth making when the screen gives an honest opening."""

    authorised_facts: list[str] = Field(default_factory=list)
    """Claims the operator vouches for that the page may not state itself."""

    avoid_claims: list[str] = Field(default_factory=list)
    """Things never to say, regardless of what the page seems to imply."""

    def as_prompt(self) -> str:
        """Render as a system-prompt section.

        Explicitly framed as operator instruction so the model does not confuse
        it with the page. The precedence rule is stated here rather than left
        implied: when editorial guidance and the screen disagree, the screen
        wins, because the screen is what the user is looking at.
        """
        lines = ["## Product briefing (from the operator — trusted)", ""]
        lines.append(f"Product: {self.name}")
        if self.url:
            lines.append(f"Site: {self.url}")
        if self.summary:
            lines.append(f"Summary: {self.summary}")

        for title, items in (
            ("Goals for this demo", self.goals),
            ("Worth showing if you can find it", self.highlights),
            ("Points worth making when the screen supports them", self.value_messages),
            ("Facts the operator has authorised you to state", self.authorised_facts),
        ):
            if items:
                lines.append("")
                lines.append(f"{title}:")
                lines.extend(f"- {item}" for item in items)

        if self.avoid_claims:
            lines.append("")
            lines.append("Never claim the following, even if the page seems to suggest it:")
            lines.extend(f"- {item}" for item in self.avoid_claims)

        lines.append("")
        lines.append(
            "This briefing describes intent, not layout. It names no elements and "
            "no order. Work out how to honour it from what is actually on screen, "
            "and if the screen contradicts the briefing, the screen is right."
        )
        return "\n".join(lines)


def load_product(path: str | Path) -> ProductKnowledge:
    """Load a product briefing from YAML."""
    data: Any = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a YAML mapping at the top level")
    return ProductKnowledge.model_validate(data)


#: Used when no briefing is supplied — a generic URL demo with no editorial
#: guidance at all. The agent still works; it just has nothing to say beyond
#: what it can see, which is the correct behaviour rather than a degraded one.
UNBRIEFED = ProductKnowledge(
    name="this product",
    summary="",
)
