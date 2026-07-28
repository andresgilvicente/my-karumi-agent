import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ui_control.catalog import UIControlCatalog


GUIDED_DEMO_PATH = Path(__file__).with_name("guided_demo.json")
DEMO_INTENTS = {
    "start_demo",
    "continue_demo",
    "pause_demo",
    "resume_demo",
    "stop_demo",
    "restart_demo",
}


class GuidedDemoState(StrEnum):
    IDLE = "idle"
    OFFERED = "offered"
    RUNNING = "running"
    WAITING_FOR_USER = "waiting_for_user"
    ANSWERING_QUESTION = "answering_question"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True)
class GuidedDemoStep:
    id: str
    command: str
    payload: dict
    transition_message: str
    explanation: str


@dataclass(frozen=True)
class GuidedDemoDefinition:
    offer_delay_secs: float
    user_intervention_window_secs: float
    transcript_grace_secs: float
    introduction: str
    auto_start_message: str
    pause_message: str
    stop_message: str
    failure_message: str
    completion_message: str
    intents: dict[str, tuple[str, ...]]
    steps: tuple[GuidedDemoStep, ...]


def load_guided_demo(
    catalog: UIControlCatalog,
    path: Path = GUIDED_DEMO_PATH,
) -> GuidedDemoDefinition:
    with path.open(encoding="utf-8") as definition_file:
        data = json.load(definition_file)
    validate_guided_demo(data, catalog)
    return GuidedDemoDefinition(
        offer_delay_secs=float(data["offerDelaySecs"]),
        user_intervention_window_secs=float(data["userInterventionWindowSecs"]),
        transcript_grace_secs=float(data["transcriptGraceSecs"]),
        introduction=data["introduction"],
        auto_start_message=data["autoStartMessage"],
        pause_message=data["pauseMessage"],
        stop_message=data["stopMessage"],
        failure_message=data["failureMessage"],
        completion_message=data["completionMessage"],
        intents={intent: tuple(phrases) for intent, phrases in data["intents"].items()},
        steps=tuple(
            GuidedDemoStep(
                id=step["id"],
                command=step["action"]["command"],
                payload=step["action"]["payload"],
                transition_message=step["transitionMessage"],
                explanation=step["explanation"],
            )
            for step in data["steps"]
        ),
    )


def validate_guided_demo(data: dict, catalog: UIControlCatalog) -> None:
    for delay in ("offerDelaySecs", "userInterventionWindowSecs", "transcriptGraceSecs"):
        if not isinstance(data.get(delay), (int, float)) or data[delay] <= 0:
            raise ValueError(f"{delay} must be a positive number")

    for message in (
        "introduction",
        "autoStartMessage",
        "pauseMessage",
        "stopMessage",
        "failureMessage",
        "completionMessage",
    ):
        if not isinstance(data.get(message), str) or not data[message].strip():
            raise ValueError(f"{message} must be a non-empty string")

    intents = data.get("intents")
    if not isinstance(intents, dict) or set(intents) != DEMO_INTENTS:
        raise ValueError("guided demo must define the complete closed intent set")
    normalized_phrases: set[str] = set()
    for intent, phrases in intents.items():
        if not isinstance(phrases, list) or not phrases:
            raise ValueError(f"{intent} must define at least one phrase")
        for phrase in phrases:
            normalized = _normalize_phrase(phrase) if isinstance(phrase, str) else ""
            if not normalized or normalized in normalized_phrases:
                raise ValueError("guided demo intent phrases must be non-empty and unique")
            normalized_phrases.add(normalized)

    steps = data.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError("guided demo must define at least one step")
    step_ids: set[str] = set()
    step_views: set[str] = set()
    for step in steps:
        if not isinstance(step, dict) or not isinstance(step.get("id"), str):
            raise ValueError("each guided demo step must define an id")
        if step["id"] in step_ids:
            raise ValueError("guided demo step ids must be unique")
        step_ids.add(step["id"])
        action = step.get("action")
        if not isinstance(action, dict) or action.get("command") != "navigate_to":
            raise ValueError("guided demo steps must use navigate_to")
        payload = action.get("payload")
        view = payload.get("view") if isinstance(payload, dict) else None
        if view not in catalog.views or view in step_views:
            raise ValueError("guided demo steps must reference unique supported views")
        step_views.add(view)
        for message in ("transitionMessage", "explanation"):
            if not isinstance(step.get(message), str) or not step[message].strip():
                raise ValueError(f"guided demo step {step['id']} must define {message}")


def resolve_demo_intent(text: str, definition: GuidedDemoDefinition) -> str | None:
    current = _normalize_phrase(text)
    for intent, phrases in definition.intents.items():
        if current in {_normalize_phrase(phrase) for phrase in phrases}:
            return intent
    return None


class GuidedDemoController:
    """Own the confirmed, interruptible guided-demo sequence for one client session."""

    def __init__(
        self,
        definition: GuidedDemoDefinition,
        execute_action: Callable[[str, dict], Awaitable[dict]],
        emit_speech: Callable[[str, str | None], Awaitable[None]],
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.definition = definition
        self.state = GuidedDemoState.IDLE
        self.session_id: str | None = None
        self.current_step_index: int | None = None
        self.last_confirmed_step: str | None = None
        self.next_step_index = 0
        self.action_in_progress = False
        self.last_user_activity: float | None = None
        self._execute_action = execute_action
        self._emit_speech = emit_speech
        self._sleep = sleep
        self._connected = False
        self._ui_ready = False
        self._generation = 0
        self._timer_task: asyncio.Task | None = None
        self._action_task: asyncio.Task | None = None
        self._speech_purpose: tuple[int, str, int | None] | None = None
        self._speech_started = False
        self._user_speaking = False
        self._awaiting_answer = False
        self._answer_started = False
        self._explicitly_paused = False
        self._pause_after_action = False
        self._answer_finished_while_action = False
        self._deferred_failure = False
        self._interrupted_speech: tuple[str, int | None] | None = None
        self._pending_explanation_step: int | None = None

    @property
    def pending_auto_advance(self) -> bool:
        return self._timer_task is not None and not self._timer_task.done()

    async def connect(self, session_id: str) -> None:
        self._invalidate_run()
        self.session_id = session_id
        self._connected = True
        self._ui_ready = False
        self.state = GuidedDemoState.IDLE
        self.current_step_index = None
        self.last_confirmed_step = None
        self.next_step_index = 0

    async def set_ui_ready(self, ready: bool) -> None:
        self._ui_ready = ready
        if ready and self._connected and self.state == GuidedDemoState.IDLE:
            self.state = GuidedDemoState.OFFERED
            await self._speak(self.definition.introduction, "introduction")

    async def disconnect(self) -> None:
        self._connected = False
        self._ui_ready = False
        self._invalidate_run()
        self.session_id = None
        self.state = GuidedDemoState.IDLE

    def on_user_started_speaking(self) -> None:
        if not self._connected or self._user_speaking:
            return
        self._user_speaking = True
        self.last_user_activity = time.monotonic()
        self._cancel_timer()
        self._awaiting_answer = False
        self._answer_started = False
        if self._speech_purpose:
            _, purpose, step_index = self._speech_purpose
            self._interrupted_speech = (purpose, step_index)
            self._speech_purpose = None
            self._speech_started = False
        if self.action_in_progress:
            self._pause_after_action = True
        if self.state in {
            GuidedDemoState.RUNNING,
            GuidedDemoState.WAITING_FOR_USER,
            GuidedDemoState.ANSWERING_QUESTION,
        }:
            self.state = GuidedDemoState.ANSWERING_QUESTION

    def on_normal_transcription(self) -> None:
        self._user_speaking = False
        self._interrupted_speech = None
        if self.state != GuidedDemoState.PAUSED:
            self._pending_explanation_step = None
        self.last_user_activity = time.monotonic()
        if self.state == GuidedDemoState.FAILED and self._deferred_failure:
            self._awaiting_answer = True
            self._answer_started = False
            return
        if self.state in {
            GuidedDemoState.IDLE,
            GuidedDemoState.COMPLETED,
            GuidedDemoState.CANCELLED,
            GuidedDemoState.FAILED,
        }:
            return
        self._awaiting_answer = True
        self._answer_started = False
        if self.state not in {GuidedDemoState.OFFERED, GuidedDemoState.PAUSED}:
            self.state = GuidedDemoState.ANSWERING_QUESTION

    async def handle_control(self, intent: str, user_text: str) -> bool:
        self._user_speaking = False
        self.last_user_activity = time.monotonic()
        self._awaiting_answer = False
        self._answer_started = False
        self._interrupted_speech = None

        if intent in {"start_demo", "continue_demo", "resume_demo", "restart_demo"} and (
            not self._connected or not self._ui_ready
        ):
            return False

        if intent == "start_demo":
            if self.state in {
                GuidedDemoState.OFFERED,
                GuidedDemoState.IDLE,
                GuidedDemoState.COMPLETED,
                GuidedDemoState.CANCELLED,
                GuidedDemoState.FAILED,
            }:
                await self._start_demo(user_text)
            else:
                await self._continue_demo(user_text)
            return True
        if intent == "restart_demo":
            await self._start_demo(user_text)
            return True
        if intent == "continue_demo":
            if self.state in {
                GuidedDemoState.OFFERED,
                GuidedDemoState.RUNNING,
                GuidedDemoState.WAITING_FOR_USER,
                GuidedDemoState.ANSWERING_QUESTION,
                GuidedDemoState.PAUSED,
            }:
                await self._continue_demo(user_text)
                return True
            return False
        if intent == "resume_demo":
            if self.state == GuidedDemoState.PAUSED:
                self._explicitly_paused = False
                await self._continue_demo(user_text)
                return True
            return False
        if intent == "pause_demo":
            if self.state in {
                GuidedDemoState.OFFERED,
                GuidedDemoState.RUNNING,
                GuidedDemoState.WAITING_FOR_USER,
                GuidedDemoState.ANSWERING_QUESTION,
            }:
                self._cancel_timer()
                self._explicitly_paused = True
                self.state = GuidedDemoState.PAUSED
                await self._speak(self.definition.pause_message, "control_ack", user_text=user_text)
                return True
            return False
        if intent == "stop_demo":
            if self.state not in {GuidedDemoState.IDLE, GuidedDemoState.CANCELLED}:
                self._invalidate_run()
                self.state = GuidedDemoState.CANCELLED
                await self._speak(self.definition.stop_message, "control_ack", user_text=user_text)
                return True
            return False
        return False

    async def on_bot_started_speaking(self) -> None:
        if self._speech_purpose:
            self._speech_started = True
        elif self._awaiting_answer:
            self._answer_started = True

    async def on_bot_stopped_speaking(self) -> None:
        if self._speech_purpose and self._speech_started:
            generation, purpose, step_index = self._speech_purpose
            self._speech_purpose = None
            self._speech_started = False
            if generation != self._generation or not self._connected:
                return
            if purpose == "introduction":
                self._schedule_timer("offer", self.definition.offer_delay_secs)
            elif purpose == "auto_start":
                self._launch_action(step_index)
            elif purpose == "transition":
                self._launch_action(step_index)
            elif purpose == "explanation":
                self.state = GuidedDemoState.WAITING_FOR_USER
                self._schedule_timer(
                    "advance", self.definition.user_intervention_window_secs
                )
            elif purpose == "completion":
                self.state = GuidedDemoState.COMPLETED
            return

        if self._awaiting_answer and self._answer_started:
            self._awaiting_answer = False
            self._answer_started = False
            if self.state == GuidedDemoState.OFFERED:
                self._schedule_timer("offer", self.definition.offer_delay_secs)
            elif self.state == GuidedDemoState.ANSWERING_QUESTION:
                if self.action_in_progress:
                    self._answer_finished_while_action = True
                else:
                    self.state = GuidedDemoState.WAITING_FOR_USER
                    self._schedule_timer(
                        "advance", self.definition.user_intervention_window_secs
                    )
            elif self.state == GuidedDemoState.FAILED and self._deferred_failure:
                self._deferred_failure = False
                await self._speak(self.definition.failure_message, "failure")

    async def on_transcription_timeout(self) -> None:
        if not self._user_speaking or not self._connected:
            return
        self._user_speaking = False
        self._awaiting_answer = False
        self._answer_started = False
        interrupted_speech = self._interrupted_speech
        self._interrupted_speech = None
        if self._explicitly_paused:
            self.state = GuidedDemoState.PAUSED
        elif self.action_in_progress:
            self._pause_after_action = False
            self.state = GuidedDemoState.RUNNING
        elif self._pending_explanation_step is not None:
            step_index = self._pending_explanation_step
            self._pending_explanation_step = None
            self.state = GuidedDemoState.RUNNING
            await self._speak(
                self.definition.steps[step_index].explanation,
                "explanation",
                step_index=step_index,
            )
        elif interrupted_speech:
            purpose, step_index = interrupted_speech
            if purpose == "introduction":
                self.state = GuidedDemoState.OFFERED
                await self._speak(self.definition.introduction, "introduction")
            elif purpose in {"auto_start", "transition"} and step_index is not None:
                await self._begin_step(step_index)
            elif purpose == "explanation" and step_index is not None:
                self.state = GuidedDemoState.RUNNING
                await self._speak(
                    self.definition.steps[step_index].explanation,
                    "explanation",
                    step_index=step_index,
                )
            elif purpose == "completion":
                await self._complete_demo()
        elif self.state == GuidedDemoState.OFFERED:
            self._schedule_timer("offer", self.definition.offer_delay_secs)
        elif self.current_step_index is not None and (
            self.last_confirmed_step
            != self.definition.steps[self.current_step_index].id
        ):
            await self._begin_step(self.current_step_index)
        elif self.state not in {
            GuidedDemoState.IDLE,
            GuidedDemoState.COMPLETED,
            GuidedDemoState.CANCELLED,
            GuidedDemoState.FAILED,
        }:
            self.state = GuidedDemoState.WAITING_FOR_USER
            self._schedule_timer(
                "advance", self.definition.user_intervention_window_secs
            )

    async def on_external_action(self, command: str, payload: dict, result: dict) -> None:
        if (
            result.get("status") != "confirmed"
            or command != "navigate_to"
            or self.state
            in {
                GuidedDemoState.IDLE,
                GuidedDemoState.COMPLETED,
                GuidedDemoState.CANCELLED,
                GuidedDemoState.FAILED,
            }
        ):
            return
        if self.state == GuidedDemoState.OFFERED:
            self._cancel_timer()
            self.state = GuidedDemoState.CANCELLED
            return
        view = payload.get("view")
        for index, step in enumerate(self.definition.steps):
            if step.payload.get("view") == view:
                self._cancel_timer()
                self._pending_explanation_step = None
                self.current_step_index = index
                self.last_confirmed_step = step.id
                self.next_step_index = index + 1
                self.state = (
                    GuidedDemoState.PAUSED
                    if self._explicitly_paused
                    else GuidedDemoState.ANSWERING_QUESTION
                )
                return

    async def wait_for_action(self) -> None:
        task = self._action_task
        if task:
            await task

    async def _start_demo(self, user_text: str | None = None) -> None:
        self._invalidate_run()
        self.current_step_index = None
        self.last_confirmed_step = None
        self.next_step_index = 0
        self._explicitly_paused = False
        self.state = GuidedDemoState.RUNNING
        await self._begin_step(0, user_text=user_text)

    async def _continue_demo(self, user_text: str | None = None) -> None:
        if self.action_in_progress:
            self._pause_after_action = False
            if not self._explicitly_paused:
                self.state = GuidedDemoState.RUNNING
            return
        self._cancel_timer()
        self._explicitly_paused = False
        if self._pending_explanation_step is not None:
            step_index = self._pending_explanation_step
            self._pending_explanation_step = None
            self.state = GuidedDemoState.RUNNING
            await self._speak(
                self.definition.steps[step_index].explanation,
                "explanation",
                step_index=step_index,
                user_text=user_text,
            )
            return
        if self.next_step_index >= len(self.definition.steps):
            await self._complete_demo(user_text=user_text)
            return
        await self._begin_step(self.next_step_index, user_text=user_text)

    async def _begin_step(self, step_index: int, user_text: str | None = None) -> None:
        if not self._can_run() or step_index >= len(self.definition.steps):
            return
        self.current_step_index = step_index
        self.next_step_index = step_index
        self.state = GuidedDemoState.RUNNING
        step = self.definition.steps[step_index]
        await self._speak(
            step.transition_message,
            "transition",
            step_index=step_index,
            user_text=user_text,
        )

    def _launch_action(self, step_index: int | None) -> None:
        if step_index is None or self.action_in_progress or not self._can_run():
            return
        generation = self._generation
        task = asyncio.create_task(self._run_action(step_index, generation))
        self._action_task = task
        task.add_done_callback(self._consume_task_result)

    async def _run_action(self, step_index: int, generation: int) -> None:
        self.action_in_progress = True
        step = self.definition.steps[step_index]
        try:
            try:
                result = await self._execute_action(step.command, step.payload)
            except asyncio.CancelledError:
                raise
            except Exception:
                result = {"status": "error", "code": "action_failed"}
        finally:
            self.action_in_progress = False
        if generation != self._generation or not self._connected:
            return
        if result.get("status") != "confirmed":
            self.state = GuidedDemoState.FAILED
            if self._user_speaking or self._awaiting_answer:
                self._deferred_failure = True
            else:
                await self._speak(self.definition.failure_message, "failure")
            return

        self.current_step_index = step_index
        self.last_confirmed_step = step.id
        self.next_step_index = step_index + 1
        if self._explicitly_paused:
            self._pending_explanation_step = step_index
            self._pause_after_action = False
            self._answer_finished_while_action = False
            self.state = GuidedDemoState.PAUSED
            return
        if self._answer_finished_while_action:
            self._answer_finished_while_action = False
            self._pause_after_action = False
            self.state = GuidedDemoState.WAITING_FOR_USER
            self._schedule_timer(
                "advance", self.definition.user_intervention_window_secs
            )
            return
        if self._user_speaking or self._pause_after_action or self._explicitly_paused:
            if self._user_speaking or not self._awaiting_answer:
                self._pending_explanation_step = step_index
            self._pause_after_action = False
            if not self._explicitly_paused:
                self.state = GuidedDemoState.ANSWERING_QUESTION
            return
        await self._speak(step.explanation, "explanation", step_index=step_index)

    async def _complete_demo(self, user_text: str | None = None) -> None:
        self.state = GuidedDemoState.RUNNING
        await self._speak(
            self.definition.completion_message,
            "completion",
            user_text=user_text,
        )

    def _schedule_timer(self, kind: str, delay: float) -> None:
        if not self._can_run() or self._explicitly_paused:
            return
        self._cancel_timer()
        generation = self._generation
        task = asyncio.create_task(self._run_timer(kind, delay, generation))
        self._timer_task = task
        task.add_done_callback(self._consume_task_result)

    async def _run_timer(self, kind: str, delay: float, generation: int) -> None:
        try:
            await self._sleep(delay)
            if self._timer_task is asyncio.current_task():
                self._timer_task = None
            if generation != self._generation or not self._can_run() or self._user_speaking:
                return
            if kind == "offer" and self.state == GuidedDemoState.OFFERED:
                self.current_step_index = 0
                self.next_step_index = 0
                self.state = GuidedDemoState.RUNNING
                await self._speak(
                    self.definition.auto_start_message,
                    "auto_start",
                    step_index=0,
                )
            elif kind == "advance" and self.state == GuidedDemoState.WAITING_FOR_USER:
                if self.next_step_index >= len(self.definition.steps):
                    await self._complete_demo()
                else:
                    await self._begin_step(self.next_step_index)
        finally:
            if self._timer_task is asyncio.current_task():
                self._timer_task = None

    async def _speak(
        self,
        text: str,
        purpose: str,
        *,
        step_index: int | None = None,
        user_text: str | None = None,
    ) -> None:
        if not self._connected:
            return
        self._speech_purpose = (self._generation, purpose, step_index)
        self._speech_started = False
        await self._emit_speech(text, user_text)

    def _can_run(self) -> bool:
        return self._connected and self._ui_ready and self.state not in {
            GuidedDemoState.CANCELLED,
            GuidedDemoState.COMPLETED,
            GuidedDemoState.FAILED,
        }

    def _cancel_timer(self) -> None:
        if self._timer_task and not self._timer_task.done():
            self._timer_task.cancel()
        self._timer_task = None

    def _cancel_action(self) -> None:
        if self._action_task and not self._action_task.done():
            self._action_task.cancel()
        self._action_task = None
        self.action_in_progress = False

    def _invalidate_run(self) -> None:
        self._generation += 1
        self._cancel_timer()
        self._cancel_action()
        self._speech_purpose = None
        self._speech_started = False
        self._awaiting_answer = False
        self._answer_started = False
        self._pause_after_action = False
        self._answer_finished_while_action = False
        self._deferred_failure = False
        self._interrupted_speech = None
        self._explicitly_paused = False
        self._user_speaking = False
        self._pending_explanation_step = None

    @staticmethod
    def _consume_task_result(task: asyncio.Task) -> None:
        try:
            task.exception()
        except asyncio.CancelledError:
            pass


def _normalize_phrase(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s']", " ", text.casefold()).split())
