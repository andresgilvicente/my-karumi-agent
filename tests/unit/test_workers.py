"""Worker logic that does not need a browser or an LLM.

Covers the two things we own in the worker layer: how an `ActionResult` is
shrunk before it reaches a model's context, and how the UI worker decides which
page state an action belongs to.

The UI worker is exercised without running Pipecat's constructor. That is
deliberate: instantiating it would build an LLM pipeline and prove only that
Pipecat works. What needs testing is our logic — that an action is stamped with
the snapshot the model was actually shown, and that acting before seeing
anything is refused.
"""

from __future__ import annotations

from typing import Any

import pytest

from karumi_agent.domain.models import (
    ActionKind,
    ActionResult,
    ElementState,
    ErrorCode,
    Evidence,
    PageInfo,
    Timings,
)
from karumi_agent.workers._pipecat import UI_SNAPSHOT_EVENT, BusUIEventMessage
from karumi_agent.workers.browser_worker import _failure, _result_payload
from karumi_agent.workers.ui_worker import KarumiUIWorker


def evidence(**kwargs: Any) -> Evidence:
    base: dict[str, Any] = {
        "page_before": PageInfo(url="https://x.test/a", title="A"),
        "page_after": PageInfo(url="https://x.test/b", title="B"),
        "changes": ["url https://x.test/a → https://x.test/b"],
        "snapshot_id_after": "s9",
    }
    base.update(kwargs)
    return Evidence.model_validate(base)


class TestResultPayload:
    def test_carries_the_verdict_and_the_observed_changes(self) -> None:
        payload = _result_payload(
            ActionResult(
                action_id="a1",
                kind=ActionKind.CLICK,
                ok=True,
                verified=True,
                message="now at https://x.test/b",
                evidence=evidence(),
                timings=Timings(observe_ms=1.24, execute_ms=50.6, verify_ms=370.9),
            )
        )

        assert payload["verified"] is True
        assert payload["changes"] == ["url https://x.test/a → https://x.test/b"]
        assert payload["url"] == "https://x.test/b"
        assert payload["snapshot_id"] == "s9"
        assert payload["timings_ms"]["verify"] == 370.9

    def test_unverified_result_says_so_in_words_the_model_will_repeat(self) -> None:
        """The model only knows an action failed if the payload tells it."""
        payload = _result_payload(
            ActionResult(
                action_id="a2",
                kind=ActionKind.CLICK,
                ok=True,
                verified=False,
                error_code=ErrorCode.NO_CHANGE_DETECTED,
                message="the click produced no observable change",
                evidence=evidence(changes=[]),
            )
        )

        assert payload["verified"] is False
        assert "Do not tell the user it worked" in payload["summary"]

    def test_full_evidence_stays_out_of_the_context_window(self) -> None:
        """Rects and attributes belong in the log, not in the model's context."""
        payload = _result_payload(
            ActionResult(
                action_id="a3",
                kind=ActionKind.SCROLL_TO,
                ok=True,
                verified=True,
                evidence=evidence(
                    element_after=ElementState(
                        ref="f0.e1", rect={"x": 0, "y": 0, "width": 100, "height": 20}
                    )
                ),
            )
        )

        assert "evidence" not in payload
        assert "element_after" not in payload
        assert "rect" not in str(payload)

    def test_failure_payload_shape_matches_the_success_one(self) -> None:
        """One shape means the model never has to branch on which it got."""
        failure = _failure({"action_id": "a4", "kind": "click"}, ErrorCode.INVALID_ACTION, "bad")
        success = _result_payload(
            ActionResult(action_id="a4", kind=ActionKind.CLICK, ok=True, verified=True)
        )

        assert set(failure) <= set(success)
        assert failure["ok"] is False


class StubUIWorker(KarumiUIWorker):
    """The UI worker's own logic, without Pipecat's constructor.

    `UIWorker.__init__` builds an LLM pipeline; running it here would test
    Pipecat rather than us. Only the attributes our methods touch are set.
    """

    def __init__(self) -> None:  # deliberately does not call super()
        self._browser_worker = "browser"
        self._active_snapshot_id = None
        self._action_timeout = 5.0
        self.requested: list[dict[str, Any]] = []

    async def _super_on_bus_message(self, message: Any) -> None:
        return None


@pytest.fixture
def worker(monkeypatch: pytest.MonkeyPatch) -> StubUIWorker:
    # Neutralise the base class hop; we are testing the override, not Pipecat.
    monkeypatch.setattr(
        "karumi_agent.workers.ui_worker.UIWorker.on_bus_message",
        StubUIWorker._super_on_bus_message,
        raising=False,
    )
    return StubUIWorker()


class TestSnapshotTracking:
    async def test_captures_the_id_of_the_page_the_model_was_shown(
        self, worker: StubUIWorker
    ) -> None:
        await worker.on_bus_message(
            BusUIEventMessage(
                source="browser",
                target=None,
                event_name=UI_SNAPSHOT_EVENT,
                payload={"snapshot_id": "s42", "root": {"ref": "", "role": "generic"}},
            )
        )

        assert worker.active_snapshot_id == "s42"

    async def test_ignores_other_bus_traffic(self, worker: StubUIWorker) -> None:
        await worker.on_bus_message(
            BusUIEventMessage(
                source="browser", target=None, event_name="nav_click", payload={"view": "x"}
            )
        )

        assert worker.active_snapshot_id is None

    async def test_later_snapshots_supersede_earlier_ones(
        self, worker: StubUIWorker
    ) -> None:
        for snapshot_id in ("s1", "s2", "s3"):
            await worker.on_bus_message(
                BusUIEventMessage(
                    source="browser",
                    target=None,
                    event_name=UI_SNAPSHOT_EVENT,
                    payload={"snapshot_id": snapshot_id},
                )
            )

        assert worker.active_snapshot_id == "s3"


class TestActing:
    async def test_refuses_to_act_before_the_page_has_been_seen(
        self, worker: StubUIWorker
    ) -> None:
        """No snapshot means no grounds for an action — and no browser call."""
        result = await worker._act(ActionKind.CLICK, ref="f0.e1")

        assert result["ok"] is False
        assert result["error_code"] == "STALE_SNAPSHOT"

    async def test_stamps_the_action_with_the_snapshot_the_model_saw(
        self, worker: StubUIWorker, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The worker supplies the id, never the model. A model-supplied id adds
        a failure mode with no upside: it knows nothing about freshness that we
        do not already know."""
        worker._active_snapshot_id = "s7"
        sent: dict[str, Any] = {}

        class FakeJob:
            def __init__(self, payload: dict[str, Any]) -> None:
                sent.update(payload)
                self.response = {"ok": True, "verified": True, "summary": "done"}

            async def __aenter__(self) -> FakeJob:
                return self

            async def __aexit__(self, *exc: object) -> None:
                return None

        monkeypatch.setattr(
            StubUIWorker,
            "job",
            lambda self, worker_name, *, name, payload, timeout: FakeJob(payload),
            raising=False,
        )

        result = await worker._act(ActionKind.HIGHLIGHT, ref="f0.e5")

        assert sent["snapshot_id"] == "s7"
        assert sent["kind"] == "highlight"
        assert sent["ref"] == "f0.e5"
        assert sent["action_id"]
        assert result["verified"] is True
