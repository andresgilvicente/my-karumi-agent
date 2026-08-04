"""Compact semantic representation of a page.

Built from Playwright's ``page.aria_snapshot(mode="ai", boxes=True)``, which emits
an indented, distilled accessibility tree with stable ``[ref=eN]`` handles. That
format is the source of truth; this module parses it into a typed tree, applies
node and token budgets, and renders the text the model actually sees.

Three properties matter and are enforced here rather than hoped for:

* **The model never sees the DOM.** It sees roles, accessible names, states and
  refs -- a fraction of the tokens, and nothing executable.
* **Refs belong to a snapshot.** Every snapshot carries an id; acting on a ref
  requires naming the snapshot it came from, so a stale ref is refused rather
  than silently resolved against whatever now occupies that position.
* **Budgets are explicit.** A hostile or merely large page cannot flood the
  context. When we drop nodes we say so.

Parsing is deliberately defensive: any line we cannot interpret becomes a text
node holding the raw content. A page we do not control must never be able to
crash the agent with malformed markup.
"""

from __future__ import annotations

import contextlib
import itertools
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

# Roughly 4 characters per token for English prose plus punctuation. Only used to
# keep the snapshot inside a budget, so an approximation is fine.
_CHARS_PER_TOKEN = 4

# Landmarks and structural roles worth keeping even when they carry no ref: they
# give the model the page's shape.
_LANDMARK_ROLES = frozenset(
    {
        "banner",
        "complementary",
        "contentinfo",
        "form",
        "main",
        "navigation",
        "region",
        "search",
        "article",
    }
)

_STRUCTURE_ROLES = frozenset(
    {"heading", "table", "row", "columnheader", "rowheader", "list", "listitem", "tablist", "tab"}
)

# `- role "name" [attr] [attr=value]: trailing value`
_LINE_RE = re.compile(
    r"""
    ^-\s*
    (?P<role>/?[A-Za-z][\w./-]*)      # role, or a property key like /url
    (?:\s+(?P<name>"(?:[^"\\]|\\.)*"|/(?:[^/\\]|\\.)*/))?   # quoted name or /regex/
    (?P<attrs>(?:\s*\[[^\]]*\])*)     # zero or more [bracketed] attributes
    (?:\s*:\s*(?P<value>.*))?$        # optional trailing value
    """,
    re.VERBOSE,
)

_ATTR_RE = re.compile(r"\[([^\]]*)\]")


@dataclass(frozen=True, slots=True)
class Box:
    """Viewport-relative geometry in CSS pixels, from ``[box=x,y,w,h]``."""

    x: float
    y: float
    width: float
    height: float

    @property
    def area(self) -> float:
        return max(0.0, self.width) * max(0.0, self.height)

    def intersects_viewport(self, width: int, height: int) -> bool:
        """Is any part of this box inside the viewport?"""
        return (
            self.x < width
            and self.y < height
            and self.x + self.width > 0
            and self.y + self.height > 0
        )

    def fully_inside_viewport(self, width: int, height: int) -> bool:
        return (
            self.x >= 0
            and self.y >= 0
            and self.x + self.width <= width
            and self.y + self.height <= height
        )

    def as_dict(self) -> dict[str, float]:
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}


@dataclass(slots=True)
class SnapshotNode:
    """One node of the accessibility tree."""

    role: str
    ref: str | None = None
    name: str | None = None
    value: str | None = None
    url: str | None = None
    #: Playwright emits `- /placeholder: ...` for inputs. Worth keeping separate
    #: from `value`: it says what a field expects rather than what it contains.
    placeholder: str | None = None
    level: int | None = None
    states: tuple[str, ...] = ()
    box: Box | None = None
    cursor_pointer: bool = False
    children: list[SnapshotNode] = field(default_factory=list)

    # ----------------------------------------------------------------- helpers
    @property
    def is_interactive(self) -> bool:
        """Does this node accept interaction?

        In ``mode="ai"`` Playwright only assigns refs to elements that are visible
        and receive pointer events, so a ref is itself the signal.
        """
        return self.ref is not None

    @property
    def label(self) -> str:
        """Best human-readable identifier, for confirmations and logs."""
        return self.name or self.value or self.role

    def walk(self) -> itertools.chain[SnapshotNode]:
        """Depth-first iteration over this node and its descendants."""
        return itertools.chain([self], *(child.walk() for child in self.children))

    def render_line(self, *, include_box: bool = False) -> str:
        """Render just this node, without children."""
        parts = [self.role]
        if self.name:
            parts.append(f'"{self.name}"')
        if self.level is not None:
            parts.append(f"[level={self.level}]")
        for state in self.states:
            parts.append(f"[{state}]")
        if self.url:
            parts.append(f"[url={self.url}]")
        if self.placeholder:
            parts.append(f'[placeholder="{self.placeholder}"]')
        if self.cursor_pointer:
            parts.append("[clickable]")
        if include_box and self.box is not None:
            box = self.box
            parts.append(f"[box={box.x:.0f},{box.y:.0f},{box.width:.0f},{box.height:.0f}]")
        if self.ref:
            parts.append(f"[ref={self.ref}]")
        line = " ".join(parts)
        if self.value:
            line = f"{line}: {self.value}"
        return line

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"role": self.role}
        if self.ref:
            payload["ref"] = self.ref
        if self.name:
            payload["name"] = self.name
        if self.value:
            payload["value"] = self.value
        if self.url:
            payload["url"] = self.url
        if self.placeholder:
            payload["placeholder"] = self.placeholder
        if self.level is not None:
            payload["level"] = self.level
        if self.states:
            payload["states"] = list(self.states)
        if self.box:
            payload["box"] = self.box.as_dict()
        if self.children:
            payload["children"] = [c.as_dict() for c in self.children]
        return payload


@dataclass(frozen=True, slots=True)
class Viewport:
    """The visible region, used to prioritise what the model sees first."""

    width: int
    height: int


@dataclass(slots=True)
class PageSnapshot:
    """An observation of the page at a moment in time.

    ``snapshot_id`` is the unit of freshness. Actions must cite the snapshot whose
    refs they used; see ``domain.actions`` and the executor's staleness check.
    """

    snapshot_id: str
    captured_at: float
    url: str
    title: str
    root: SnapshotNode
    viewport: Viewport
    node_count: int = 0
    rendered_node_count: int = 0
    truncated: bool = False
    #: Set when a login wall was detected (a visible password field).
    has_password_field: bool = False

    _by_ref: dict[str, SnapshotNode] = field(default_factory=dict, repr=False)

    # ------------------------------------------------------------------ lookup
    def resolve(self, ref: str) -> SnapshotNode | None:
        """Find the node a ref points at, or None."""
        return self._by_ref.get(ref)

    def refs(self) -> list[str]:
        """Every actionable ref, in document order.

        This is what becomes the ``enum`` of the model's JSON schema, so an
        invalid ref stops being expressible rather than merely discouraged.
        """
        return list(self._by_ref.keys())

    def interactive_nodes(self) -> list[SnapshotNode]:
        return [node for node in self.root.walk() if node.is_interactive]

    def age_s(self, *, now: float | None = None) -> float:
        return (now if now is not None else time.time()) - self.captured_at

    def is_expired(self, max_age_s: float, *, now: float | None = None) -> bool:
        return self.age_s(now=now) > max_age_s

    # ----------------------------------------------------------------- render
    def render(
        self,
        *,
        max_nodes: int = 320,
        max_tokens: int = 2200,
        include_boxes: bool = False,
        prioritise_viewport: bool = True,
    ) -> str:
        """Render the tree as indented text within a budget.

        Nodes are scored, the best ``max_nodes`` are kept, and every ancestor of a
        kept node is retained so the structure stays legible. If the result still
        exceeds ``max_tokens`` the budget is tightened and we render again.
        """
        budget = max_nodes
        text = ""
        kept: set[int] = set()
        # Halving converges in a handful of rounds; the floor stops it degenerating
        # into an empty snapshot on a pathologically large page.
        while True:
            kept = self._select_nodes(budget, prioritise_viewport=prioritise_viewport)
            lines: list[str] = []
            self._render_into(self.root, kept, 0, lines, include_boxes=include_boxes)
            text = "\n".join(lines)
            if len(text) // _CHARS_PER_TOKEN <= max_tokens or budget <= 20:
                break
            budget = max(20, budget // 2)

        self.rendered_node_count = len(kept)
        self.truncated = len(kept) < self.node_count
        return text

    def render_ui_state(
        self,
        *,
        max_nodes: int = 320,
        max_tokens: int = 2200,
        include_boxes: bool = False,
        prioritise_viewport: bool = True,
    ) -> str:
        """Render the full block handed to the model.

        Page content is untrusted, so it is fenced in a tag the system prompt
        teaches the model to treat as data.

        The snapshot id is deliberately **not** in this header. It used to be,
        for attribution, and it was a measurable mistake: the id changes on every
        observation, so it changed the prompt prefix on every turn and forced a
        full cold prefill each time -- 7.7 s against 1 s, in the benchmark that
        caught it. The model never needed it either, because the planner attaches
        ``source_snapshot_id`` from the store rather than from anything the model
        says. Two identical observations of an unchanged page now render
        byte-identically and hit the KV cache.
        """
        body = self.render(
            max_nodes=max_nodes,
            max_tokens=max_tokens,
            include_boxes=include_boxes,
            prioritise_viewport=prioritise_viewport,
        )
        header = f'<ui_state url="{_escape_attr(self.url)}" title="{_escape_attr(self.title)}">'
        note = ""
        if self.truncated:
            note = (
                f"\n<note>Showing {self.rendered_node_count} of {self.node_count} elements; "
                f"the page is larger than the budget.</note>"
            )
        return f"{header}\n{body}{note}\n</ui_state>"

    def _select_nodes(self, max_nodes: int, *, prioritise_viewport: bool) -> set[int]:
        """Choose which nodes to render, by id()."""
        scored: list[tuple[float, int, SnapshotNode]] = []
        parents = self._parent_map()
        for order, node in enumerate(self.root.walk()):
            scored.append((self._score(node, prioritise_viewport=prioritise_viewport), order, node))

        # Highest score first; document order breaks ties so output stays stable.
        scored.sort(key=lambda item: (-item[0], item[1]))
        kept: set[int] = set()
        for _, _, node in scored[:max_nodes]:
            kept.add(id(node))
            # Keep ancestors so the tree remains connected and readable.
            current = parents.get(id(node))
            while current is not None and id(current) not in kept:
                kept.add(id(current))
                current = parents.get(id(current))
        return kept

    def _parent_map(self) -> dict[int, SnapshotNode]:
        parents: dict[int, SnapshotNode] = {}
        stack = [self.root]
        while stack:
            node = stack.pop()
            for child in node.children:
                parents[id(child)] = node
                stack.append(child)
        return parents

    def _score(self, node: SnapshotNode, *, prioritise_viewport: bool) -> float:
        """Rank a node's usefulness to the model."""
        score = 0.0
        if node.is_interactive:
            score += 10.0
        if node.role in _STRUCTURE_ROLES:
            score += 6.0
        if node.role in _LANDMARK_ROLES:
            score += 5.0
        if node.name:
            score += 3.0
        if node.value:
            score += 2.0
        if node.cursor_pointer:
            score += 1.0
        if node.role == "heading":
            # Shallower headings describe the page better than deep ones.
            score += max(0.0, 4.0 - float(node.level or 3))
        if prioritise_viewport and node.box is not None:
            if node.box.intersects_viewport(self.viewport.width, self.viewport.height):
                score += 8.0
            else:
                score -= 2.0
        if node.role in {"generic", "none", "presentation"} and not node.name:
            score -= 4.0
        return score

    def _render_into(
        self,
        node: SnapshotNode,
        kept: set[int],
        depth: int,
        lines: list[str],
        *,
        include_boxes: bool,
    ) -> None:
        visible = id(node) in kept
        if visible:
            lines.append("  " * depth + "- " + node.render_line(include_box=include_boxes))
        child_depth = depth + 1 if visible else depth
        for child in node.children:
            self._render_into(child, kept, child_depth, lines, include_boxes=include_boxes)

    # ------------------------------------------------------------------- find
    def find(self, query: str, *, context: int = 2, limit: int = 12) -> str:
        """Search the tree for text, returning matches with their ancestor path.

        Much cheaper than rendering the whole page when the model only needs to
        locate one thing. Ported in spirit from Playwright MCP's ``browser_find``.
        """
        needle = query.strip().lower()
        if not needle:
            return ""

        parents = self._parent_map()
        matches: list[str] = []
        for node in self.root.walk():
            haystack = " ".join(filter(None, [node.role, node.name, node.value, node.url])).lower()
            if needle not in haystack:
                continue

            path: list[str] = []
            current = parents.get(id(node))
            while current is not None and len(path) < context:
                if current.name or current.role in _LANDMARK_ROLES:
                    path.append(current.render_line())
                current = parents.get(id(current))
            path.reverse()

            block = [f"  {'  ' * i}{line}" for i, line in enumerate(path)]
            block.append(f"  {'  ' * len(path)}{node.render_line()}")
            for child in node.children[:context]:
                block.append(f"  {'  ' * (len(path) + 1)}{child.render_line()}")
            matches.append("\n".join(block))
            if len(matches) >= limit:
                break

        if not matches:
            return f'<find query="{_escape_attr(query)}">no matches</find>'
        return f'<find query="{_escape_attr(query)}">\n' + "\n---\n".join(matches) + "\n</find>"

    # ------------------------------------------------------------------- diff
    def diff(self, previous: PageSnapshot | None, **render_kwargs: Any) -> str:
        """Lines present now that were not present before.

        Used to narrate a change without re-sending the whole page, which is the
        difference between a usable and an unusable context on a small model.
        """
        current_lines = self.render(**render_kwargs).splitlines()
        if previous is None:
            return "\n".join(current_lines)
        # Compare on stripped text: indentation shifts when ancestors are pruned,
        # and we care about content appearing, not about it moving.
        before = {line.strip() for line in previous.render(**render_kwargs).splitlines()}
        return "\n".join(line for line in current_lines if line.strip() not in before)

    def as_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "captured_at": self.captured_at,
            "url": self.url,
            "title": self.title,
            "node_count": self.node_count,
            "truncated": self.truncated,
            "viewport": {"width": self.viewport.width, "height": self.viewport.height},
        }


def _escape_attr(value: str) -> str:
    return value.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")


def _unquote(raw: str) -> str:
    """Strip the quoting Playwright applies to accessible names."""
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        return raw[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    if len(raw) >= 2 and raw[0] == raw[-1] == "/":
        # A regex-style name means Playwright could not quote it verbatim.
        return raw[1:-1]
    return raw


def _parse_attrs(
    raw: str,
) -> tuple[str | None, int | None, tuple[str, ...], Box | None, bool, str | None]:
    """Parse the ``[...]`` attribute run.

    Returns (ref, level, states, box, cursor_pointer, url).
    """
    ref: str | None = None
    level: int | None = None
    box: Box | None = None
    cursor_pointer = False
    url: str | None = None
    states: list[str] = []

    for match in _ATTR_RE.finditer(raw):
        token = match.group(1).strip()
        if not token:
            continue
        key, _, value = token.partition("=")
        key = key.strip()
        value = value.strip()

        if key == "ref":
            ref = value or None
        elif key == "level":
            # A non-numeric level is malformed page output, not a reason to fail.
            with contextlib.suppress(ValueError):
                level = int(value)
        elif key == "cursor":
            cursor_pointer = value == "pointer"
        elif key == "url":
            url = value or None
        elif key == "box":
            parts = value.split(",")
            if len(parts) == 4:
                try:
                    box = Box(*(float(p) for p in parts))
                except ValueError:
                    box = None
        else:
            states.append(token if value else key)

    return ref, level, tuple(states), box, cursor_pointer, url


def parse_aria_snapshot(
    text: str,
    *,
    url: str,
    title: str,
    viewport: Viewport,
    snapshot_id: str | None = None,
    captured_at: float | None = None,
) -> PageSnapshot:
    """Parse Playwright's ``aria_snapshot(mode="ai")`` output into a typed tree.

    Unparseable lines are preserved as text nodes rather than dropped or raised
    on: the input comes from a page we do not control.
    """
    root = SnapshotNode(role="page", name=title or None)
    # (indent, node) stack. Root sits at indent -1 so any real line nests under it.
    stack: list[tuple[int, SnapshotNode]] = [(-1, root)]
    by_ref: dict[str, SnapshotNode] = {}
    count = 0
    has_password = False

    for raw_line in text.splitlines():
        if not raw_line.strip():
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        stripped = raw_line.strip()

        if not stripped.startswith("-"):
            # A continuation of the previous node's value (multi-line text).
            if stack:
                parent = stack[-1][1]
                parent.value = f"{parent.value} {stripped}".strip() if parent.value else stripped
            continue

        while len(stack) > 1 and stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1]

        match = _LINE_RE.match(stripped)
        if match is None:
            node = SnapshotNode(role="text", value=stripped.lstrip("- ").strip())
            parent.children.append(node)
            count += 1
            continue

        role = match.group("role")
        # `- /url: https://...` and `- /placeholder: ...` are properties of the
        # parent node, not child nodes of their own.
        if role.startswith("/"):
            key = role.lstrip("/")
            value = (match.group("value") or "").strip()
            if key == "url":
                parent.url = value
            elif key == "placeholder":
                parent.placeholder = value or None
            elif value:
                parent.value = f"{parent.value} {value}".strip() if parent.value else value
            continue

        name_raw = match.group("name")
        ref, level, states, box, cursor_pointer, attr_url = _parse_attrs(match.group("attrs") or "")
        value = (match.group("value") or "").strip() or None

        node = SnapshotNode(
            role=role,
            ref=ref,
            name=_unquote(name_raw) if name_raw else None,
            value=value,
            url=attr_url,
            level=level,
            states=states,
            box=box,
            cursor_pointer=cursor_pointer,
        )
        parent.children.append(node)
        stack.append((indent, node))
        count += 1

        if ref:
            by_ref[ref] = node
        if _looks_like_password_field(node):
            has_password = True

    return PageSnapshot(
        snapshot_id=snapshot_id or uuid.uuid4().hex[:12],
        captured_at=captured_at if captured_at is not None else time.time(),
        url=url,
        title=title,
        root=root,
        viewport=viewport,
        node_count=count,
        _by_ref=by_ref,
        has_password_field=has_password,
    )


_PASSWORD_HINT_RE = re.compile(
    r"\b(password|passphrase|contrase|mot de passe|passwort)\b", re.IGNORECASE
)


def _looks_like_password_field(node: SnapshotNode) -> bool:
    """Heuristic password-field detection from the accessibility tree alone.

    A real ``input[type=password]`` is confirmed separately from the DOM; this
    catches the common case where the accessible name or placeholder says so.
    Used to notice a login wall, never to decide it is safe to type.
    """
    if node.role not in {"textbox", "searchbox", "combobox"}:
        return False
    haystack = f"{node.name or ''} {node.placeholder or ''}"
    return bool(_PASSWORD_HINT_RE.search(haystack))
