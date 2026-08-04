"""Turning what the operator said into a verified action and something to say.

This is the hot path, and it is deliberately **one LLM inference per turn**. The
textbook Pipecat arrangement (voice model decides, delegates to a UI worker,
which runs a second inference, whose answer goes back to the voice model) costs
two inferences. At the ~15 tok/s this machine manages, that is four to five
seconds per turn -- not a conversation. So the planner sees both the transcript
and the page, decides once, and speaks its own answer.

The model's output is never trusted on its face. It arrives schema-constrained,
then every field is re-validated against the live snapshot before anything
happens, and the resulting action is verified after the fact.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from karumi_agent.agent import prompts
from karumi_agent.agent.schemas import NO_REF, build_action_schema, build_demo_beat_schema
from karumi_agent.browser.executor import ActionExecutor
from karumi_agent.browser.snapshotter import Snapshotter
from karumi_agent.config import Settings
from karumi_agent.domain.actions import ActionKind, ActionRequest, ActionResult, ActionStatus
from karumi_agent.domain.demo import EditorialConfig
from karumi_agent.domain.safety import Sensitivity, classify_label
from karumi_agent.domain.snapshot import PageSnapshot
from karumi_agent.errors import LLMError
from karumi_agent.llm.ollama import LLMTiming, OllamaClient
from karumi_agent.logging import get_logger

log = get_logger("planner")

#: Emitted while the model is still deciding, so the operator is never met with
#: silence. Spoken only when the model is slow, and never asserts an outcome.
THINKING_FILLER = "Let me look."


@dataclass(slots=True)
class Turn:
    """Everything that happened in one exchange."""

    utterance: str
    say: str = ""
    action: ActionRequest | None = None
    result: ActionResult | None = None
    timing: LLMTiming = field(default_factory=LLMTiming)
    snapshot_id: str | None = None
    started_at: float = field(default_factory=time.time)
    total_s: float = 0.0
    #: Set when the model asked for something the page could not support.
    repaired: str | None = None

    @property
    def verified(self) -> bool:
        return self.result.verified if self.result else True

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "utterance": self.utterance[:200],
            "say": self.say[:300],
            "snapshot_id": self.snapshot_id,
            "total_s": round(self.total_s, 3),
            "llm": self.timing.as_dict(),
        }
        if self.action is not None:
            payload["action"] = self.action.as_dict()
        if self.result is not None:
            payload["result"] = self.result.as_dict()
        if self.repaired:
            payload["repaired"] = self.repaired
        return payload


class Planner:
    """Decides what to say and what to do, then makes it happen."""

    def __init__(
        self,
        *,
        settings: Settings,
        llm: OllamaClient,
        snapshotter: Snapshotter,
        executor: ActionExecutor,
        editorial: EditorialConfig | None = None,
    ) -> None:
        self._settings = settings
        self._llm = llm
        self._snapshotter = snapshotter
        self._executor = executor
        self._editorial = editorial
        self._covered: list[str] = []
        self._pending_confirmation: ActionRequest | None = None
        self._prewarm_task: asyncio.Task[None] | None = None

    @property
    def pending_confirmation(self) -> ActionRequest | None:
        return self._pending_confirmation

    async def warmup(self) -> float:
        """Pre-load the model *with the real system prefix*.

        Warming with a throwaway prompt loads the weights but leaves the rules
        and few-shot examples uncached, so the first real turn still pays for
        them. Measured here: 6.6 s to first token on turn one, versus 0.6 s once
        the actual prefix is warm.
        """
        return await self._llm.warmup(
            self._settings.llm.effective_ui_model, system=prompts.UI_AGENT_SYSTEM
        )

    async def prewarm_page(self) -> None:
        """Prefill the current page state into the KV cache, off the hot path.

        After a navigation the page state is new, so the next real turn would pay
        cold prefill for it -- measured at roughly seven seconds for a
        400-token snapshot on this machine. Issuing one throwaway request with
        the new state moves that cost to a moment when nobody is waiting, and the
        operator's next question resumes the cached prefix instead.

        Failures are swallowed: this is an optimisation, never a dependency.
        """
        ui_state = self._snapshotter.render_current()
        if not ui_state:
            return
        user = prompts.build_user_prompt(
            ui_state=ui_state,
            utterance="",
            editorial=self._editorial,
            covered=self._covered,
        )
        try:
            await self._llm.warmup_prefix(
                self._settings.llm.effective_ui_model,
                system=prompts.UI_AGENT_SYSTEM,
                user=user,
            )
        except LLMError as exc:  # pragma: no cover - best effort
            log.debug(f"page prewarm skipped: {exc}")

    # ------------------------------------------------------------------ main
    async def handle(
        self,
        utterance: str,
        *,
        on_speech: Callable[[str], Any] | None = None,
        autonomous: bool = False,
    ) -> Turn:
        """Handle one operator turn end to end.

        ``on_speech`` receives spoken text incrementally as the model decodes it,
        so synthesis can start before the decision is complete.
        """
        turn = Turn(utterance=utterance)
        started = time.perf_counter()

        if self._pending_confirmation is not None:
            confirmed = self._resolve_confirmation(utterance)
            if confirmed is not None:
                turn.action = confirmed
                turn.result = await self._executor.execute(confirmed)
                turn.say = self._narrate(turn.result, "")
                if on_speech is not None:
                    await _maybe_await(on_speech(turn.say))
                turn.total_s = time.perf_counter() - started
                return turn

        snapshot = self._snapshotter.store.current
        turn.snapshot_id = snapshot.snapshot_id if snapshot else None

        schema = (
            build_demo_beat_schema(snapshot)
            if autonomous
            else build_action_schema(snapshot, utterance=utterance)
        )
        system = prompts.AUTONOMOUS_SYSTEM if autonomous else prompts.UI_AGENT_SYSTEM
        user = prompts.build_user_prompt(
            ui_state=self._snapshotter.render_current(),
            utterance=utterance,
            editorial=self._editorial,
            covered=self._covered,
        )

        spoken: list[str] = []
        response = None
        try:
            async for delta, final in self._llm.stream_structured(
                model=self._settings.llm.effective_ui_model,
                system=system,
                user=user,
                schema=schema,
                stream_field="say",
            ):
                if delta and on_speech is not None:
                    spoken.append(delta)
                    await _maybe_await(on_speech(delta))
                elif delta:
                    spoken.append(delta)
                if final is not None:
                    response = final
        except LLMError as exc:
            log.warning("planner llm failed", error=str(exc))
            turn.say = "Sorry, I lost my train of thought there. Could you say that again?"
            if on_speech is not None:
                await _maybe_await(on_speech(turn.say))
            turn.total_s = time.perf_counter() - started
            return turn

        if response is None:
            turn.say = prompts.CANNOT_SEE
            turn.total_s = time.perf_counter() - started
            return turn

        turn.timing = response.timing
        turn.say = str(response.data.get("say") or "").strip() or "".join(spoken)

        request, repaired = self._to_request(response.data, snapshot, utterance)
        turn.repaired = repaired

        if request is not None:
            turn.action = request
            turn.result = await self._executor.execute(request)
            if turn.result.status is ActionStatus.NEEDS_CONFIRMATION:
                self._pending_confirmation = request
            follow_up = self._narrate(turn.result, turn.say)
            if follow_up and follow_up != turn.say:
                # Only spoken when the outcome differs from what we already said,
                # so the agent corrects itself rather than papering over a failure.
                if on_speech is not None:
                    await _maybe_await(on_speech(" " + follow_up))
                turn.say = f"{turn.say} {follow_up}".strip()

        self._remember(turn)
        turn.total_s = time.perf_counter() - started

        # If the page moved, start prefilling the new state now rather than
        # making the operator's next question pay for it.
        if turn.result is not None and turn.result.result_snapshot_id != turn.snapshot_id:
            self._spawn_prewarm()

        log.info("turn complete", **turn.as_dict())
        return turn

    def _spawn_prewarm(self) -> None:
        """Warm the new page state in the background.

        Fire and forget on purpose. Ollama serialises requests, so if the
        operator speaks immediately their turn simply queues behind the same
        prefill it would have done itself -- the work is moved, not duplicated.
        """
        if self._prewarm_task is not None and not self._prewarm_task.done():
            self._prewarm_task.cancel()
        self._prewarm_task = asyncio.create_task(self.prewarm_page())

    async def aclose(self) -> None:
        """Cancel any background work."""
        if self._prewarm_task is not None and not self._prewarm_task.done():
            self._prewarm_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._prewarm_task

    # ------------------------------------------------------------ validation
    def _to_request(
        self,
        data: dict[str, Any],
        snapshot: PageSnapshot | None,
        utterance: str,
    ) -> tuple[ActionRequest | None, str | None]:
        """Turn the model's object into an action, repairing what we safely can.

        The schema already guarantees the shape and that any ref exists in the
        snapshot. This is the second gate: it catches the combinations the schema
        cannot express, such as a ref-taking action arriving with ``none``.
        """
        raw_action = str(data.get("action") or ActionKind.ANSWER.value)
        raw_ref = str(data.get("ref") or NO_REF)

        try:
            kind = ActionKind(raw_action)
        except ValueError:
            return None, f"unknown action {raw_action!r}, spoke instead"

        if kind is ActionKind.ANSWER:
            return None, None

        ref: str | None = None if raw_ref == NO_REF else raw_ref

        if kind.needs_ref:
            if ref is None:
                return None, f"{kind.value} needs an element but none was chosen"
            if snapshot is None or snapshot.resolve(ref) is None:
                # Should be impossible given the enum, but a wrong ref must never
                # become a click on whatever happens to match.
                return None, f"ref {ref!r} is not on the page"

        node = snapshot.resolve(ref) if (snapshot and ref) else None

        # "navigate" against a link is really a click on that link; sending the
        # href through goto() would lose SPA routing and reload the whole app.
        if kind is ActionKind.NAVIGATE and node is not None and node.url:
            kind = ActionKind.CLICK

        # Classify the operator's own words, not just the chosen target. Observed
        # failure: asked to "click the delete workspace button" on a page where
        # that button was not present, the model picked an unrelated element and
        # narrated "opening Delete workspace". The target looked harmless, so
        # label-based classification passed it. Intent has to travel with the
        # request.
        intent = classify_label(utterance)
        force = kind.mutates_page and intent.sensitivity is not Sensitivity.SAFE

        return (
            ActionRequest(
                kind=kind,
                ref=ref,
                say=str(data.get("say") or ""),
                target_label=node.label if node else None,
                source_snapshot_id=snapshot.snapshot_id if snapshot else "",
                force_confirmation=force,
                confirmation_reason=intent.reason if force else None,
            ),
            None,
        )

    def _resolve_confirmation(self, utterance: str) -> ActionRequest | None:
        """Interpret a yes/no answer to a pending confirmation."""
        text = utterance.strip().lower()
        pending = self._pending_confirmation
        if pending is None:
            return None

        affirmative = any(
            word in text
            for word in ("yes", "yeah", "yep", "go ahead", "do it", "confirm", "please do")
        )
        negative = any(word in text for word in ("no", "don't", "do not", "stop", "cancel", "nope"))

        if affirmative and not negative:
            self._pending_confirmation = None
            self._executor.confirm(pending.action_id)
            return pending
        if negative:
            self._pending_confirmation = None
            return None
        # Anything else is a new request, not an answer; drop the pending action
        # rather than leaving it armed.
        self._pending_confirmation = None
        return None

    # ------------------------------------------------------------- narration
    def _narrate(self, result: ActionResult, already_said: str) -> str:
        """What to add out loud once we know what actually happened.

        Returns "" when the action did what it said on the tin: repeating
        yourself is worse than saying nothing. Anything else gets an honest line.
        """
        hint = result.narration_hint()
        if not hint:
            return ""
        if hint.lower() in already_said.lower():
            return ""
        return hint

    def _remember(self, turn: Turn) -> None:
        """Track what has been covered, so the tour does not loop."""
        if turn.action is not None and turn.action.target_label:
            label = turn.action.target_label
            if label not in self._covered:
                self._covered.append(label)


async def _maybe_await(value: Any) -> None:
    """Allow ``on_speech`` to be either sync or async."""
    if hasattr(value, "__await__"):
        await value
