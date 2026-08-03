"""Post-action verification.

The rule the whole project hangs on: the agent may not claim an action worked
until the page says it did. This module turns a before/after pair into a list of
observed changes, and that list is the only thing licensed to become "done".

Verification is intentionally structural rather than semantic. We do not try to
decide whether the *right* thing happened — that is the model's job, using this
evidence. We only establish that something consistent with the request actually
changed, and say so plainly when it did not.
"""

from __future__ import annotations

from karumi_agent.domain.models import (
    ActionKind,
    ElementState,
    Evidence,
    PageInfo,
    Snapshot,
)

#: Scroll deltas below this are noise (momentum, sub-pixel layout shifts).
SCROLL_EPSILON_PX = 8


def _page_changes(before: PageInfo, after: PageInfo) -> list[str]:
    changes: list[str] = []
    if before.url != after.url:
        changes.append(f"url {before.url} → {after.url}")
    if before.title != after.title:
        changes.append(f"title {before.title!r} → {after.title!r}")
    if abs(before.scroll_y - after.scroll_y) > SCROLL_EPSILON_PX:
        changes.append(f"scrolled {before.scroll_y}px → {after.scroll_y}px")
    return changes


def _element_changes(before: ElementState | None, after: ElementState | None) -> list[str]:
    if before is None and after is None:
        return []
    if before is not None and after is None:
        return ["target element left the page"]
    if before is None and after is not None:
        return ["target element appeared"]

    assert before is not None and after is not None
    changes: list[str] = []
    if before.value != after.value:
        changes.append(f"value {before.value!r} → {after.value!r}")
    if before.checked != after.checked:
        changes.append(f"checked {before.checked} → {after.checked}")
    if before.expanded != after.expanded:
        changes.append(f"expanded {before.expanded} → {after.expanded}")
    if before.selected != after.selected:
        changes.append(f"selected {before.selected} → {after.selected}")
    if before.focused != after.focused:
        changes.append(f"focused {before.focused} → {after.focused}")
    if before.in_viewport != after.in_viewport:
        changes.append(f"in viewport {before.in_viewport} → {after.in_viewport}")
    if before.disabled != after.disabled:
        changes.append(f"disabled {before.disabled} → {after.disabled}")
    return changes


def _tree_delta(before: Snapshot, after: Snapshot) -> tuple[int, int, list[str]]:
    """Refs that appeared or vanished — the signal an SPA actually re-rendered."""
    before_refs = set(before.index())
    after_refs = set(after.index())
    added = after_refs - before_refs
    removed = before_refs - after_refs

    changes: list[str] = []
    if added:
        changes.append(f"{len(added)} new element(s) appeared")
    if removed:
        changes.append(f"{len(removed)} element(s) disappeared")
    return len(added), len(removed), changes


def build_evidence(
    *,
    before: Snapshot,
    after: Snapshot,
    element_before: ElementState | None,
    element_after: ElementState | None,
) -> Evidence:
    """Diff two observations into reportable evidence."""
    refs_added, refs_removed, tree_changes = _tree_delta(before, after)
    changes = [
        *_page_changes(before.page, after.page),
        *_element_changes(element_before, element_after),
        *tree_changes,
    ]
    return Evidence(
        page_before=before.page,
        page_after=after.page,
        element_before=element_before,
        element_after=element_after,
        changes=changes,
        refs_added=refs_added,
        refs_removed=refs_removed,
        snapshot_id_after=after.snapshot_id,
    )


def is_verified(kind: ActionKind, evidence: Evidence) -> bool:
    """Decide whether the evidence actually confirms this kind of action.

    Each action has its own bar, because "the page changed" is too weak a test
    for some and too strong for others. A click on an inert element and a click
    that opened a modal both "succeed" at the Playwright level; only one of them
    is something we should tell a user about.
    """
    return _verdict(kind, evidence)[0]


def verified_because(kind: ActionKind, evidence: Evidence) -> str:
    """Why the evidence counts as confirmation — the sentence behind "done".

    Some actions are confirmed by a page change and some by a state the page
    now holds. A highlight, for instance, is *supposed* to leave the document
    untouched, so "nothing changed" is the success case and the confirmation
    has to come from elsewhere. Saying so explicitly keeps the agent honest
    instead of letting an empty diff read as success.
    """
    return _verdict(kind, evidence)[1]


def _verdict(kind: ActionKind, evidence: Evidence) -> tuple[bool, str]:
    joined = ", ".join(evidence.changes)

    match kind:
        case ActionKind.SCROLL_TO:
            # Either the page moved, or the element was already on screen —
            # both mean the user can now see it, which is what was asked.
            moved = any("scrolled" in c for c in evidence.changes)
            visible = evidence.element_after is not None and evidence.element_after.in_viewport
            if moved:
                return True, joined
            if visible:
                return True, "target was already within the viewport"
            return False, "the target is still outside the viewport"

        case ActionKind.HIGHLIGHT:
            # A highlight is a pure overlay: the page is *supposed* to be
            # unchanged. What we verify is that the element is real and on
            # screen, so the ring is actually pointing at something.
            after = evidence.element_after
            if after is not None and after.visible and after.in_viewport:
                rect = after.rect
                return True, (
                    f"element is visible on screen at "
                    f"{rect.get('width', 0)}x{rect.get('height', 0)}px, ring drawn"
                )
            return False, "the target is not visible on screen, so nothing was pointed at"

        case ActionKind.SELECT_TEXT:
            if evidence.element_after is not None:
                return True, "text selected on a live element"
            return False, "the target no longer exists"

        case ActionKind.FILL:
            value_changed = [c for c in evidence.changes if c.startswith("value ")]
            if value_changed:
                return True, value_changed[0]
            return False, "the field's value did not change"

        case ActionKind.CLICK:
            # Any observable consequence counts: navigation, a re-render, or a
            # state flip on the control itself.
            if evidence.changes:
                return True, joined
            return False, "the click produced no observable change"

        case ActionKind.NAVIGATE | ActionKind.GO_BACK:
            if evidence.page_before.url != evidence.page_after.url:
                return True, f"now at {evidence.page_after.url}"
            return False, "the url did not change"

    return bool(evidence.changes), joined
