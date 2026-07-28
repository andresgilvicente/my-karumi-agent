import asyncio
import json
import time
from collections import deque
from collections.abc import Awaitable, Callable
from uuid import uuid4

from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.frames.frames import (
    BotStoppedSpeakingFrame,
    LLMRunFrame,
    LLMUpdateSettingsFrame,
    MetricsFrame,
    TTSSpeakFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.metrics.metrics import ProcessingMetricsData, TTFAMetricsData
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.observers.user_bot_latency_observer import UserBotLatencyObserver
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.processors.frameworks.rtvi import RTVIUICommandFrame
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.services.kokoro.tts import KokoroTTSService
from pipecat.services.llm_service import FunctionCallParams
from pipecat.services.ollama.llm import OLLamaLLMService
from pipecat.services.whisper.stt import WhisperSTTService
from pipecat.transcriptions.language import Language
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.turns.user_start import VADUserTurnStartStrategy
from pipecat.turns.user_stop import SpeechTimeoutUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.workers.runner import WorkerRunner

# ---------------------------------------------------------------------------
# Config — change these to swap models / voices without touching the pipeline.
# ---------------------------------------------------------------------------
STT_MODEL = "base" # "tiny"
STT_LANGUAGE = Language.EN

LLM_MODEL = "qwen2.5:1.5b"

TTS_VOICE = "af_heart"
TTS_LANGUAGE = Language.EN

USER_SPEECH_TIMEOUT_SECS = 1 # 0.6
UI_ACTION_TIMEOUT_SECS = 5.0

# ---------------------------------------------------------------------------
# Agent personality
# ---------------------------------------------------------------------------
SYSTEM_INSTRUCTION = """You are Karumi Agent, an AI Product Demo Specialist for Metric Master Suite.

You are speaking with a user in a live voice conversation. Keep responses concise, natural, and
easy to follow when spoken aloud. Use plain sentences, not markdown, lists, or emojis.
Keep responses under 2-3 sentences unless the user asks for detail.

Take initiative instead of behaving like a passive support chatbot. Introduce the product, answer
questions, and suggest a useful next topic when appropriate. The product includes a dashboard with
revenue, active users, conversion and growth metrics; company goals; traffic analytics; and team
information.

You can control the connected Metric Master Suite interface only through the provided tools. For
this MVP, call tools before giving the final spoken response. A tool action is complete only when
its result has status "confirmed". Never say or imply that an action succeeded after an error or
timeout; say briefly that you could not update the interface instead. The navigate_to tool also
performs and confirms the standard focus action for that view: Revenue on Dashboard, Traffic
Overview on Analytics, and the member list on Team. After navigate_to returns confirmed, explain
the focused content without calling a second tool. Never narrate a command or speak instructions
such as "Navigate to the Analytics view" instead of calling the tool. Use highlight or scroll_to
separately only when the user asks to focus an element on the view that is already open.
Do not use interface tools for Goals, search, analytics tabs, or any other action. Always respond in
English.

At the beginning of a session, introduce yourself as Karumi Agent, offer a brief interactive tour
of Metric Master Suite, and invite the user to interrupt or ask questions."""

UI_VIEWS = {"dashboard", "analytics", "team"}
UI_HIGHLIGHT_TARGETS = {
    "total-revenue-card": "dashboard",
    "traffic-overview": "analytics",
}
UI_SCROLL_TARGETS = {"team-member-list": "team"}

NAVIGATION_PHRASES = (
    "show me",
    "take me",
    "bring me",
    "go to",
    "open",
    "navigate",
    "switch to",
    "move to",
    "visit",
    "look at",
)
UI_VIEW_PHRASES = {
    "dashboard": ("dashboard", "revenue"),
    "analytics": ("analytics", "traffic"),
    "team": ("team", "member list", "members"),
}


def _first_phrase_index(text: str, phrases: tuple[str, ...]) -> int | None:
    indexes = [text.find(phrase) for phrase in phrases if phrase in text]
    return min(indexes) if indexes else None


def _requested_view(text: str) -> tuple[str, int] | None:
    matches = {
        view: _first_phrase_index(text, phrases)
        for view, phrases in UI_VIEW_PHRASES.items()
    }
    matches = {view: index for view, index in matches.items() if index is not None}
    if len(matches) != 1:
        return None
    return next(iter(matches.items()))


def resolve_ui_intent(text: str, previous_text: str = "") -> tuple[str, dict] | None:
    """Resolve only the small set of semantic UI actions supported by this MVP."""
    current = text.casefold().strip()
    previous = previous_text.casefold().strip()
    requested_view = _requested_view(current)

    if "scroll" in current and any(
        phrase in current for phrase in ("team", "member", "list")
    ):
        return "scroll_to", {"target": "team-member-list"}

    focus_index = _first_phrase_index(current, ("highlight", "focus", "emphasize"))
    if focus_index is not None and requested_view is not None:
        view, target_index = requested_view
        if focus_index < target_index and view == "dashboard":
            return "highlight", {"target": "total-revenue-card"}
        if focus_index < target_index and view == "analytics":
            return "highlight", {"target": "traffic-overview"}

    if requested_view is None:
        return None

    view, target_index = requested_view
    navigation_index = _first_phrase_index(current, NAVIGATION_PHRASES)
    continued_navigation = (
        _first_phrase_index(previous, NAVIGATION_PHRASES) is not None
        and current.startswith(("to ", "into "))
    )
    if (
        navigation_index is not None and navigation_index < target_index
    ) or continued_navigation:
        return "navigate_to", {"view": view}

    return None


def ui_action_response(command: str, payload: dict, result: dict) -> str:
    if result.get("status") != "confirmed":
        return "I couldn't update the interface. Please try again."

    if command == "navigate_to":
        return {
            "dashboard": "The Dashboard is open, with Total Revenue highlighted.",
            "analytics": "Analytics is open, with Traffic Overview highlighted.",
            "team": "The Team view is open, and I've scrolled to the member list.",
        }[payload["view"]]
    if command == "highlight":
        return {
            "total-revenue-card": "I've highlighted Total Revenue.",
            "traffic-overview": "I've highlighted Traffic Overview.",
        }[payload["target"]]
    return "I've scrolled to the Team member list."


class UIActionToolChoiceProcessor(FrameProcessor):
    """Dispatch explicit UI intents and leave normal conversation to the LLM."""

    def __init__(
        self,
        context: LLMContext,
        *,
        navigate_tool: FunctionSchema,
        highlight_tool: FunctionSchema,
        scroll_tool: FunctionSchema,
        execute_action: Callable[[str, dict], Awaitable[dict]],
    ):
        super().__init__()
        self._context = context
        self._navigate_tool = navigate_tool
        self._highlight_tool = highlight_tool
        self._scroll_tool = scroll_tool
        self._execute_action = execute_action
        self._previous_transcript = ""
        self._previous_transcript_at = 0.0

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame):
            text = frame.text.casefold()
            now = time.monotonic()
            previous_text = (
                self._previous_transcript
                if now - self._previous_transcript_at <= 6.0
                else ""
            )
            intent = resolve_ui_intent(text, previous_text)
            self._previous_transcript = text
            self._previous_transcript_at = now

            if intent is not None:
                command, payload = intent
                self._context.set_tools([])
                self._context.set_tool_choice("auto")
                self._context.add_message({"role": "user", "content": frame.text.strip()})
                logger.info(f"[UI] Deterministic intent: {command} {payload}")
                result = await self._execute_action(command, payload)
                response = ui_action_response(command, payload, result)
                logger.info(f"[UI] Deterministic result: {result}")
                self._previous_transcript = ""
                self._previous_transcript_at = 0.0
                await self.push_frame(TTSSpeakFrame(response), direction)
                return

            navigation_action = any(
                term in text
                for term in ("show me", "take me", "go to", "open", "navigate")
            ) and any(
                target in text
                for target in ("dashboard", "analytics", "team", "revenue", "traffic", "there")
            )
            if "scroll" in text:
                selected_tool = self._scroll_tool
            elif any(term in text for term in ("highlight", "focus")):
                selected_tool = self._highlight_tool
            elif navigation_action:
                selected_tool = self._navigate_tool
            else:
                selected_tool = None

            self._context.set_tools([selected_tool] if selected_tool else [])
            tool_choice = "required" if selected_tool else "auto"
            self._context.set_tool_choice(tool_choice)
            tool_name = selected_tool.name if selected_tool else "none"
            logger.info(f"[UI] Tool choice for turn: {tool_choice} ({tool_name})")
        await self.push_frame(frame, direction)

# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------
TRANSPORT_PARAMS = {
    "webrtc": lambda: TransportParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
    ),
}


# ---------------------------------------------------------------------------
# Latency observer — supplements Pipecat's end-to-end latency observer with
# transcription, TTFA, and total processing measurements from native metrics.
# ---------------------------------------------------------------------------
class VoiceMetricsObserver(BaseObserver):
    """Log per-turn transcription and native service timing metrics."""

    def __init__(self, *, stt_name: str, llm_name: str, tts_name: str):
        super().__init__()
        self._stt_name = stt_name
        self._llm_name = llm_name
        self._tts_name = tts_name
        self._speech_end_time: float | None = None
        self._tts_processing_total = 0.0
        self._tts_ttfa_logged = False
        self._processed_frames: set[int] = set()
        self._frame_history: deque[int] = deque(maxlen=200)

    async def on_push_frame(self, data: FramePushed):
        if (
            data.direction != FrameDirection.DOWNSTREAM
            or data.frame.id in self._processed_frames
        ):
            return

        self._processed_frames.add(data.frame.id)
        self._frame_history.append(data.frame.id)
        if len(self._processed_frames) > len(self._frame_history):
            self._processed_frames = set(self._frame_history)

        frame = data.frame
        if isinstance(frame, VADUserStartedSpeakingFrame):
            self._speech_end_time = None
            self._tts_processing_total = 0.0
            self._tts_ttfa_logged = False
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            self._speech_end_time = frame.timestamp - frame.stop_secs
        elif isinstance(frame, TranscriptionFrame):
            logger.info(f"[STT] Transcription: {frame.text.strip()!r}")
            if self._speech_end_time is not None:
                latency = time.time() - self._speech_end_time
                logger.info(f"[LATENCY] End of user speech -> transcription: {latency:.3f}s")
        elif isinstance(frame, MetricsFrame):
            self._handle_metrics(frame)
        elif isinstance(frame, BotStoppedSpeakingFrame) and self._tts_processing_total > 0:
            logger.info(
                f"[LATENCY] TTS generation total across segments: "
                f"{self._tts_processing_total:.3f}s"
            )
            self._speech_end_time = None

    def _handle_metrics(self, frame: MetricsFrame):
        for metric in frame.data:
            if isinstance(metric, ProcessingMetricsData):
                if metric.processor == self._stt_name:
                    logger.info(f"[LATENCY] STT processing: {metric.value:.3f}s")
                elif metric.processor == self._llm_name:
                    logger.info(f"[LATENCY] LLM total: {metric.value:.3f}s")
                elif metric.processor == self._tts_name:
                    self._tts_processing_total += metric.value
                    logger.info(f"[LATENCY] TTS segment total: {metric.value:.3f}s")
            elif (
                isinstance(metric, TTFAMetricsData)
                and metric.processor == self._tts_name
                and not self._tts_ttfa_logged
            ):
                self._tts_ttfa_logged = True
                logger.info(f"[LATENCY] TTS TTFA: {metric.ttfa:.3f}s")


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------
async def run_bot(transport: BaseTransport, runner_args: RunnerArguments) -> None:
    current_ui_state: dict | None = None
    pending_ui_actions: dict[str, asyncio.Future] = {}
    worker: PipelineWorker | None = None

    def instruction_with_ui_state() -> str:
        if current_ui_state is None:
            state_instruction = (
                "No React UI state has been received. Do not call interface tools until the "
                "frontend publishes its state."
            )
        else:
            state_instruction = (
                "Current React UI state (replace any older UI assumptions with this value): "
                f"{json.dumps(current_ui_state, separators=(',', ':'))}"
            )
        return f"{SYSTEM_INSTRUCTION}\n\n{state_instruction}"

    stt = WhisperSTTService(
        device="cpu",
        compute_type="int8",
        settings=WhisperSTTService.Settings(
            model=STT_MODEL,
            language=STT_LANGUAGE,
        ),
    )

    llm = OLLamaLLMService(
        settings=OLLamaLLMService.Settings(
            model=LLM_MODEL,
            system_instruction=instruction_with_ui_state(),
            temperature=0.3,
            max_tokens=150,
        ),
    )

    tts = KokoroTTSService(
        settings=KokoroTTSService.Settings(
            voice=TTS_VOICE,
            language=TTS_LANGUAGE,
        ),
    )

    async def execute_ui_action(command: str, payload: dict) -> dict:
        """Send one semantic command and wait for the React confirmation."""
        if worker is None or current_ui_state is None:
            return {"status": "error", "code": "ui_state_unavailable"}

        command_id = uuid4().hex
        confirmation = asyncio.get_running_loop().create_future()
        pending_ui_actions[command_id] = confirmation
        logger.info(f"[UI] Sending {command} command {command_id}: {payload}")

        try:
            await worker.queue_frame(
                RTVIUICommandFrame(
                    command=command,
                    payload={"commandId": command_id, **payload},
                )
            )
            result = await asyncio.wait_for(confirmation, timeout=UI_ACTION_TIMEOUT_SECS)
        except TimeoutError:
            logger.warning(f"[UI] Command {command_id} timed out")
            return {"status": "timeout", "action": command}
        except Exception as error:
            logger.warning(f"[UI] Command {command_id} failed: {error}")
            return {"status": "error", "action": command, "code": "command_failed"}
        finally:
            pending_ui_actions.pop(command_id, None)

        if result.get("status") != "ok":
            return {
                "status": "error",
                "action": command,
                "code": result.get("code", "action_failed"),
            }

        return {
            "status": "confirmed",
            "action": command,
            **{key: value for key, value in result.items() if key not in {"commandId", "status"}},
        }

    async def finish_tool_call(params: FunctionCallParams, result: dict) -> None:
        context.set_tools([])
        context.set_tool_choice("auto")
        await params.result_callback(result)

    async def perform_navigation(view: str) -> dict:
        if view not in UI_VIEWS:
            return {"status": "error", "code": "unsupported_view"}

        navigation = await execute_ui_action("navigate_to", {"view": view})
        if navigation.get("status") != "confirmed":
            return navigation

        if view == "dashboard":
            focus = await execute_ui_action("highlight", {"target": "total-revenue-card"})
        elif view == "analytics":
            focus = await execute_ui_action("highlight", {"target": "traffic-overview"})
        else:
            focus = await execute_ui_action("scroll_to", {"target": "team-member-list"})

        if focus.get("status") != "confirmed":
            return {
                "status": "error",
                "code": "focus_action_failed",
                "navigation": navigation,
                "focus": focus,
            }

        return {
            "status": "confirmed",
            "action": "navigate_to",
            "view": view,
            "navigation": navigation,
            "focus": focus,
        }

    async def perform_highlight(target: str) -> dict:
        expected_page = UI_HIGHLIGHT_TARGETS.get(target)
        if expected_page is None:
            return {"status": "error", "code": "unsupported_target"}
        if current_ui_state is None or current_ui_state.get("currentPage") != expected_page:
            return {"status": "error", "code": "target_not_visible"}
        return await execute_ui_action("highlight", {"target": target})

    async def perform_scroll(target: str) -> dict:
        expected_page = UI_SCROLL_TARGETS.get(target)
        if expected_page is None:
            return {"status": "error", "code": "unsupported_target"}
        if current_ui_state is None or current_ui_state.get("currentPage") != expected_page:
            return {"status": "error", "code": "target_not_visible"}
        return await execute_ui_action("scroll_to", {"target": target})

    async def execute_ui_intent(command: str, payload: dict) -> dict:
        if command == "navigate_to":
            return await perform_navigation(payload.get("view"))
        if command == "highlight":
            return await perform_highlight(payload.get("target"))
        if command == "scroll_to":
            return await perform_scroll(payload.get("target"))
        return {"status": "error", "code": "unsupported_action"}

    async def navigate_to(params: FunctionCallParams):
        await finish_tool_call(
            params,
            await perform_navigation(params.arguments.get("view")),
        )

    async def highlight(params: FunctionCallParams):
        await finish_tool_call(
            params,
            await perform_highlight(params.arguments.get("target")),
        )

    async def scroll_to(params: FunctionCallParams):
        await finish_tool_call(
            params,
            await perform_scroll(params.arguments.get("target")),
        )

    navigate_tool = FunctionSchema(
        name="navigate_to",
        description=(
            "Required for every request to show, open, or go to Dashboard, Analytics, or Team. "
            "This function performs the UI action; never describe the command in spoken text "
            "instead."
        ),
        properties={
            "view": {
                "type": "string",
                "enum": ["dashboard", "analytics", "team"],
                "description": "The destination requested by the user.",
            }
        },
        required=["view"],
        handler=navigate_to,
    )
    highlight_tool = FunctionSchema(
        name="highlight",
        description="Highlight a supported element on the currently visible view.",
        properties={
            "target": {
                "type": "string",
                "enum": ["total-revenue-card", "traffic-overview"],
            }
        },
        required=["target"],
        handler=highlight,
    )
    scroll_tool = FunctionSchema(
        name="scroll_to",
        description="Scroll to the Team member list when the Team view is visible.",
        properties={
            "target": {
                "type": "string",
                "enum": ["team-member-list"],
            }
        },
        required=["target"],
        handler=scroll_to,
    )
    context = LLMContext(
        tools=[navigate_tool, highlight_tool, scroll_tool]
    )
    ui_intent_gate = UIActionToolChoiceProcessor(
        context,
        navigate_tool=navigate_tool,
        highlight_tool=highlight_tool,
        scroll_tool=scroll_tool,
        execute_action=execute_ui_intent,
    )
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(),
            user_turn_strategies=UserTurnStrategies(
                start=[VADUserTurnStartStrategy()],
                stop=[
                    SpeechTimeoutUserTurnStopStrategy(
                        user_speech_timeout=USER_SPEECH_TIMEOUT_SECS
                    )
                ],
            ),
        ),
    )

    latency_observer = UserBotLatencyObserver()
    metrics_observer = VoiceMetricsObserver(
        stt_name=stt.name,
        llm_name=llm.name,
        tts_name=tts.name,
    )

    @latency_observer.event_handler("on_first_bot_speech_latency")
    async def on_first_bot_speech_latency(observer, latency):
        logger.info(f"[LATENCY] Client connected -> first bot audio: {latency:.3f}s")

    @latency_observer.event_handler("on_latency_measured")
    async def on_latency_measured(observer, latency):
        logger.info(f"[LATENCY] End of user speech -> first bot audio: {latency:.3f}s")

    @latency_observer.event_handler("on_latency_breakdown")
    async def on_latency_breakdown(observer, breakdown):
        events = breakdown.chronological_events()
        if events:
            logger.info(f"[LATENCY] Breakdown: {' | '.join(events)}")

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            ui_intent_gate,
            user_aggregator,
            llm,
            tts,
            transport.output(),
            assistant_aggregator,
        ]
    )

    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(
            audio_out_sample_rate=24000,
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
        observers=[latency_observer, metrics_observer],
        idle_timeout_secs=runner_args.pipeline_idle_timeout_secs,
    )

    async def update_ui_instruction() -> None:
        await worker.queue_frame(
            LLMUpdateSettingsFrame(
                delta=OLLamaLLMService.Settings(
                    system_instruction=instruction_with_ui_state(),
                ),
                service=llm,
            )
        )

    @worker.rtvi.event_handler("on_ui_message")
    async def on_ui_message(rtvi, message):
        nonlocal current_ui_state
        if message.type != "ui-event" or not isinstance(message.data.payload, dict):
            return

        payload = message.data.payload
        if message.data.event == "ui_state":
            current_page = payload.get("currentPage")
            if current_page not in {"home", *UI_VIEWS}:
                logger.warning(f"[UI] Ignoring invalid UI state: {payload}")
                return
            visible_elements = payload.get("visibleElements")
            available_actions = payload.get("availableActions")
            current_ui_state = {
                "revision": payload.get("revision"),
                "currentPage": current_page,
                "selectedTab": payload.get("selectedTab"),
                "visibleElements": visible_elements[:10]
                if isinstance(visible_elements, list)
                else [],
                "availableActions": available_actions[:10]
                if isinstance(available_actions, list)
                else [],
            }
            logger.info(f"[UI] State updated: {current_ui_state}")
            await update_ui_instruction()
        elif message.data.event == "ui_action_result":
            command_id = payload.get("commandId")
            confirmation = pending_ui_actions.get(command_id)
            if confirmation is None or confirmation.done():
                logger.debug(f"[UI] Ignoring late or unknown confirmation: {command_id}")
                return

            if payload.get("status") == "ok" and payload.get("currentPage") in UI_VIEWS:
                current_ui_state = {
                    **(current_ui_state or {}),
                    "currentPage": payload["currentPage"],
                }
                await update_ui_instruction()
            logger.info(f"[UI] Confirmation received for {command_id}: {payload}")
            confirmation.set_result(payload)

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        await worker.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        await worker.cancel()

    runner = WorkerRunner(handle_sigint=runner_args.handle_sigint)
    await runner.add_workers(worker)
    await runner.run()


async def bot(runner_args: RunnerArguments) -> None:
    transport = await create_transport(runner_args, TRANSPORT_PARAMS)
    await run_bot(transport, runner_args)


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()
