"""The closed action vocabulary and its typed results.

Two deliberate constraints live here:

* **The vocabulary is closed.** There is no "run this JavaScript" action and no
  free-form escape hatch. A page cannot talk the model into an action that does
  not exist, so the entire "LLM executes arbitrary code" risk class is removed
  structurally rather than by instruction.
* **Every action names the snapshot it was planned against.** Refs are only
  meaningful relative to an observation; carrying ``source_snapshot_id`` lets the
  executor refuse stale plans instead of resolving them against a page that has
  since changed underneath.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ActionKind(StrEnum):
    """Everything the agent is able to do to a page.

    Ordering matters: this is rendered into the model's JSON schema as an enum,
    and small models bias toward the first value. ``ANSWER`` leads so that
    "just talk about it" is the path of least resistance, which is the correct
    default for a demo agent.
    """

    ANSWER = "answer"
    HIGHLIGHT = "highlight"
    SCROLL = "scroll"
    NAVIGATE = "navigate"
    CLICK = "click"
    SELECT_TEXT = "select_text"
    FILL = "fill"
    BACK = "back"
    FORWARD = "forward"

    @property
    def needs_ref(self) -> bool:
        """Does this action target a specific element?"""
        return self in {
            ActionKind.HIGHLIGHT,
            ActionKind.SCROLL,
            ActionKind.CLICK,
            ActionKind.SELECT_TEXT,
            ActionKind.FILL,
        }

    @property
    def mutates_page(self) -> bool:
        """Could this change page state, as opposed to only the view?"""
        return self in {
            ActionKind.CLICK,
            ActionKind.FILL,
            ActionKind.NAVIGATE,
            ActionKind.BACK,
            ActionKind.FORWARD,
        }

    @property
    def is_read_only(self) -> bool:
        return not self.mutates_page


class ActionStatus(StrEnum):
    """How an attempted action ended."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REFUSED = "refused"
    NEEDS_CONFIRMATION = "needs_confirmation"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class ActionRequest:
    """A single intended action, bound to the observation that motivated it."""

    kind: ActionKind
    #: Ref from ``source_snapshot_id``. Required when ``kind.needs_ref``.
    ref: str | None = None
    #: Destination for NAVIGATE. Always validated against the origin allowlist.
    url: str | None = None
    #: Text for FILL. Never a password; the executor refuses password fields.
    text: str | None = None
    #: What the agent will say about this action. Kept with the request so the
    #: spoken narration and the action are planned together and can be cancelled
    #: together on barge-in.
    say: str | None = None
    #: Human-readable description of the target, used for confirmation prompts.
    #: Mirrors Playwright MCP's `element` field: "obtain permission to interact".
    target_label: str | None = None
    #: Forces the confirmation gate regardless of what the target is labelled.
    #: Set when the *operator's own words* express destructive intent. Observed
    #: failure this closes: asked to "click the delete workspace button", the
    #: model chose an unrelated element while narrating "opening Delete
    #: workspace". Label-based classification saw an innocuous target and let it
    #: through. Intent lives in the request, not only in the page.
    force_confirmation: bool = False
    #: Why confirmation was forced, used to phrase the question.
    confirmation_reason: str | None = None

    source_snapshot_id: str = ""
    action_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    requested_at: float = field(default_factory=time.time)

    def describe(self) -> str:
        """One-line description for logs and spoken confirmations."""
        target = self.target_label or self.ref or self.url or ""
        return f"{self.kind.value}({target})" if target else self.kind.value

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "action_id": self.action_id,
            "kind": self.kind.value,
            "source_snapshot_id": self.source_snapshot_id,
        }
        for key in ("ref", "url", "text", "target_label"):
            value = getattr(self, key)
            if value:
                # Never log typed text verbatim: it may be user-entered content.
                payload[key] = "<redacted>" if key == "text" else value
        return payload


@dataclass(frozen=True, slots=True)
class Evidence:
    """A fact observed from the page, with where it came from.

    The agent may only make claims it can point at. Evidence is what makes
    "verified" mean something and what stops narration drifting into invention.
    """

    kind: str
    detail: str
    ref: str | None = None
    snapshot_id: str | None = None
    url: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload = {"kind": self.kind, "detail": self.detail}
        for key in ("ref", "snapshot_id", "url"):
            value = getattr(self, key)
            if value:
                payload[key] = value
        return payload


@dataclass(frozen=True, slots=True)
class ObservedChange:
    """A difference between before and after, used to verify an action."""

    field: str
    before: str | None
    after: str | None

    def as_dict(self) -> dict[str, Any]:
        return {"field": self.field, "before": self.before, "after": self.after}


@dataclass(slots=True)
class ActionResult:
    """The outcome of an action.

    ``verified`` is the load-bearing field. It is only true when we re-observed
    the page and confirmed the intended effect actually happened. The agent must
    never narrate an action as successful on the strength of "no exception was
    raised" -- that is exactly the fiction this type exists to prevent.
    """

    action_id: str
    kind: ActionKind
    status: ActionStatus
    source_snapshot_id: str
    #: Snapshot taken after the action, if one was captured.
    result_snapshot_id: str | None = None
    verified: bool = False
    #: Why verification did or did not succeed, in plain language.
    verification_detail: str = ""
    evidence: list[Evidence] = field(default_factory=list)
    changes: list[ObservedChange] = field(default_factory=list)
    duration_ms: float = 0.0
    error: dict[str, Any] | None = None
    #: Set when status is NEEDS_CONFIRMATION, explaining what to confirm.
    confirmation_prompt: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status is ActionStatus.SUCCEEDED

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "action_id": self.action_id,
            "kind": self.kind.value,
            "status": self.status.value,
            "verified": self.verified,
            "source_snapshot_id": self.source_snapshot_id,
            "duration_ms": round(self.duration_ms, 1),
        }
        if self.result_snapshot_id:
            payload["result_snapshot_id"] = self.result_snapshot_id
        if self.verification_detail:
            payload["verification_detail"] = self.verification_detail
        if self.evidence:
            payload["evidence"] = [e.as_dict() for e in self.evidence]
        if self.changes:
            payload["changes"] = [c.as_dict() for c in self.changes]
        if self.error:
            payload["error"] = self.error
        if self.confirmation_prompt:
            payload["confirmation_prompt"] = self.confirmation_prompt
        return payload

    def narration_hint(self) -> str:
        """How the agent should describe this outcome out loud.

        Deliberately blunt about failure: an unverified action is reported as
        unverified, not glossed over.
        """
        if self.status is ActionStatus.NEEDS_CONFIRMATION:
            return self.confirmation_prompt or "That action needs your confirmation first."
        if self.status is ActionStatus.STALE:
            return "The page changed while I was working; let me look again."
        if self.status is ActionStatus.REFUSED:
            reason = (self.error or {}).get("message", "it is not allowed")
            return f"I did not do that because {reason}."
        if self.status is ActionStatus.FAILED:
            return "That did not work, so I have not changed anything."
        if not self.verified:
            return "I tried that, but I could not confirm it took effect."
        return ""
