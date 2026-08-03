"""Domain models — pure data, no infrastructure.

These types are the contract between the browser layer, the workers and the
tools. Nothing here imports Playwright or Pipecat: the same models describe a
page whether it was observed through a real browser or replayed from a fixture,
which is what makes the observation logic unit-testable without a browser.

The accessibility types deliberately mirror Pipecat's
`pipecat.processors.frameworks.rtvi.models` (`A11yNode`, `A11ySelection`,
`A11ySnapshot`) field for field, so a snapshot can be handed to a `UIWorker`
with no translation step. Divergence here would mean re-deriving `<ui_state>`
ourselves, which is exactly what we are trying not to do.
"""

from __future__ import annotations

from collections.abc import Iterator
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

# --------------------------------------------------------------------------
# Observation
# --------------------------------------------------------------------------


class A11yNode(BaseModel):
    """One node of the accessibility tree, as emitted by Pipecat's walker."""

    model_config = ConfigDict(extra="ignore")

    ref: str
    role: str
    name: str | None = None
    value: str | None = None
    state: list[str] = Field(default_factory=list)
    level: int | None = None
    colcount: int | None = None
    rowcount: int | None = None
    children: list[A11yNode] = Field(default_factory=list)

    def walk(self) -> Iterator[A11yNode]:
        """Depth-first traversal, self first."""
        yield self
        for child in self.children:
            yield from child.walk()

    @property
    def offscreen(self) -> bool:
        return "offscreen" in self.state


class Selection(BaseModel):
    """The user's current text selection, when there is one."""

    model_config = ConfigDict(extra="ignore")

    ref: str
    text: str
    start_offset: int | None = None
    end_offset: int | None = None


class PageInfo(BaseModel):
    """Coarse page state, used for verification and for navigation policy."""

    model_config = ConfigDict(extra="ignore")

    url: str
    title: str
    scroll_y: int = 0
    scroll_height: int = 0
    viewport_height: int = 0
    ready_state: str = "complete"


class Snapshot(BaseModel):
    """A point-in-time observation of the page.

    `snapshot_id` is assigned by the host (not the page) and is the freshness
    token every action must carry. An action built against a stale snapshot is
    rejected rather than applied to a page the model has not actually seen —
    see `ErrorCode.STALE_SNAPSHOT`.
    """

    model_config = ConfigDict(extra="ignore")

    snapshot_id: str
    captured_at: int
    page: PageInfo
    root: A11yNode
    selection: Selection | None = None
    frame_count: int = 1

    def index(self) -> dict[str, A11yNode]:
        """Map every ref in the tree to its node."""
        return {node.ref: node for node in self.root.walk() if node.ref}

    def node_count(self) -> int:
        return sum(1 for _ in self.root.walk())


# --------------------------------------------------------------------------
# Action
# --------------------------------------------------------------------------


class Capability(StrEnum):
    """The allowlist is of *capabilities*, never of specific elements.

    Anything not represented here has no schema the model can call, so a page
    cannot talk the agent into an action the operator never granted.
    """

    READ = "read"
    SCROLL = "scroll"
    HIGHLIGHT = "highlight"
    NAVIGATE = "navigate"
    CLICK = "click"
    FILL = "fill"


class ActionKind(StrEnum):
    SCROLL_TO = "scroll_to"
    HIGHLIGHT = "highlight"
    CLICK = "click"
    FILL = "fill"
    SELECT_TEXT = "select_text"
    NAVIGATE = "navigate"
    GO_BACK = "go_back"


ACTION_CAPABILITY: dict[ActionKind, Capability] = {
    ActionKind.SCROLL_TO: Capability.SCROLL,
    ActionKind.HIGHLIGHT: Capability.HIGHLIGHT,
    ActionKind.CLICK: Capability.CLICK,
    ActionKind.FILL: Capability.FILL,
    ActionKind.SELECT_TEXT: Capability.HIGHLIGHT,
    ActionKind.NAVIGATE: Capability.NAVIGATE,
    ActionKind.GO_BACK: Capability.NAVIGATE,
}


class Action(BaseModel):
    """A single, typed browser action.

    There is no free-form escape hatch by design: the model can never supply
    JavaScript, a CSS selector or coordinates, only a ref from a snapshot it was
    shown plus one of the operations above.
    """

    model_config = ConfigDict(extra="forbid")

    action_id: str
    kind: ActionKind
    snapshot_id: str
    ref: str | None = None
    url: str | None = None
    value: str | None = None
    replace: bool = True
    start_offset: int | None = None
    end_offset: int | None = None
    duration_ms: int = 2200
    reason: str = ""


class ErrorCode(StrEnum):
    STALE_SNAPSHOT = "STALE_SNAPSHOT"
    REF_NOT_FOUND = "REF_NOT_FOUND"
    BLOCKED_BY_POLICY = "BLOCKED_BY_POLICY"
    NEEDS_CONFIRMATION = "NEEDS_CONFIRMATION"
    ELEMENT_DISABLED = "ELEMENT_DISABLED"
    ELEMENT_NOT_ACTIONABLE = "ELEMENT_NOT_ACTIONABLE"
    NAVIGATION_BLOCKED = "NAVIGATION_BLOCKED"
    TIMEOUT = "TIMEOUT"
    NO_CHANGE_DETECTED = "NO_CHANGE_DETECTED"
    BROWSER_ERROR = "BROWSER_ERROR"
    INVALID_ACTION = "INVALID_ACTION"


class ElementState(BaseModel):
    """Small, verification-oriented view of an element.

    Intentionally not context for the model — it exists to answer "did this
    actually change?" rather than "what is on the page".
    """

    model_config = ConfigDict(extra="ignore")

    ref: str
    tag: str = ""
    role: str | None = None
    type: str | None = None
    name: str = ""
    value: str | None = None
    checked: bool | None = None
    disabled: bool = False
    expanded: str | None = None
    selected: str | None = None
    focused: bool = False
    visible: bool = False
    in_viewport: bool = False
    rect: dict[str, int] = Field(default_factory=dict)


class Evidence(BaseModel):
    """What the page did, observed rather than assumed."""

    page_before: PageInfo
    page_after: PageInfo
    element_before: ElementState | None = None
    element_after: ElementState | None = None
    changes: list[str] = Field(default_factory=list)
    refs_added: int = 0
    refs_removed: int = 0
    snapshot_id_after: str = ""

    @property
    def changed(self) -> bool:
        return bool(self.changes)


class Timings(BaseModel):
    """Milliseconds per stage. Reported for every action, always."""

    observe_ms: float = 0.0
    execute_ms: float = 0.0
    verify_ms: float = 0.0

    @property
    def total_ms(self) -> float:
        return self.observe_ms + self.execute_ms + self.verify_ms


class ActionResult(BaseModel):
    """The outcome of an action.

    `ok` means the command was dispatched without error. `verified` means the
    page was observed afterwards and actually changed in a way consistent with
    the request. Only `verified` licenses the agent to tell the user something
    happened — that distinction is the whole point of this type.
    """

    action_id: str
    kind: ActionKind
    ok: bool
    verified: bool = False
    error_code: ErrorCode | None = None
    message: str = ""
    evidence: Evidence | None = None
    timings: Timings = Field(default_factory=Timings)

    def summary(self) -> str:
        """One-line, model-facing description grounded in what was observed."""
        if not self.ok:
            return f"{self.kind} failed ({self.error_code}): {self.message}"
        if not self.verified:
            return (
                f"{self.kind} was dispatched but could not be confirmed against the page "
                f"({self.message}). Do not tell the user it worked."
            )
        return f"{self.kind} confirmed: {self.message}"
