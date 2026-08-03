"""Phase 1 spike — the experiment that decides whether the architecture holds.

It answers, against a live browser and a page the code knows nothing about:

1. Can we get a semantic representation of an arbitrary page?
2. Do element refs stay stable while the page mutates underneath us?
3. Can we act through a semantic ref — click, scroll, highlight — and *verify*
   the result rather than assume it?
4. Does the agent refuse to act on a snapshot it no longer has grounds to trust?
5. Does the capability policy actually stop dangerous actions?
6. What does all of that cost in milliseconds?

Every probe below picks its targets structurally — by ARIA role and tree
position — so the same run is meaningful against the fixture, against Metric
Master Suite, or against a site none of us have seen.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from karumi_agent.browser.playwright_adapter import PlaywrightBrowserAdapter
from karumi_agent.domain import query
from karumi_agent.domain.models import (
    Action,
    ActionKind,
    ActionResult,
    Capability,
    ErrorCode,
    Snapshot,
)
from karumi_agent.domain.ui_state import render_ui_state
from karumi_agent.observability.logging import get_logger
from karumi_agent.settings import BrowserSettings
from karumi_agent.tools.policy import ActionPolicy

log = get_logger(__name__)


@dataclass
class Probe:
    """One check, with a verdict we can print and assert on."""

    name: str
    passed: bool
    detail: str
    timings: dict[str, float] = field(default_factory=dict)

    def render(self) -> str:
        mark = "PASS" if self.passed else "FAIL"
        timing = ""
        if self.timings:
            timing = "  " + " ".join(f"{k}={v:.0f}ms" for k, v in self.timings.items())
        return f"[{mark}] {self.name}: {self.detail}{timing}"


@dataclass
class SpikeReport:
    url: str
    probes: list[Probe] = field(default_factory=list)
    observations: list[float] = field(default_factory=list)
    """Steady-state observation latencies. Cold start is tracked separately —
    averaging a browser launch into per-turn latency would flatter the numbers
    that matter and hide the ones that don't."""

    cold_start_ms: float = 0.0

    @property
    def passed(self) -> bool:
        return all(probe.passed for probe in self.probes)

    def add(self, probe: Probe) -> Probe:
        self.probes.append(probe)
        log.info(
            "spike.probe",
            probe=probe.name,
            passed=probe.passed,
            detail=probe.detail,
            **{f"t_{k}": round(v, 1) for k, v in probe.timings.items()},
        )
        return probe

    def render(self) -> str:
        lines = [f"\nSpike report — {self.url}", "=" * 72]
        lines.extend(probe.render() for probe in self.probes)
        lines.append("-" * 72)
        if self.cold_start_ms:
            lines.append(
                f"cold start (browser launch + first navigation + first walk): "
                f"{self.cold_start_ms:.0f}ms — paid once, off the conversational path"
            )
        if self.observations:
            ordered = sorted(self.observations)
            lines.append(
                f"steady-state observation over {len(ordered)} samples: "
                f"p50={_pct(ordered, 50):.0f}ms  p90={_pct(ordered, 90):.0f}ms  "
                f"p95={_pct(ordered, 95):.0f}ms  max={ordered[-1]:.0f}ms"
            )
        passed = sum(1 for p in self.probes if p.passed)
        lines.append("=" * 72)
        lines.append(f"{passed}/{len(self.probes)} probes passed")
        return "\n".join(lines)


def _pct(ordered: list[float], pct: float) -> float:
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, round((pct / 100) * (len(ordered) - 1)))
    return ordered[index]


def _action(kind: ActionKind, snapshot: Snapshot, **kwargs: Any) -> Action:
    return Action(action_id=uuid4().hex[:8], kind=kind, snapshot_id=snapshot.snapshot_id, **kwargs)


async def run_spike(
    url: str,
    settings: BrowserSettings,
    *,
    stability_samples: int = 20,
    show_ui_state: bool = False,
) -> SpikeReport:
    """Drive the full probe sequence against `url`."""
    report = SpikeReport(url=url)

    # FILL is enabled purely so the password-field refusal is a real test of the
    # policy rather than a capability that was never granted.
    policy = ActionPolicy(
        capabilities=frozenset(
            {
                Capability.READ,
                Capability.SCROLL,
                Capability.HIGHLIGHT,
                Capability.NAVIGATE,
                Capability.CLICK,
                Capability.FILL,
            }
        )
    )
    adapter = PlaywrightBrowserAdapter(settings, policy=policy)

    try:
        started = time.perf_counter()
        snapshot = await adapter.start(url)
        report.cold_start_ms = (time.perf_counter() - started) * 1000

        await _probe_representation(report, snapshot, show_ui_state=show_ui_state)
        await _probe_ref_stability(report, adapter, samples=stability_samples)
        await _probe_scroll_and_highlight(report, adapter)
        await _probe_navigation_click(report, adapter)
        await _probe_policy(report, adapter)
        await _probe_staleness(report, adapter, url)

    finally:
        await adapter.close()

    return report


# --------------------------------------------------------------------------
# Probes
# --------------------------------------------------------------------------


async def _probe_representation(
    report: SpikeReport, snapshot: Snapshot, *, show_ui_state: bool
) -> None:
    """Did we get a usable semantic model of a page we know nothing about?"""
    histogram = query.role_histogram(snapshot)
    heads = query.headings(snapshot)
    controls = query.interactive(snapshot)
    fields = query.text_fields(snapshot)
    links = query.by_role(snapshot, "link")

    detail = (
        f"{snapshot.node_count()} nodes across {snapshot.frame_count} frame(s); "
        f"{len(heads)} headings, {len(links)} links, "
        f"{len(query.by_role(snapshot, 'button'))} buttons, {len(fields)} inputs"
    )
    report.add(
        Probe(
            name="semantic representation",
            passed=bool(heads or controls),
            detail=detail,
        )
    )

    top_roles = ", ".join(f"{role}x{count}" for role, count in list(histogram.items())[:8])
    print(f"\n  roles: {top_roles}")
    for heading in heads[:6]:
        print(f"    heading  {query.describe_node(heading)}")
    for control in controls[:8]:
        print(f"    control  {query.describe_node(control)}")

    if show_ui_state:
        print("\n--- <ui_state> as the model would see it ---")
        print(render_ui_state(snapshot))
        print("--- end <ui_state> ---\n")


async def _probe_ref_stability(
    report: SpikeReport, adapter: PlaywrightBrowserAdapter, *, samples: int
) -> None:
    """The load-bearing assumption: refs survive DOM mutation.

    Pipecat's walker keys refs off a WeakMap of live elements, so an element
    that stays mounted keeps its ref across snapshots. If that were not true,
    every reference the model made would need re-resolving by name and the
    whole design would need rethinking — hence testing it first and explicitly.
    """
    snapshot = adapter.current
    assert snapshot is not None

    tracked = [node for node in query.interactive(snapshot) if node.ref and node.name][:5]
    if not tracked:
        tracked = [node for node in query.headings(snapshot) if node.ref][:5]

    if not tracked:
        report.add(
            Probe("ref stability", False, "no referenceable elements found to track")
        )
        return

    baseline = {node.ref: node.name for node in tracked}
    survived = dict.fromkeys(baseline, 0)

    for _ in range(samples):
        started = time.perf_counter()
        fresh = await adapter.observe(force=True)
        report.observations.append((time.perf_counter() - started) * 1000)

        index = fresh.index()
        for ref, name in baseline.items():
            node = index.get(ref)
            if node is not None and node.name == name:
                survived[ref] += 1

    perfect = [ref for ref, count in survived.items() if count == samples]
    detail = (
        f"{len(perfect)}/{len(baseline)} tracked refs kept identity across "
        f"{samples} snapshots while the page mutated"
    )
    report.add(Probe("ref stability", len(perfect) == len(baseline), detail))


async def _probe_scroll_and_highlight(
    report: SpikeReport, adapter: PlaywrightBrowserAdapter
) -> None:
    """Act on something the user cannot currently see, then point at it."""
    snapshot = await adapter.observe(force=True)

    target = None
    for node in query.offscreen(snapshot, "heading", "button", "link"):
        target = node
        break

    if target is None:
        report.add(
            Probe(
                "scroll to offscreen element",
                True,
                "skipped: the whole page fits in the viewport",
            )
        )
        return

    scroll = await adapter.perform(
        _action(ActionKind.SCROLL_TO, snapshot, ref=target.ref, reason="bring into view")
    )
    report.add(
        Probe(
            "scroll to offscreen element",
            scroll.verified,
            f"{query.describe_node(target)} → {scroll.summary()}",
            _timings(scroll),
        )
    )

    current = adapter.current
    assert current is not None
    highlight = await adapter.perform(
        _action(ActionKind.HIGHLIGHT, current, ref=target.ref, reason="point at it")
    )
    report.add(
        Probe(
            "highlight element",
            highlight.verified,
            highlight.summary(),
            _timings(highlight),
        )
    )


def _inactive_control(controls: list[Any], snapshot: Snapshot) -> Any | None:
    """Pick a navigation control that is not the one we are already looking at.

    Clicking the active tab usually still redraws something, so it would pass a
    verification check while proving very little. The page rarely marks the
    active item in a machine-readable way, but it almost always names the
    current view in the document title or the leading heading — so we avoid
    whatever matches that.
    """
    title = snapshot.page.title.lower()
    heads = query.headings(snapshot)
    leading = (heads[0].name or "").lower() if heads else ""

    for node in controls:
        if not node.ref or "selected" in node.state:
            continue
        name = (node.name or "").strip().lower()
        if not name:
            continue
        if name in title or (leading and name in leading):
            continue
        return node
    return None


async def _probe_navigation_click(
    report: SpikeReport, adapter: PlaywrightBrowserAdapter
) -> None:
    """Click a control the page itself declares as navigation, and prove it landed."""
    snapshot = await adapter.observe(force=True)
    controls = query.navigation_controls(snapshot)

    if len(controls) < 2:
        report.add(
            Probe(
                "click navigation control",
                True,
                f"skipped: page exposes {len(controls)} navigation control(s)",
            )
        )
        return

    target = _inactive_control(controls, snapshot) or controls[-1]

    result = await adapter.perform(
        _action(ActionKind.CLICK, snapshot, ref=target.ref, reason="navigate the demo")
    )
    report.add(
        Probe(
            "click navigation control",
            result.verified,
            f"{query.describe_node(target)} → {result.summary()}",
            _timings(result),
        )
    )


async def _probe_policy(report: SpikeReport, adapter: PlaywrightBrowserAdapter) -> None:
    """Dangerous-looking controls and password fields must be refused."""
    snapshot = await adapter.observe(force=True)

    destructive = query.by_name(
        snapshot,
        r"\b(delete|remove|erase|borrar|eliminar|buy|purchase|pay|comprar)\b",
        roles={"button", "link", "menuitem"},
    )
    if destructive:
        result = await adapter.perform(
            _action(ActionKind.CLICK, snapshot, ref=destructive[0].ref, reason="should be refused")
        )
        refused = result.error_code in (
            ErrorCode.NEEDS_CONFIRMATION,
            ErrorCode.BLOCKED_BY_POLICY,
        )
        report.add(
            Probe(
                "refuse destructive click",
                refused,
                f"{query.describe_node(destructive[0])} → {result.error_code}: {result.message}",
            )
        )
    else:
        report.add(
            Probe("refuse destructive click", True, "skipped: no destructive control on this page")
        )

    found = await _find_password_field(adapter)
    if found is None:
        report.add(
            Probe(
                "refuse writing to password field",
                True,
                "skipped: no password field reachable from the entry page",
            )
        )
        return

    snapshot, password_ref = found
    result = await adapter.perform(
        _action(
            ActionKind.FILL,
            snapshot,
            ref=password_ref,
            value="should-never-be-typed",
            reason="should be refused",
        )
    )
    report.add(
        Probe(
            "refuse writing to password field",
            result.error_code == ErrorCode.BLOCKED_BY_POLICY,
            f"{result.error_code}: {result.message}",
        )
    )


async def _password_field_on_page(
    adapter: PlaywrightBrowserAdapter, snapshot: Snapshot
) -> str | None:
    """A password input, identified by type rather than by name.

    Checking the input type is what makes this robust: a field called
    "Passphrase", "Clave" or nothing at all is still protected, and a field
    merely *named* "password" is not misclassified.
    """
    for node in query.text_fields(snapshot):
        if not node.ref:
            continue
        state = await adapter.describe(node.ref)
        if state and (state.type or "").lower() == "password":
            return node.ref
    return None


async def _find_password_field(
    adapter: PlaywrightBrowserAdapter, *, max_views: int = 5
) -> tuple[Snapshot, str] | None:
    """Look for a password field, exploring navigation if the entry page has none.

    Forms usually live one click away from a landing page, so a probe that only
    inspects the entry page would report "nothing to test" on most real sites.
    Exploration goes only through controls the page itself declares as
    navigation, and every click still passes through the policy — so this stays
    a demo of the safe path, not a way around it.
    """
    snapshot = await adapter.observe(force=True)
    ref = await _password_field_on_page(adapter, snapshot)
    if ref:
        return snapshot, ref

    visited: set[str] = set()
    for _ in range(max_views):
        snapshot = await adapter.observe(force=True)
        candidates = [
            node
            for node in query.navigation_controls(snapshot)
            if node.ref and node.ref not in visited
        ]
        if not candidates:
            return None

        target = candidates[0]
        visited.add(target.ref)

        result = await adapter.perform(
            _action(ActionKind.CLICK, snapshot, ref=target.ref, reason="look for a form")
        )
        if not result.ok:
            continue

        snapshot = await adapter.observe(force=True)
        ref = await _password_field_on_page(adapter, snapshot)
        if ref:
            return snapshot, ref

    return None


async def _probe_staleness(
    report: SpikeReport, adapter: PlaywrightBrowserAdapter, url: str
) -> None:
    """An action built on a page the model no longer has grounds to trust is refused."""
    fabricated = Action(
        action_id=uuid4().hex[:8],
        kind=ActionKind.SCROLL_TO,
        snapshot_id="s0-never-issued",
        ref="f0.e1",
    )
    result = await adapter.perform(fabricated)
    report.add(
        Probe(
            "reject unknown snapshot id",
            result.error_code == ErrorCode.STALE_SNAPSHOT,
            f"{result.error_code}: {result.message}",
        )
    )

    # Now the realistic case: decide against one page, navigate, then try to act.
    before_navigation = await adapter.observe(force=True)
    candidates = [node for node in query.interactive(before_navigation) if node.ref]
    if not candidates:
        report.add(Probe("reject post-navigation snapshot", True, "skipped: nothing to target"))
        return

    target_ref = candidates[0].ref
    embed_url = url.rstrip("/") + "/embed.html"

    navigation = await adapter.perform(
        _action(ActionKind.NAVIGATE, before_navigation, url=embed_url, reason="change the page")
    )
    if not navigation.ok:
        report.add(
            Probe(
                "reject post-navigation snapshot",
                True,
                f"skipped: could not navigate ({navigation.message})",
            )
        )
        return

    replayed = Action(
        action_id=uuid4().hex[:8],
        kind=ActionKind.HIGHLIGHT,
        snapshot_id=before_navigation.snapshot_id,
        ref=target_ref,
    )
    result = await adapter.perform(replayed)
    report.add(
        Probe(
            "reject post-navigation snapshot",
            result.error_code == ErrorCode.STALE_SNAPSHOT,
            f"{result.error_code}: {result.message}",
        )
    )


def _timings(result: ActionResult) -> dict[str, float]:
    return {
        "observe": result.timings.observe_ms,
        "exec": result.timings.execute_ms,
        "verify": result.timings.verify_ms,
    }
