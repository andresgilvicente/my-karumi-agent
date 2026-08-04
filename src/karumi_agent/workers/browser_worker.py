"""The browser as a peer on Pipecat's worker bus.

This is the piece that makes the whole design work without a browser client.

Pipecat's `UIWorker` normally gets its accessibility snapshots from the official
React client over RTVI. But `UIWorker.on_bus_message` stores a snapshot whenever
a `BusUIEventMessage` named `__ui_snapshot` arrives on the bus — *regardless of
where it came from* — and `BaseWorker.send_bus_message` is public API. So a
plain worker that drives a real browser can publish snapshots under that name
and the UI worker is none the wiser. The entire official protocol is reused,
with a browser we control standing in for the client we don't have.

Actions take the other route. `UIWorker.send_command` publishes a fire-and-forget
`BusUICommandMessage`, which is fine for a client that owns its own DOM but
useless here: a tool that cannot await the outcome cannot know whether to tell
the user it worked. So actions arrive as **jobs** instead, and the response
carries the verified `ActionResult`. Nothing reports success without evidence.
"""

from __future__ import annotations

from typing import Any

from karumi_agent.browser.adapter import BrowserAdapter
from karumi_agent.domain.models import Action, ActionResult, ErrorCode, Snapshot
from karumi_agent.observability.logging import get_logger
from karumi_agent.workers._pipecat import (
    UI_SNAPSHOT_EVENT,
    BaseWorker,
    BusUIEventMessage,
    JobStatus,
    job,
)

log = get_logger(__name__)

#: Job names this worker answers to.
JOB_OBSERVE = "observe"
JOB_ACT = "act"


class BrowserWorker(BaseWorker):
    """Owns the browser; publishes what it sees and executes what it is asked.

    Deliberately not an LLM worker. It holds no model, no prompt and no
    conversational state — it observes, acts, verifies, and reports. Keeping
    the reasoning out of here is what lets the interface model be swapped, or
    run at a different size, without touching the code that touches the page.
    """

    def __init__(self, name: str, adapter: BrowserAdapter, *, url: str) -> None:
        super().__init__(name=name)
        self._adapter = adapter
        self._url = url
        self._started = False
        self._latest: Snapshot | None = None

    @property
    def adapter(self) -> BrowserAdapter:
        return self._adapter

    @property
    def latest(self) -> Snapshot | None:
        """The most recent snapshot this worker published."""
        return self._latest

    # -- lifecycle ---------------------------------------------------------

    async def open(self) -> Snapshot:
        """Launch the browser and start streaming snapshots onto the bus."""
        if self._started:
            snapshot = await self._adapter.observe(force=True)
            return snapshot

        snapshot = await self._adapter.start(self._url)
        self._started = True

        # Every subsequent snapshot the page pushes goes straight onto the bus,
        # so the UI worker's view of the screen keeps up with the page without
        # anyone having to ask for it.
        self._adapter.subscribe(self._on_snapshot)
        await self.publish_snapshot(snapshot)

        log.info("browser_worker.open", url=self._url, snapshot_id=snapshot.snapshot_id)
        return snapshot

    async def close(self) -> None:
        if self._started:
            await self._adapter.close()
            self._started = False

    def _on_snapshot(self, snapshot: Snapshot) -> None:
        """Adapter callback. Snapshots arrive from a sync context, so hand off."""
        self.create_task(
            self.publish_snapshot(snapshot), f"{self.name}::publish_snapshot"
        )

    async def publish_snapshot(self, snapshot: Snapshot) -> None:
        """Put a snapshot on the bus in the shape `UIWorker` expects.

        The payload is the A11ySnapshot wire shape — `root`, `captured_at` and
        an optional `selection`. `snapshot_id` rides along too: Pipecat ignores
        the extra key when rendering `<ui_state>`, while our own UI worker uses
        it to stamp actions with the exact page state the model was looking at.
        """
        self._latest = snapshot
        await self.send_bus_message(
            BusUIEventMessage(
                source=self.name,
                target=None,
                event_name=UI_SNAPSHOT_EVENT,
                payload=snapshot.model_dump(mode="json"),
            )
        )

    # -- jobs --------------------------------------------------------------

    @job(name=JOB_OBSERVE)
    async def _observe_job(self, message: Any) -> None:
        """Re-walk the page and publish. Used when a caller needs certainty."""
        snapshot = await self._adapter.observe(force=True)
        await self.publish_snapshot(snapshot)
        await self.send_job_response(
            message.job_id,
            {
                "snapshot_id": snapshot.snapshot_id,
                "url": snapshot.page.url,
                "title": snapshot.page.title,
                "nodes": snapshot.node_count(),
            },
        )

    @job(name=JOB_ACT, sequential=True)
    async def _act_job(self, message: Any) -> None:
        """Execute one action and answer with its verified result.

        Sequential on purpose: two clicks racing on the same page would make
        the before/after diff meaningless, and with it any claim about what
        happened.
        """
        payload = message.payload or {}
        try:
            action = Action.model_validate(payload)
        except Exception as exc:  # malformed tool call
            await self.send_job_response(
                message.job_id,
                _failure(payload, ErrorCode.INVALID_ACTION, str(exc)),
                status=JobStatus.COMPLETED,
            )
            return

        result = await self._adapter.perform(action)

        # A successful action changes the page, so publish immediately rather
        # than waiting for the debounced stream — otherwise the model's next
        # turn reasons about the page as it was before its own action.
        current = self._adapter.current
        if current is not None:
            await self.publish_snapshot(current)

        await self.send_job_response(message.job_id, _result_payload(result))


def _result_payload(result: ActionResult) -> dict[str, Any]:
    """Shrink an ActionResult to what a model actually needs to decide.

    Full evidence goes to the structured log, not into the context window: the
    model needs to know whether it happened and what changed, not every rect and
    attribute we compared to find out.
    """
    evidence = result.evidence
    return {
        "action_id": result.action_id,
        "kind": str(result.kind),
        "ok": result.ok,
        "verified": result.verified,
        "error_code": str(result.error_code) if result.error_code else None,
        "summary": result.summary(),
        "changes": evidence.changes if evidence else [],
        "url": evidence.page_after.url if evidence else None,
        "title": evidence.page_after.title if evidence else None,
        "snapshot_id": evidence.snapshot_id_after if evidence else None,
        "timings_ms": {
            "observe": round(result.timings.observe_ms, 1),
            "execute": round(result.timings.execute_ms, 1),
            "verify": round(result.timings.verify_ms, 1),
        },
    }


def _failure(payload: dict[str, Any], code: ErrorCode, message: str) -> dict[str, Any]:
    return {
        "action_id": payload.get("action_id", ""),
        "kind": payload.get("kind", ""),
        "ok": False,
        "verified": False,
        "error_code": str(code),
        "summary": f"{code}: {message}",
        "changes": [],
    }
