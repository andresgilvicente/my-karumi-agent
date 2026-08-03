"""Rendering and budgeting of the accessibility tree.

Two responsibilities, both about what the model is allowed to see:

* `render_ui_state` reproduces Pipecat's `UIWorker.render_ui_state` byte for
  byte. In production Pipecat does the rendering; we keep our own copy so the
  CLI and the tests can show and assert on exactly what the model will get,
  and so a change in the upstream format is caught by a test instead of by a
  confused model.

* `prune` enforces a hard node budget *before* the snapshot is published. The
  walker already collapses the DOM to nodes that carry accessible roles, which
  is most of the reduction, but a large dashboard can still blow past a
  sensible context budget. Pruning is deterministic and drops the least
  informative material first: offscreen subtrees the user cannot even see.
"""

from __future__ import annotations

from karumi_agent.domain.models import A11yNode, Snapshot

#: Names longer than this are truncated. The walker already caps at 100; this
#: is a second bound for the rare node that carries a long composed name.
MAX_NAME_CHARS = 100

#: Marker state tag applied to a subtree we collapsed. Single lowercase word, so
#: it renders as `[collapsed]` and matches the convention the prompt guide sets.
COLLAPSED = "collapsed"


def render_node(node: A11yNode, depth: int, lines: list[str]) -> None:
    """Render one node in Playwright-MCP style, matching Pipecat exactly."""
    parts: list[str] = [f"- {node.role}"]
    if node.name:
        parts.append(f'"{node.name}"')
    if node.value:
        parts.append(f'= "{node.value}"')
    if node.level is not None:
        parts.append(f"[level={node.level}]")
    if node.colcount is not None:
        parts.append(f"[cols={node.colcount}]")
    if node.rowcount is not None:
        parts.append(f"[rows={node.rowcount}]")
    for tag in node.state:
        if tag:
            parts.append(f"[{tag}]")
    if node.ref:
        parts.append(f"[ref={node.ref}]")

    line = "  " * depth + " ".join(parts)
    if node.children:
        line += ":"
    lines.append(line)

    for child in node.children:
        render_node(child, depth + 1, lines)


def render_ui_state(snapshot: Snapshot) -> str:
    """Render a snapshot as the `<ui_state>` block the LLM sees."""
    lines = ["<ui_state>"]
    render_node(snapshot.root, 0, lines)
    if snapshot.selection and snapshot.selection.ref and snapshot.selection.text:
        lines.append(f'<selection ref="{snapshot.selection.ref}">')
        lines.append(snapshot.selection.text)
        lines.append("</selection>")
    lines.append("</ui_state>")
    return "\n".join(lines)


def _subtree_size(node: A11yNode) -> int:
    return 1 + sum(_subtree_size(child) for child in node.children)


def _collapse(node: A11yNode) -> int:
    """Drop a node's children, tagging it so the model knows it was elided."""
    removed = _subtree_size(node) - 1
    node.children = []
    if COLLAPSED not in node.state:
        node.state = [*node.state, COLLAPSED]
    return removed


def _collapsible_offscreen(root: A11yNode) -> list[A11yNode]:
    """Offscreen container nodes, largest subtree first.

    Only nodes that are themselves offscreen are eligible: a node the user can
    see stays intact even if it is expensive, because the demo is about what is
    on screen right now.
    """
    candidates = [
        node
        for node in root.walk()
        if node.offscreen and node.children and COLLAPSED not in node.state
    ]
    candidates.sort(key=_subtree_size, reverse=True)
    return candidates


def _truncate_names(root: A11yNode) -> None:
    for node in root.walk():
        if node.name and len(node.name) > MAX_NAME_CHARS:
            node.name = node.name[: MAX_NAME_CHARS - 1] + "…"
        if node.value and len(node.value) > MAX_NAME_CHARS:
            node.value = node.value[: MAX_NAME_CHARS - 1] + "…"


def _cap_depth(node: A11yNode, max_depth: int, depth: int = 0) -> int:
    removed = 0
    if depth >= max_depth:
        return _collapse(node)
    for child in node.children:
        removed += _cap_depth(child, max_depth, depth + 1)
    return removed


class PruneStats:
    """What pruning did, for logging and for the limitations doc."""

    __slots__ = ("collapsed_subtrees", "depth_capped", "nodes_after", "nodes_before")

    def __init__(self) -> None:
        self.nodes_before = 0
        self.nodes_after = 0
        self.collapsed_subtrees = 0
        self.depth_capped = False

    def as_dict(self) -> dict[str, object]:
        return {
            "nodes_before": self.nodes_before,
            "nodes_after": self.nodes_after,
            "collapsed_subtrees": self.collapsed_subtrees,
            "depth_capped": self.depth_capped,
        }


def prune(snapshot: Snapshot, *, max_nodes: int, max_depth: int) -> PruneStats:
    """Bring a snapshot within budget, in place. Returns what it had to do.

    Order matters: names first (free), then offscreen subtrees (invisible to
    the user anyway), then a depth cap as a last resort. A snapshot that is
    already within budget is left completely untouched.
    """
    stats = PruneStats()
    stats.nodes_before = snapshot.node_count()

    _truncate_names(snapshot.root)

    remaining = stats.nodes_before
    if remaining > max_nodes:
        for candidate in _collapsible_offscreen(snapshot.root):
            if remaining <= max_nodes:
                break
            remaining -= _collapse(candidate)
            stats.collapsed_subtrees += 1

    if remaining > max_nodes:
        removed = _cap_depth(snapshot.root, max_depth)
        if removed:
            stats.depth_capped = True
            remaining -= removed

    stats.nodes_after = snapshot.node_count()
    return stats
