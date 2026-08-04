"""The JSON schema the model is constrained to produce.

This is the single most important reliability mechanism in the project, and it
came directly from measurement. With ordinary tool calling, a 1.5B model asked to
act on a page produced refs like ``"Analytics"`` -- the accessible name, not the
handle -- roughly half the time. Building the schema so that ``ref`` is an
**enum of the refs in the current snapshot** made an invalid ref inexpressible:
6/6 valid in the same test. The model is not being asked to be careful; the
grammar simply has no path to a wrong answer.

Two further ordering decisions, both deliberate:

* ``say`` comes **first**. Constrained decoding emits properties in schema order,
  so the spoken sentence is generated before the action fields. That lets text-to
  -speech start on the first tokens instead of waiting for the whole object --
  worth roughly two seconds on this hardware.
* ``action`` lists ``answer`` first. Small models bias toward the first enum
  value, and "just talk about it" is the right default for a demo agent. In
  testing, an unordered enum collapsed to ``click`` for every input.

Nothing here is product-specific: the enum is rebuilt from whatever page is
currently loaded.
"""

from __future__ import annotations

import re
from typing import Any

from karumi_agent.domain.actions import ActionKind
from karumi_agent.domain.snapshot import PageSnapshot

#: Placeholder used when an action targets no element.
NO_REF = "none"

# Imperative verbs, from the *operator's* speech, that narrow which actions are
# plausible. This is a prior over the action enum, never a parser of page text:
# the trusted channel is what the human said, and page content must never be able
# to widen the agent's capabilities.
_SHOW_VERBS = re.compile(
    r"\b(show|highlight|point|indicate|where|find|look at|see|display)\b", re.IGNORECASE
)
_MOVE_VERBS = re.compile(
    r"\b(go|open|navigate|take me|switch|visit|back|forward|return|scroll|down|up)\b",
    re.IGNORECASE,
)
_ACT_VERBS = re.compile(
    r"\b(click|press|tap|select|choose|toggle|submit|type|enter|fill|search for)\b",
    re.IGNORECASE,
)
_BACK_VERBS = re.compile(r"\b(back|previous|return)\b", re.IGNORECASE)
_FORWARD_VERBS = re.compile(r"\b(forward|next page)\b", re.IGNORECASE)


def allowed_actions(utterance: str, *, allow_mutating: bool = True) -> list[ActionKind]:
    """Narrow the action enum using the operator's own words.

    A question with no imperative verb cannot yield a click. That single rule
    removes most of the action-collapse failure mode observed on small models,
    and it does so without trusting the model's judgement or the page's content.
    """
    text = utterance or ""
    actions: list[ActionKind] = [ActionKind.ANSWER]

    wants_show = bool(_SHOW_VERBS.search(text))
    wants_move = bool(_MOVE_VERBS.search(text))
    wants_act = bool(_ACT_VERBS.search(text))

    if wants_show or wants_move or wants_act:
        actions += [ActionKind.HIGHLIGHT, ActionKind.SCROLL]
    if wants_move or wants_act:
        actions.append(ActionKind.NAVIGATE)
    if _BACK_VERBS.search(text):
        actions.append(ActionKind.BACK)
    if _FORWARD_VERBS.search(text):
        actions.append(ActionKind.FORWARD)
    if wants_act and allow_mutating:
        actions += [ActionKind.CLICK, ActionKind.SELECT_TEXT]

    # Preserve declaration order (ANSWER first) and drop duplicates.
    seen: set[ActionKind] = set()
    ordered: list[ActionKind] = []
    for kind in ActionKind:
        if kind in actions and kind not in seen:
            seen.add(kind)
            ordered.append(kind)
    return ordered


def build_action_schema(
    snapshot: PageSnapshot | None,
    *,
    utterance: str = "",
    allow_mutating: bool = True,
    max_refs: int = 220,
) -> dict[str, Any]:
    """Build the response schema for the current page and request.

    Args:
        snapshot: The observation the model is reasoning over. Its refs become the
            ``ref`` enum, so the model cannot name an element that is not there.
        utterance: What the operator said, used only to narrow the action enum.
        allow_mutating: False during autonomous narration, where the agent should
            explain rather than click.
        max_refs: Cap on enum size. A huge enum costs prompt tokens and confuses
            small models; the snapshot is already budgeted to a similar size.
    """
    refs = snapshot.refs()[:max_refs] if snapshot else []
    actions = allowed_actions(utterance, allow_mutating=allow_mutating)

    return {
        "type": "object",
        "properties": {
            # First on purpose: streams to TTS while the rest is still decoding.
            "say": {
                "type": "string",
                "description": "One or two short spoken sentences. Plain language, no markup.",
            },
            "action": {
                "type": "string",
                "enum": [a.value for a in actions],
                "description": "What to do. Use 'answer' when speaking is enough.",
            },
            "ref": {
                "type": "string",
                "enum": [*refs, NO_REF],
                "description": (
                    "The element to act on, taken from [ref=...] in the page state. "
                    f"Use '{NO_REF}' when the action targets no element."
                ),
            },
        },
        "required": ["say", "action", "ref"],
        "additionalProperties": False,
    }


def build_demo_beat_schema(snapshot: PageSnapshot | None, *, max_refs: int = 220) -> dict[str, Any]:
    """Schema for the autonomous tour: pick something worth showing, and say why.

    Mutating actions are excluded outright. An unattended agent clicking things on
    a live product during a demo is not a risk worth taking, so the tour is
    limited to navigating, scrolling and highlighting.
    """
    refs = snapshot.refs()[:max_refs] if snapshot else []
    return {
        "type": "object",
        "properties": {
            "say": {
                "type": "string",
                "description": "Two or three short sentences explaining this part of the page.",
            },
            "action": {
                "type": "string",
                "enum": [
                    ActionKind.HIGHLIGHT.value,
                    ActionKind.SCROLL.value,
                    ActionKind.NAVIGATE.value,
                    ActionKind.ANSWER.value,
                ],
            },
            "ref": {"type": "string", "enum": [*refs, NO_REF]},
        },
        "required": ["say", "action", "ref"],
        "additionalProperties": False,
    }
