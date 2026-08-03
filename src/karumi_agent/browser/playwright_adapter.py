"""Playwright implementation of the browser port.

Why Playwright and not raw CDP, an extension, or a remote browser: it gives us
`add_init_script` (inject into any origin, before page script runs), real
trusted input with actionability checks, native handling of frames, popups and
SPA navigation — and, because the browser is headed on the operator's machine,
the demo surface *is* the browser. No video encoding, no streaming, no lag.

The division of labour is deliberate. Pipecat's walker, running in the page,
owns observation and ref allocation. Playwright, running here, owns actuation.
Nothing in between ever asks the model for a selector, a coordinate or a line of
JavaScript.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from playwright.async_api import (
    Browser,
    BrowserContext,
    ElementHandle,
    Frame,
    Page,
    Playwright,
    ViewportSize,
    async_playwright,
)
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout

from karumi_agent.browser.verify import build_evidence, is_verified, verified_because
from karumi_agent.domain.models import (
    A11yNode,
    Action,
    ActionKind,
    ActionResult,
    ElementState,
    ErrorCode,
    PageInfo,
    Selection,
    Snapshot,
    Timings,
)
from karumi_agent.domain.ui_state import prune
from karumi_agent.observability.logging import get_logger
from karumi_agent.settings import BrowserSettings
from karumi_agent.tools.policy import ActionPolicy

log = get_logger(__name__)

BUNDLE_PATH = Path(__file__).parent / "injected" / "dist" / "karumi-page.js"

#: How many past snapshots stay addressable. An action referencing anything
#: older is rejected rather than guessed at.
SNAPSHOT_HISTORY = 24

#: Hard age limit for a snapshot an action may be built on.
MAX_SNAPSHOT_AGE_S = 60.0

#: Separator between the host-assigned frame id and the walker's own ref.
REF_SEP = "."


class SnapshotUnavailableError(RuntimeError):
    """Raised when no frame could be walked — the page is gone or blank."""


def _qualify(frame_id: str, ref: str) -> str:
    return f"{frame_id}{REF_SEP}{ref}"


def _split_ref(ref: str) -> tuple[str, str]:
    """`f0.e7` -> `("f0", "e7")`. A bare ref is assumed to be the main frame."""
    frame_id, _, local = ref.partition(REF_SEP)
    return (frame_id, local) if local else ("f0", ref)


def _qualify_tree(raw: dict[str, Any], frame_id: str) -> A11yNode:
    """Parse a raw walker node, namespacing every ref to its frame.

    The walker allocates refs per JS realm, so `e7` in a child frame and `e7` in
    the main document are different elements. Prefixing on ingest — where we
    know which frame spoke — makes refs unique page-wide without the page ever
    needing to learn its own identity.
    """
    node = A11yNode(
        ref=_qualify(frame_id, raw["ref"]) if raw.get("ref") else "",
        role=raw.get("role", "generic"),
        name=raw.get("name"),
        value=raw.get("value"),
        state=list(raw.get("state") or []),
        level=raw.get("level"),
        colcount=raw.get("colcount"),
        rowcount=raw.get("rowcount"),
    )
    node.children = [_qualify_tree(child, frame_id) for child in raw.get("children") or []]
    return node


class FrameSnapshot:
    """The latest observation of a single frame."""

    __slots__ = ("captured_at", "frame_id", "page", "root", "selection", "seq")

    def __init__(
        self,
        frame_id: str,
        root: A11yNode,
        captured_at: int,
        seq: int,
        page: PageInfo,
        selection: Selection | None,
    ) -> None:
        self.frame_id = frame_id
        self.root = root
        self.captured_at = captured_at
        self.seq = seq
        self.page = page
        self.selection = selection


class PlaywrightBrowserAdapter:
    """A real, visible Chromium the agent observes and drives."""

    def __init__(
        self,
        settings: BrowserSettings,
        policy: ActionPolicy | None = None,
    ) -> None:
        self._settings = settings
        self._policy = policy or ActionPolicy()

        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

        self._frame_ids: dict[Frame, str] = {}
        self._next_frame_index = 0
        self._frames: dict[str, FrameSnapshot] = {}
        #: frame id -> ref of the `<iframe>` element hosting it, when locatable.
        self._frame_hosts: dict[str, str] = {}

        self._snapshot_seq = 0
        self._current: Snapshot | None = None
        self._history: dict[str, Snapshot] = {}
        self._history_order: list[str] = []

        self._listeners: list[Callable[[Snapshot], None]] = []

    # -- lifecycle ---------------------------------------------------------

    @property
    def current(self) -> Snapshot | None:
        return self._current

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("browser not started")
        return self._page

    def subscribe(self, listener: Callable[[Snapshot], None]) -> None:
        self._listeners.append(listener)

    async def start(self, url: str) -> Snapshot:
        if not BUNDLE_PATH.exists():
            raise FileNotFoundError(
                f"Injected page bundle missing at {BUNDLE_PATH}. Build it with:\n"
                "  npm --prefix src/karumi_agent/browser/injected install\n"
                "  npm --prefix src/karumi_agent/browser/injected run build"
            )

        # Confine navigation to the origin the demo starts on unless the
        # operator opted out. A demo has no business wandering off-site.
        if not self._settings.allow_cross_origin_navigation:
            self._policy = self._policy.with_origin(url)

        self._playwright = await async_playwright().start()

        launch: dict[str, Any] = {
            "headless": self._settings.headless,
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        executable = self._settings.resolved_executable()
        if executable:
            # Preprovisioned images often ship a Chromium whose build number
            # does not match the installed Playwright; point at it explicitly
            # rather than failing on a managed-download lookup.
            launch["executable_path"] = executable

        viewport = ViewportSize(
            width=self._settings.viewport_width,
            height=self._settings.viewport_height,
        )

        if self._settings.user_data_dir:
            self._context = await self._playwright.chromium.launch_persistent_context(
                self._settings.user_data_dir, viewport=viewport, **launch
            )
        else:
            self._browser = await self._playwright.chromium.launch(**launch)
            self._context = await self._browser.new_context(viewport=viewport)

        self._context.set_default_timeout(self._settings.action_timeout_ms)
        self._context.set_default_navigation_timeout(self._settings.nav_timeout_ms)

        # Order matters: the binding must exist before the init script that
        # calls it, and both must be registered before the first navigation so
        # they apply at document start in every frame, including iframes.
        await self._context.expose_binding("__karumiEmitSnapshot", self._on_page_snapshot)
        await self._context.add_init_script(path=str(BUNDLE_PATH))

        self._context.on("page", self._on_new_page)

        pages = self._context.pages
        self._page = pages[0] if pages else await self._context.new_page()

        log.info("browser.start", url=url, executable=executable or "playwright-managed")
        await self._page.goto(url, wait_until="domcontentloaded")

        await self._start_streaming()
        return await self.observe(force=True)

    async def close(self) -> None:
        for closer in (self._context, self._browser):
            if closer is not None:
                with contextlib.suppress(PlaywrightError):  # already gone
                    await closer.close()
        if self._playwright is not None:
            await self._playwright.stop()
        self._playwright = None

    # -- frames ------------------------------------------------------------

    def _frame_id(self, frame: Frame) -> str:
        """Stable per-frame id, assigned in discovery order (`f0` = main)."""
        existing = self._frame_ids.get(frame)
        if existing is not None:
            return existing
        frame_id = f"f{self._next_frame_index}"
        self._next_frame_index += 1
        self._frame_ids[frame] = frame_id
        return frame_id

    def _frame_for_id(self, frame_id: str) -> Frame | None:
        for frame, fid in self._frame_ids.items():
            if fid == frame_id:
                return frame
        return None

    async def _start_streaming(self) -> None:
        """Ask every frame to push snapshots as the page changes."""
        options = {
            "debounceMs": self._settings.snapshot_debounce_ms,
            "trackViewport": self._settings.track_viewport,
        }
        for frame in self.page.frames:
            try:
                await frame.evaluate(
                    "(o) => window.__karumi && window.__karumi.startStream(o)", options
                )
            except PlaywrightError:
                continue  # frame detached or not yet injected; it will retry

    # -- observation -------------------------------------------------------

    async def _on_page_snapshot(self, source: dict[str, Any], raw: dict[str, Any]) -> None:
        """Binding target: a frame pushed a new snapshot."""
        frame = source.get("frame")
        if frame is None or not isinstance(raw, dict):
            return
        self._store_frame_snapshot(frame, raw)

    def _store_frame_snapshot(self, frame: Frame, raw: dict[str, Any]) -> None:
        frame_id = self._frame_id(frame)
        raw_page = raw.get("page") or {}
        selection_raw = raw.get("selection")
        selection = None
        if isinstance(selection_raw, dict) and selection_raw.get("ref"):
            selection = Selection(
                ref=_qualify(frame_id, selection_raw["ref"]),
                text=selection_raw.get("text", ""),
                start_offset=selection_raw.get("start_offset"),
                end_offset=selection_raw.get("end_offset"),
            )

        self._frames[frame_id] = FrameSnapshot(
            frame_id=frame_id,
            root=_qualify_tree(raw.get("root") or {"ref": "", "role": "generic"}, frame_id),
            captured_at=int(raw.get("captured_at") or time.time() * 1000),
            seq=int(raw.get("seq") or 0),
            page=PageInfo.model_validate(raw_page) if raw_page else PageInfo(url="", title=""),
            selection=selection,
        )

    async def observe(self, *, force: bool = False) -> Snapshot:
        """Return the page state, re-walking every frame when `force`."""
        if force or not self._frames:
            await self._walk_all_frames()
        elif self._current is not None:
            return self._current
        return self._publish()

    async def _walk_all_frames(self) -> None:
        options = {"trackViewport": self._settings.track_viewport}
        seen: set[str] = set()
        for frame in self.page.frames:
            try:
                raw = await frame.evaluate(
                    "(o) => window.__karumi ? window.__karumi.snapshot(o) : null", options
                )
            except PlaywrightError:
                continue  # detached mid-walk, or a frame we cannot script
            if isinstance(raw, dict):
                self._store_frame_snapshot(frame, raw)
                seen.add(self._frame_id(frame))

        # Forget frames that no longer exist so their refs stop resolving.
        for frame_id in [fid for fid in self._frames if fid not in seen]:
            if self._frame_for_id(frame_id) not in self.page.frames:
                self._frames.pop(frame_id, None)
                self._frame_hosts.pop(frame_id, None)

        if len(self._frames) > 1:
            await self._refresh_frame_hosts()

    async def _iframe_host_ref(self, frame: Frame) -> str | None:
        """The ref of the `<iframe>` element that hosts `frame`, if we can find it.

        Used to splice a child frame's tree into the right place in its parent,
        so the model sees one coherent page rather than a pile of documents.
        """
        parent = frame.parent_frame
        if parent is None:
            return None
        try:
            handle = await frame.frame_element()
            local = await parent.evaluate(
                "(el) => window.__karumi ? window.__karumi.refForElement(el) : null", handle
            )
        except PlaywrightError:
            return None
        if not local:
            return None
        return _qualify(self._frame_id(parent), str(local))

    def _publish(self) -> Snapshot:
        """Merge per-frame trees into one snapshot and hand it out."""
        main = self._frames.get("f0")
        if main is None:
            raise SnapshotUnavailableError("no frame produced a snapshot")

        root = main.root.model_copy(deep=True)

        # Child frames are attached under their host <iframe> when we managed to
        # locate it, and appended at the root otherwise. Losing the position is
        # a cosmetic degradation; losing the content would not be.
        if len(self._frames) > 1:
            hosts = {node.ref: node for node in root.walk()}
            for frame_id, snap in self._frames.items():
                if frame_id == "f0":
                    continue
                subtree = snap.root.model_copy(deep=True)
                subtree.role = "iframe"
                subtree.name = snap.page.title or snap.page.url
                host = hosts.get(self._frame_hosts.get(frame_id, ""))
                (host or root).children.append(subtree)

        self._snapshot_seq += 1
        snapshot = Snapshot(
            snapshot_id=f"s{self._snapshot_seq}",
            captured_at=main.captured_at,
            page=main.page,
            root=root,
            selection=main.selection,
            frame_count=len(self._frames),
        )

        stats = prune(
            snapshot,
            max_nodes=self._settings.max_nodes,
            max_depth=self._settings.max_depth,
        )
        if stats.nodes_after != stats.nodes_before:
            log.info("snapshot.pruned", snapshot_id=snapshot.snapshot_id, **stats.as_dict())

        self._remember(snapshot)
        self._current = snapshot
        for listener in self._listeners:
            listener(snapshot)
        return snapshot

    def _remember(self, snapshot: Snapshot) -> None:
        self._history[snapshot.snapshot_id] = snapshot
        self._history_order.append(snapshot.snapshot_id)
        while len(self._history_order) > SNAPSHOT_HISTORY:
            self._history.pop(self._history_order.pop(0), None)

    # -- element access ----------------------------------------------------

    async def _handle_for(self, ref: str) -> tuple[Frame, ElementHandle] | None:
        frame_id, local = _split_ref(ref)
        frame = self._frame_for_id(frame_id)
        if frame is None:
            return None
        try:
            js_handle = await frame.evaluate_handle(
                "(r) => window.__karumi ? window.__karumi.resolve(r) : null", local
            )
            element = js_handle.as_element()
        except PlaywrightError:
            return None
        if element is None:
            return None
        return frame, element

    async def describe(self, ref: str) -> ElementState | None:
        frame_id, local = _split_ref(ref)
        frame = self._frame_for_id(frame_id)
        if frame is None:
            return None
        try:
            raw = await frame.evaluate(
                "(r) => window.__karumi ? window.__karumi.describe(r) : null", local
            )
        except PlaywrightError:
            return None
        if not isinstance(raw, dict):
            return None
        raw["ref"] = ref  # report the qualified ref back, not the frame-local one
        return ElementState.model_validate(raw)

    # -- action ------------------------------------------------------------

    def _check_freshness(self, action: Action) -> tuple[Snapshot, None] | tuple[None, ActionResult]:
        """Refuse to act on a page the model has not actually seen.

        Rejecting on *every* new snapshot would make the agent unusable — the
        walker emits one every few hundred milliseconds. What matters is whether
        the page moved out from under the decision: an unknown or expired id, a
        navigation since, or a target that has left the DOM.
        """
        reference = self._history.get(action.snapshot_id)
        if reference is None:
            return None, self._fail(
                action,
                ErrorCode.STALE_SNAPSHOT,
                f"snapshot {action.snapshot_id} is unknown or has expired; observe again",
            )

        age = time.time() - reference.captured_at / 1000
        if age > MAX_SNAPSHOT_AGE_S:
            return None, self._fail(
                action,
                ErrorCode.STALE_SNAPSHOT,
                f"snapshot {action.snapshot_id} is {age:.0f}s old; observe again",
            )

        current = self._current
        if current is not None and current.page.url != reference.page.url:
            return None, self._fail(
                action,
                ErrorCode.STALE_SNAPSHOT,
                (
                    f"the page navigated to {current.page.url} after snapshot "
                    f"{action.snapshot_id} was taken; observe again"
                ),
            )

        return reference, None

    @staticmethod
    def _fail(action: Action, code: ErrorCode, message: str) -> ActionResult:
        return ActionResult(
            action_id=action.action_id,
            kind=action.kind,
            ok=False,
            verified=False,
            error_code=code,
            message=message,
        )

    async def perform(self, action: Action) -> ActionResult:
        started = time.perf_counter()

        reference, failure = self._check_freshness(action)
        if failure is not None:
            log.warning(
                "action.rejected",
                action_id=action.action_id,
                kind=action.kind,
                error=failure.error_code,
                message=failure.message,
            )
            return failure
        assert reference is not None

        needs_target = action.kind not in (ActionKind.NAVIGATE, ActionKind.GO_BACK)
        element_before: ElementState | None = None

        if needs_target:
            if not action.ref:
                return self._fail(action, ErrorCode.INVALID_ACTION, f"{action.kind} needs a ref")
            element_before = await self.describe(action.ref)
            if element_before is None:
                return self._fail(
                    action,
                    ErrorCode.REF_NOT_FOUND,
                    f"{action.ref} is no longer on the page; observe again",
                )

        decision = self._policy.check(action, element_before)
        if not decision.allowed:
            log.warning(
                "action.blocked",
                action_id=action.action_id,
                kind=action.kind,
                error=decision.error_code,
                reason=decision.reason,
            )
            return self._fail(
                action, decision.error_code or ErrorCode.BLOCKED_BY_POLICY, decision.reason
            )

        before = self._current or await self.observe(force=True)
        observe_ms = (time.perf_counter() - started) * 1000

        exec_started = time.perf_counter()
        try:
            await self._execute(action)
        except PlaywrightTimeout as exc:
            return self._fail(
                action, ErrorCode.ELEMENT_NOT_ACTIONABLE, f"timed out: {_brief(exc)}"
            )
        except PlaywrightError as exc:
            return self._fail(action, ErrorCode.BROWSER_ERROR, _brief(exc))
        execute_ms = (time.perf_counter() - exec_started) * 1000

        verify_started = time.perf_counter()
        await self.page.wait_for_timeout(self._settings.settle_ms)
        after = await self.observe(force=True)
        element_after = await self.describe(action.ref) if action.ref else None

        evidence = build_evidence(
            before=before,
            after=after,
            element_before=element_before,
            element_after=element_after,
        )
        verified = is_verified(action.kind, evidence)
        because = verified_because(action.kind, evidence)
        verify_ms = (time.perf_counter() - verify_started) * 1000

        result = ActionResult(
            action_id=action.action_id,
            kind=action.kind,
            ok=True,
            verified=verified,
            error_code=None if verified else ErrorCode.NO_CHANGE_DETECTED,
            message=because,
            evidence=evidence,
            timings=Timings(
                observe_ms=observe_ms, execute_ms=execute_ms, verify_ms=verify_ms
            ),
        )

        log.info(
            "action.performed",
            action_id=action.action_id,
            kind=action.kind,
            ref=action.ref,
            verified=verified,
            changes=evidence.changes,
            observe_ms=round(observe_ms, 1),
            execute_ms=round(execute_ms, 1),
            verify_ms=round(verify_ms, 1),
        )
        return result

    async def _execute(self, action: Action) -> None:
        timeout = self._settings.action_timeout_ms

        if action.kind == ActionKind.NAVIGATE:
            assert action.url
            await self.page.goto(action.url, wait_until="domcontentloaded")
            await self._start_streaming()
            return

        if action.kind == ActionKind.GO_BACK:
            await self.page.go_back(wait_until="domcontentloaded")
            await self._start_streaming()
            return

        assert action.ref
        frame_id, local = _split_ref(action.ref)

        if action.kind == ActionKind.HIGHLIGHT:
            frame = self._frame_for_id(frame_id)
            if frame is not None:
                await frame.evaluate(
                    "([r, ms]) => window.__karumi.highlight(r, ms)",
                    [local, action.duration_ms],
                )
            return

        if action.kind == ActionKind.SELECT_TEXT:
            frame = self._frame_for_id(frame_id)
            if frame is not None:
                await frame.evaluate(
                    "([r, s, e]) => window.__karumi.selectText(r, s, e)",
                    [local, action.start_offset, action.end_offset],
                )
            return

        resolved = await self._handle_for(action.ref)
        if resolved is None:
            raise PlaywrightError(f"{action.ref} no longer resolves to an element")
        _, element = resolved

        match action.kind:
            case ActionKind.SCROLL_TO:
                await element.scroll_into_view_if_needed(timeout=timeout)
            case ActionKind.CLICK:
                await element.click(timeout=timeout)
            case ActionKind.FILL:
                value = action.value or ""
                if action.replace:
                    await element.fill(value, timeout=timeout)
                else:
                    await element.click(timeout=timeout)
                    await element.type(value)
            case _:
                raise PlaywrightError(f"unsupported action {action.kind}")

    # -- overlay -----------------------------------------------------------

    async def set_status(self, state: str | None, label: str = "") -> None:
        """Drive the on-page status pill. Never fails the caller."""
        with contextlib.suppress(PlaywrightError):
            await self.page.evaluate(
                "([s, l]) => window.__karumi && window.__karumi.setStatus(s, l)", [state, label]
            )

    async def set_caption(self, text: str) -> None:
        with contextlib.suppress(PlaywrightError):
            await self.page.evaluate(
                "(t) => window.__karumi && window.__karumi.setCaption(t)", text
            )

    # -- events ------------------------------------------------------------

    def _on_new_page(self, page: Page) -> None:
        """Follow popups: a link that opened a tab is still part of the demo."""
        log.info("browser.new_page", url=page.url)
        self._page = page
        self._frames.clear()
        self._frame_hosts.clear()

    async def _refresh_frame_hosts(self) -> None:
        """Locate each child frame's host `<iframe>` so subtrees splice in place."""
        hosts: dict[str, str] = {}
        for frame in self.page.frames:
            if frame.parent_frame is None:
                continue
            host_ref = await self._iframe_host_ref(frame)
            if host_ref:
                hosts[self._frame_id(frame)] = host_ref
        self._frame_hosts = hosts


def _brief(exc: BaseException) -> str:
    """First line of a Playwright error — the rest is a call log we already log."""
    return str(exc).split("\n", 1)[0].strip()
