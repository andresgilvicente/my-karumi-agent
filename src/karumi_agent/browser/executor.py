"""Executing actions against the live page, and proving they worked.

Every action runs the same pipeline:

1. **Resolve the snapshot.** The request names the observation its ref came from;
   if that is not the current one, we refuse. No re-resolving against a page that
   has moved on.
2. **Classify for safety.** Read-only actions pass. Mutating ones are checked
   against the target's accessible name, and a sensitive one comes back as
   ``NEEDS_CONFIRMATION`` rather than being performed.
3. **Act**, using Playwright's ``aria-ref`` selector engine, which fails closed
   when the element has detached.
4. **Re-observe and verify.** ``verified`` is only true when the intended effect
   is visible in the new observation. "No exception was raised" is not evidence.

That last point is the reason this module exists in this shape: an agent that
narrates success it did not confirm is worse than one that admits uncertainty.
"""

from __future__ import annotations

import contextlib
import time
from typing import TYPE_CHECKING

from karumi_agent.browser.overlay import OverlayState
from karumi_agent.browser.session import BrowserSession
from karumi_agent.browser.snapshotter import Snapshotter
from karumi_agent.config import SafetySettings
from karumi_agent.domain.actions import (
    ActionKind,
    ActionRequest,
    ActionResult,
    ActionStatus,
    Evidence,
    ObservedChange,
)
from karumi_agent.domain.safety import SafetyVerdict, Sensitivity, classify_action
from karumi_agent.domain.snapshot import PageSnapshot, SnapshotNode
from karumi_agent.errors import KarumiError, StaleSnapshotError
from karumi_agent.logging import get_logger

if TYPE_CHECKING:
    from playwright.async_api import Locator

log = get_logger("executor")


class ActionExecutor:
    """Runs typed actions and returns typed, verified results."""

    def __init__(
        self,
        session: BrowserSession,
        snapshotter: Snapshotter,
        safety: SafetySettings,
    ) -> None:
        self._session = session
        self._snapshotter = snapshotter
        self._safety = safety
        #: Set by the voice layer when the operator says yes to a confirmation.
        self._confirmed_action_ids: set[str] = set()

    def confirm(self, action_id: str) -> None:
        """Record that the operator approved a specific pending action."""
        self._confirmed_action_ids.add(action_id)

    # ------------------------------------------------------------------ main
    async def execute(self, request: ActionRequest) -> ActionResult:
        """Run one action end to end."""
        started = time.perf_counter()
        result = ActionResult(
            action_id=request.action_id,
            kind=request.kind,
            status=ActionStatus.FAILED,
            source_snapshot_id=request.source_snapshot_id,
        )

        try:
            snapshot = self._resolve_snapshot(request)
        except StaleSnapshotError as exc:
            result.status = ActionStatus.STALE
            result.error = exc.as_dict()
            result.duration_ms = (time.perf_counter() - started) * 1000
            log.warning("action refused: stale snapshot", **request.as_dict())
            return result

        node = snapshot.resolve(request.ref) if request.ref else None

        # A ref the model produced that is not in the snapshot it cited is a bug
        # in the plan, not a transient failure. Fail loudly rather than guessing.
        if request.kind.needs_ref and node is None:
            result.status = ActionStatus.FAILED
            result.error = {
                "code": "ref_not_found",
                "message": f"Ref {request.ref!r} is not in snapshot {snapshot.snapshot_id}",
            }
            result.duration_ms = (time.perf_counter() - started) * 1000
            return result

        verdict = await self._classify(request, node)
        if verdict.sensitivity is Sensitivity.BLOCKED:
            result.status = ActionStatus.REFUSED
            result.error = {"code": "blocked", "message": verdict.reason}
            result.duration_ms = (time.perf_counter() - started) * 1000
            log.warning("action refused: blocked", reason=verdict.reason, **request.as_dict())
            return result

        # Two independent routes to the confirmation gate: what the target is
        # labelled, and what the operator asked for. The second matters because
        # the model can pick a target that does not match its own stated intent.
        sensitive = verdict.sensitivity is Sensitivity.CONFIRM or request.force_confirmation
        needs_confirmation = (
            sensitive
            and self._safety.require_confirmation
            and request.action_id not in self._confirmed_action_ids
        )
        if needs_confirmation:
            label = request.target_label or (node.label if node else request.url or "that")
            reason = (
                verdict.reason
                if verdict.sensitivity is Sensitivity.CONFIRM
                else (request.confirmation_reason or "could change something")
            )
            result.status = ActionStatus.NEEDS_CONFIRMATION
            result.confirmation_prompt = (
                f'"{label}" looks like it {reason}. Do you want me to go ahead?'
            )
            result.duration_ms = (time.perf_counter() - started) * 1000
            log.info("action awaiting confirmation", reason=reason, **request.as_dict())
            return result

        before = _page_facts(snapshot)
        page = self._session.page
        await self._session.overlay.set_state(page, OverlayState.EXECUTING)

        try:
            await self._perform(request, node)
        except KarumiError as exc:
            result.error = exc.as_dict()
            result.duration_ms = (time.perf_counter() - started) * 1000
            await self._session.overlay.set_state(page, OverlayState.ERROR)
            log.warning("action failed", error=exc.code, **request.as_dict())
            return result
        except Exception as exc:
            result.error = {"code": "action_failed", "message": str(exc)[:300]}
            result.duration_ms = (time.perf_counter() - started) * 1000
            await self._session.overlay.set_state(page, OverlayState.ERROR)
            log.warning("action failed", error=str(exc)[:200], **request.as_dict())
            return result

        after = await self._snapshotter.capture_after_action()
        result.result_snapshot_id = after.snapshot_id
        result.status = ActionStatus.SUCCEEDED
        result.changes = _diff_facts(before, _page_facts(after))
        result.verified, result.verification_detail = self._verify(request, before, after, node)
        result.evidence = self._collect_evidence(request, after, node)
        result.duration_ms = (time.perf_counter() - started) * 1000

        await self._session.overlay.set_state(
            page, OverlayState.VERIFIED if result.verified else OverlayState.ERROR
        )
        log.info(
            "action done",
            verified=result.verified,
            detail=result.verification_detail,
            duration_ms=round(result.duration_ms, 1),
            **request.as_dict(),
        )
        return result

    # ------------------------------------------------------------- internals
    def _resolve_snapshot(self, request: ActionRequest) -> PageSnapshot:
        """Refuse to act on an observation that is no longer current."""
        if not request.source_snapshot_id:
            current = self._snapshotter.store.current
            if current is None:
                raise StaleSnapshotError(requested="", current=None)
            return current
        return self._snapshotter.store.require(request.source_snapshot_id)

    async def _classify(
        self,
        request: ActionRequest,
        node: SnapshotNode | None,
    ) -> SafetyVerdict:
        """Classify the action, consulting the DOM for password fields."""
        dom_is_password = False
        if request.kind is ActionKind.FILL and request.ref:
            with contextlib.suppress(Exception):
                locator = self._locator(request.ref)
                dom_is_password = await locator.get_attribute("type") == "password"

        leaves_origin = False
        if request.kind is ActionKind.NAVIGATE and request.url:
            leaves_origin = not self._session.navigation.is_allowed(request.url)
        elif node is not None and node.url:
            target = node.url
            if target.startswith(("http://", "https://")):
                leaves_origin = not self._session.navigation.is_allowed(target)

        opens_new_tab = bool(node and "target=_blank" in " ".join(node.states))

        return classify_action(
            request.kind,
            node,
            dom_is_password=dom_is_password,
            opens_new_tab=opens_new_tab,
            leaves_origin=leaves_origin,
        )

    def _locator(self, ref: str) -> Locator:
        """Resolve a ref through Playwright's aria-ref engine.

        This engine checks ``element.isConnected``, so a ref pointing at a
        detached node raises instead of acting on a ghost.
        """
        return self._session.page.locator(f"aria-ref={ref}")

    async def _perform(self, request: ActionRequest, node: SnapshotNode | None) -> None:
        """Dispatch to the concrete Playwright call."""
        session = self._session
        page = session.page
        kind = request.kind

        if kind is ActionKind.ANSWER:
            return

        if kind is ActionKind.NAVIGATE:
            if request.url:
                await session.goto(request.url)
            elif node is not None and node.url:
                await self._locator(request.ref or "").click()
            return

        if kind is ActionKind.BACK:
            await session.go_back()
            return

        if kind is ActionKind.FORWARD:
            await session.go_forward()
            return

        ref = request.ref or ""
        locator = self._locator(ref)

        if kind is ActionKind.HIGHLIGHT:
            await locator.scroll_into_view_if_needed()
            await session.overlay.clear_highlights(page)
            await session.overlay.highlight(
                page, locator, label=request.target_label or (node.label if node else None)
            )
            return

        if kind is ActionKind.SCROLL:
            await locator.scroll_into_view_if_needed()
            return

        if kind is ActionKind.SELECT_TEXT:
            # select_text() honours actionability checks, unlike a raw JS Range.
            await locator.select_text()
            return

        if kind is ActionKind.CLICK:
            # Playwright's actionability checks (visible, stable, receives events,
            # enabled) run here. Never pass force=True: those checks are exactly
            # what makes "safe click" the default rather than a thing we implement.
            await locator.click()
            return

        if kind is ActionKind.FILL:
            await locator.fill(request.text or "")
            return

    # ---------------------------------------------------------- verification
    def _verify(
        self,
        request: ActionRequest,
        before: dict[str, str],
        after: PageSnapshot,
        node: SnapshotNode | None,
    ) -> tuple[bool, str]:
        """Decide whether the intended effect actually happened.

        Each action kind has its own predicate, checked against a fresh
        observation. Anything we cannot confirm returns False with a reason.
        """
        kind = request.kind
        after_facts = _page_facts(after)

        if kind is ActionKind.ANSWER:
            return True, "no page change intended"

        if kind in {ActionKind.NAVIGATE, ActionKind.BACK, ActionKind.FORWARD}:
            if after_facts["url"] != before["url"]:
                return True, f"url changed to {after_facts['url']}"
            if after_facts["title"] != before["title"]:
                return True, f"title changed to {after_facts['title']!r}"
            return False, "url and title are unchanged"

        if kind in {ActionKind.SCROLL, ActionKind.HIGHLIGHT}:
            if request.ref is None:
                return False, "no element was targeted"
            moved = after.resolve(request.ref)
            if moved is None:
                # The ref is gone. Same role+name reappearing is good enough:
                # SPA re-renders reissue refs while keeping the element.
                if node is not None and _find_by_role_and_name(after, node) is not None:
                    return True, "element still present under a new ref"
                return False, "element is no longer on the page"
            if moved.box is not None and moved.box.intersects_viewport(
                after.viewport.width, after.viewport.height
            ):
                return True, "element is inside the viewport"
            return False, "element is still outside the viewport"

        if kind is ActionKind.CLICK:
            if after_facts["url"] != before["url"]:
                return True, f"url changed to {after_facts['url']}"
            if after_facts["title"] != before["title"]:
                return True, "page title changed"
            if after_facts["refs"] != before["refs"]:
                return True, "the set of interactive elements changed"
            if after_facts["node_count"] != before["node_count"]:
                return True, "the page structure changed"
            return False, "nothing observable changed after the click"

        if kind is ActionKind.FILL:
            if request.ref is None:
                return False, "no field was targeted"
            field = after.resolve(request.ref)
            if field is None:
                return False, "the field is no longer on the page"
            expected = (request.text or "").strip()
            actual = (field.value or "").strip()
            if expected and expected == actual:
                return True, "the field contains the expected value"
            return False, "the field does not contain the expected value"

        return False, "no verification rule for this action"

    def _collect_evidence(
        self,
        request: ActionRequest,
        after: PageSnapshot,
        node: SnapshotNode | None,
    ) -> list[Evidence]:
        """Record what we can point at, so claims stay grounded."""
        evidence = [
            Evidence(
                kind="page",
                detail=f"{after.title or 'untitled'} at {after.url}",
                snapshot_id=after.snapshot_id,
                url=after.url,
            )
        ]
        if request.ref:
            observed = after.resolve(request.ref) or node
            if observed is not None:
                evidence.append(
                    Evidence(
                        kind="element",
                        detail=observed.render_line(),
                        ref=request.ref,
                        snapshot_id=after.snapshot_id,
                    )
                )

        guard_events = self._session.guards.events.drain()
        for name, values in guard_events.items():
            for value in values[:3]:
                evidence.append(Evidence(kind=name, detail=value[:200]))
        return evidence


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _page_facts(snapshot: PageSnapshot) -> dict[str, str]:
    """The cheap comparable summary used to detect that something changed."""
    return {
        "url": snapshot.url,
        "title": snapshot.title,
        "refs": ",".join(sorted(snapshot.refs())),
        "node_count": str(snapshot.node_count),
        "snapshot_id": snapshot.snapshot_id,
    }


def _diff_facts(before: dict[str, str], after: dict[str, str]) -> list[ObservedChange]:
    changes: list[ObservedChange] = []
    for key in ("url", "title", "node_count"):
        if before.get(key) != after.get(key):
            changes.append(ObservedChange(field=key, before=before.get(key), after=after.get(key)))
    if before.get("refs") != after.get("refs"):
        before_refs = set((before.get("refs") or "").split(","))
        after_refs = set((after.get("refs") or "").split(","))
        added = sorted(after_refs - before_refs - {""})
        removed = sorted(before_refs - after_refs - {""})
        changes.append(
            ObservedChange(
                field="interactive_elements",
                before=f"{len(removed)} removed" if removed else None,
                after=f"{len(added)} added" if added else None,
            )
        )
    return changes


def _find_by_role_and_name(snapshot: PageSnapshot, node: SnapshotNode) -> SnapshotNode | None:
    """Re-find an element after a re-render reissued its ref."""
    for candidate in snapshot.root.walk():
        if candidate.role == node.role and candidate.name == node.name and candidate.name:
            return candidate
    return None
