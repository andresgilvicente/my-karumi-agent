"""Structural queries over a snapshot.

Generic, role-based lookups — the vocabulary the agent uses to talk about a page
it has never seen. There is deliberately nothing here that knows about a
particular product: no ids, no class names, no text constants. Everything is
expressed in terms of ARIA roles, accessible names and tree position, which is
what survives a redesign.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

from karumi_agent.domain.models import A11yNode, Snapshot

# The walker's role vocabulary is a mix of explicit ARIA roles and roles derived
# from the tag name, so a plain `<nav>` surfaces as "nav" while an element with
# `role="navigation"` surfaces as "navigation". Both spellings are listed
# throughout this module — matching only the ARIA name silently misses the
# majority of real sites, which use the semantic HTML element.

#: Containers that group navigation controls on essentially every site.
NAVIGATION_ROLES = frozenset({"nav", "navigation", "menubar", "tablist", "menu"})

#: Roles a user can meaningfully act on.
INTERACTIVE_ROLES = frozenset(
    {
        "button",
        "link",
        "a",
        "tab",
        "menuitem",
        "checkbox",
        "radio",
        "switch",
        "combobox",
        "select",
        "textbox",
        "searchbox",
        "textarea",
        "input",
        "slider",
        "option",
    }
)

#: Roles that hold user-editable text.
TEXT_FIELD_ROLES = frozenset({"textbox", "searchbox", "textarea", "input", "combobox"})


def nodes(snapshot: Snapshot) -> Iterator[A11yNode]:
    yield from snapshot.root.walk()


def by_role(snapshot: Snapshot, *roles: str) -> list[A11yNode]:
    wanted = {role.lower() for role in roles}
    return [node for node in nodes(snapshot) if node.role.lower() in wanted]


def by_name(snapshot: Snapshot, pattern: str, *, roles: set[str] | None = None) -> list[A11yNode]:
    """Nodes whose accessible name matches `pattern` (case-insensitive regex)."""
    regex = re.compile(pattern, re.IGNORECASE)
    out = []
    for node in nodes(snapshot):
        if roles and node.role.lower() not in roles:
            continue
        if node.name and regex.search(node.name):
            out.append(node)
    return out


def headings(snapshot: Snapshot) -> list[A11yNode]:
    return by_role(snapshot, "heading")


def interactive(snapshot: Snapshot) -> list[A11yNode]:
    return [node for node in nodes(snapshot) if node.role.lower() in INTERACTIVE_ROLES]


def text_fields(snapshot: Snapshot) -> list[A11yNode]:
    return [node for node in nodes(snapshot) if node.role.lower() in TEXT_FIELD_ROLES]


def descendants(node: A11yNode) -> Iterator[A11yNode]:
    for child in node.children:
        yield from child.walk()


def navigation_controls(snapshot: Snapshot) -> list[A11yNode]:
    """Interactive nodes inside a navigation landmark.

    This is how the agent finds "the menu" without being told where it is: not
    by looking for a selector, but by asking which controls the page itself
    declares as navigation.
    """
    found: list[A11yNode] = []
    for container in nodes(snapshot):
        if container.role.lower() not in NAVIGATION_ROLES:
            continue
        found.extend(
            child for child in descendants(container) if child.role.lower() in INTERACTIVE_ROLES
        )
    return found


def offscreen(snapshot: Snapshot, *roles: str) -> list[A11yNode]:
    """Nodes the user cannot currently see — scroll targets."""
    candidates = by_role(snapshot, *roles) if roles else list(nodes(snapshot))
    return [node for node in candidates if node.offscreen and node.ref]


def role_histogram(snapshot: Snapshot) -> dict[str, int]:
    counts: dict[str, int] = {}
    for node in nodes(snapshot):
        counts[node.role] = counts.get(node.role, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def describe_node(node: A11yNode) -> str:
    """Short human-facing label used in spike output and logs."""
    label = f'{node.role} "{node.name}"' if node.name else node.role
    tags = "".join(f"[{tag}]" for tag in node.state)
    return f"{label}{tags} ({node.ref})" if node.ref else f"{label}{tags}"
