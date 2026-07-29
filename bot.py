import asyncio
import argparse
import json
import os
import sys
import time
from collections import deque
from uuid import uuid4

from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    LLMUpdateSettingsFrame,
    MetricsFrame,
    TTSSpeakFrame,
    TranscriptionFrame,
    UserStoppedSpeakingFrame,
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
from pipecat.processors.frame_processor import FrameDirection
from pipecat.processors.frameworks.rtvi import RTVIUICommandFrame
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.services.kokoro.tts import KokoroTTSService
from pipecat.services.ollama.llm import OLLamaLLMService
from pipecat.services.whisper.stt import WhisperSTTService
from pipecat.transcriptions.language import Language
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.turns.user_start import TranscriptionUserTurnStartStrategy
from pipecat.turns.user_stop import SpeechTimeoutUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.utils.text.markdown_text_filter import MarkdownTextFilter
from pipecat.workers.runner import WorkerRunner

from ui_control.catalog import load_catalog
from ui_control.guided_demo import GuidedDemoController, load_guided_demo
from ui_control.guided_demo_processor import GuidedDemoProcessor
from ui_control.processor import UIActionExecutor, UIActionToolChoiceProcessor
from ui_control.profiling import LocalVoiceProfile, create_local_profile
from ui_control.turns import ConsumedUserTurnStopStrategy
from voice_input import BotEchoSuppressionProcessor

# ---------------------------------------------------------------------------
# Runtime configuration
# ---------------------------------------------------------------------------
STT_MODEL = "base"
STT_LANGUAGE = Language.EN
STT_NO_SPEECH_PROB = 0.2

LLM_MODEL = "qwen2.5:1.5b"

TTS_VOICE = "af_heart"
TTS_LANGUAGE = Language.EN

USER_SPEECH_TIMEOUT_SECS = 1.0
UI_ACTION_TIMEOUT_SECS = 5.0
UI_CATALOG = load_catalog()
GUIDED_DEMO = load_guided_demo(UI_CATALOG)


def debug_logs_enabled(runner_args: RunnerArguments) -> bool:
    """Enable detailed application logs with a flag or environment variable."""
    return bool(getattr(runner_args.cli_args, "debug_logs", False)) or os.getenv(
        "KARUMI_DEBUG_LOGS", ""
    ).lower() == "true"


def configure_logging(debug_enabled: bool) -> None:
    """Keep normal output focused on the application conversation and actions."""
    app_loggers = ("__main__", "bot", "ui_control")

    def is_relevant(record) -> bool:
        return record["level"].name in {"WARNING", "ERROR", "CRITICAL"} or record[
            "name"
        ].startswith(app_loggers)

    logger.remove()
    logger.add(
        sys.stderr,
        level="DEBUG" if debug_enabled else "INFO",
        filter=is_relevant,
        format="{time:HH:mm:ss} | {level:<7} | {message}",
    )


def log_agent_response(text: str) -> None:
    """Keep the transcript readable even if the model emits Markdown markers."""
    logger.opt(colors=True).info(
        "<green>[AGENT]</green> {}", text.translate(str.maketrans("", "", "*_`"))
    )


def reject_pending_ui_actions(
    pending_actions: dict[str, tuple[asyncio.Future, str, dict, int]],
    code: str,
) -> int:
    """Settle all session-bound UI waits before their transport disappears."""
    count = 0
    for command_id, (confirmation, command, _, _) in list(pending_actions.items()):
        if not confirmation.done():
            confirmation.set_result(
                {
                    "status": "error",
                    "code": code,
                    "command": command,
                    "commandId": command_id,
                }
            )
            count += 1
    pending_actions.clear()
    return count


def pending_ui_action_for_session(
    pending_actions: dict[str, tuple[asyncio.Future, str, dict, int]],
    command_id: str | None,
    session_generation: int,
) -> tuple[asyncio.Future, str, dict, int] | None:
    pending_action = pending_actions.get(command_id or "")
    if pending_action is None or pending_action[0].done():
        return None
    return pending_action if pending_action[3] == session_generation else None


# ---------------------------------------------------------------------------
# Agent personality
# ---------------------------------------------------------------------------
SYSTEM_INSTRUCTION = """You are Karumi Agent, an AI Product Demo Specialist for Metric Master Suite.

You are speaking with a user in a live voice conversation. Keep responses concise, natural, and
easy to follow when spoken aloud. Use plain sentences only. Do not use Markdown syntax, numbered
lists, bullet points, headings, or emojis.
Keep responses under 2-3 sentences unless the user asks for detail.

Take initiative instead of behaving like a passive support chatbot. Introduce the product, answer
questions, and suggest a useful next topic when appropriate. The product includes a dashboard with
revenue, active users, conversion and growth metrics; company goals; traffic analytics; and team
information.

You can control the connected Metric Master Suite interface only through the provided tools. For
this MVP, call tools before giving the final spoken response. A tool action is complete only when
its result has status "confirmed". Never say or imply that an action succeeded after an error or
timeout; say briefly that you could not update the interface instead. The navigate_to tool also
performs and confirms the standard presentation actions for that view. After navigate_to returns
confirmed, explain the focused content without calling a second tool. Use focus_target for a
specific supported panel and search_team_member only for a requested person name. Search results
must come from the confirmed tool result; never invent members. Never narrate a command or speak
instructions such as "Navigate to the Analytics view" instead of calling the tool. Do not use
interface tools for Goals, analytics tabs, or any undeclared action. Always respond in English.

The guided-demo controller owns the tour sequence and its navigation. During a tour, answer the
user's questions naturally without claiming that you advanced, paused, or completed the tour."""

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
    """Log complete conversation turns and optional native service timing metrics."""

    def __init__(
        self,
        *,
        stt_name: str,
        llm_name: str,
        tts_name: str,
        transcription_source=None,
        bot_speech_listener=None,
        profile: LocalVoiceProfile | None = None,
    ):
        super().__init__()
        self._stt_name = stt_name
        self._llm_name = llm_name
        self._tts_name = tts_name
        self._transcription_source = transcription_source
        self._bot_speech_listener = bot_speech_listener
        self._profile = profile
        self._speech_end_time: float | None = None
        self._tts_processing_total = 0.0
        self._tts_ttfa_logged = False
        self._llm_response: list[str] = []
        self._processed_frames: set[int] = set()
        self._frame_history: deque[int] = deque(maxlen=200)

    async def on_push_frame(self, data: FramePushed):
        if (
            isinstance(data.frame, TranscriptionFrame)
            and self._transcription_source is not None
            and data.source is not self._transcription_source
        ):
            return
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
            logger.debug("[TURN] User started speaking")
            if self._profile:
                self._profile.start_turn()
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            self._speech_end_time = frame.timestamp - frame.stop_secs
            logger.debug("[TURN] User stopped speaking")
            if self._profile:
                self._profile.speech_ended(silence_confirmation_ms=frame.stop_secs * 1000)
        elif isinstance(frame, UserStoppedSpeakingFrame):
            if self._profile and self._speech_end_time is not None:
                self._profile.logical_turn_closed(
                    (time.time() - self._speech_end_time) * 1000
                )
        elif isinstance(frame, TranscriptionFrame):
            logger.opt(colors=True).info("<cyan>[USER]</cyan> {}", frame.text.strip())
            transcription_latency_ms = None
            if self._speech_end_time is not None:
                latency = time.time() - self._speech_end_time
                logger.debug(f"[LATENCY] End of user speech -> transcription: {latency:.3f}s")
                transcription_latency_ms = latency * 1000
            if self._profile:
                self._profile.transcription_received(frame.text, transcription_latency_ms)
        elif isinstance(frame, LLMFullResponseStartFrame):
            self._llm_response = []
        elif isinstance(frame, LLMTextFrame):
            self._llm_response.append(frame.text)
        elif isinstance(frame, LLMFullResponseEndFrame):
            response = "".join(self._llm_response).strip()
            if response:
                log_agent_response(response)
            self._llm_response = []
        elif isinstance(frame, TTSSpeakFrame):
            log_agent_response(frame.text.strip())
        elif isinstance(frame, MetricsFrame):
            self._handle_metrics(frame)
        elif isinstance(frame, BotStartedSpeakingFrame):
            if self._bot_speech_listener:
                self._bot_speech_listener.on_bot_started_speaking()
        elif isinstance(frame, BotStoppedSpeakingFrame):
            if self._bot_speech_listener:
                self._bot_speech_listener.on_bot_stopped_speaking()
            if self._tts_processing_total > 0:
                logger.debug(
                    f"[LATENCY] TTS generation total across segments: "
                    f"{self._tts_processing_total:.3f}s"
                )
            if self._profile:
                self._profile.bot_stopped_speaking(
                    self._tts_processing_total * 1000
                    if self._tts_processing_total > 0
                    else None
                )
            self._speech_end_time = None
            self._tts_processing_total = 0.0
            self._tts_ttfa_logged = False

    def _handle_metrics(self, frame: MetricsFrame):
        for metric in frame.data:
            if isinstance(metric, ProcessingMetricsData):
                if metric.processor == self._stt_name:
                    logger.debug(f"[LATENCY] STT processing: {metric.value:.3f}s")
                    if self._profile:
                        self._profile.service_metric("stt", "processing", metric.value * 1000)
                elif metric.processor == self._llm_name:
                    logger.debug(f"[LATENCY] LLM total: {metric.value:.3f}s")
                    if self._profile:
                        self._profile.service_metric("llm", "processing", metric.value * 1000)
                elif metric.processor == self._tts_name:
                    self._tts_processing_total += metric.value
                    logger.debug(f"[LATENCY] TTS segment total: {metric.value:.3f}s")
                    if self._profile:
                        self._profile.service_metric("tts", "processing", metric.value * 1000)
            elif (
                isinstance(metric, TTFAMetricsData)
                and metric.processor == self._tts_name
                and not self._tts_ttfa_logged
            ):
                self._tts_ttfa_logged = True
                logger.debug(f"[LATENCY] TTS TTFA: {metric.ttfa:.3f}s")
                if self._profile:
                    self._profile.service_metric("tts", "ttfa", metric.ttfa * 1000)


class GuidedDemoPlaybackObserver(BaseObserver):
    """Forward actual output playback boundaries to the guided-demo controller."""

    def __init__(self, controller: GuidedDemoController):
        super().__init__()
        self._controller = controller
        self._processed_frames: set[int] = set()
        self._frame_history: deque[int] = deque(maxlen=100)

    async def on_push_frame(self, data: FramePushed):
        if data.direction != FrameDirection.DOWNSTREAM or data.frame.id in self._processed_frames:
            return
        self._processed_frames.add(data.frame.id)
        self._frame_history.append(data.frame.id)
        if len(self._processed_frames) > len(self._frame_history):
            self._processed_frames = set(self._frame_history)

        if isinstance(data.frame, BotStartedSpeakingFrame):
            await self._controller.on_bot_started_speaking()
        elif isinstance(data.frame, BotStoppedSpeakingFrame):
            await self._controller.on_bot_stopped_speaking()


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------
async def run_bot(
    transport: BaseTransport,
    runner_args: RunnerArguments,
    profile: LocalVoiceProfile | None = None,
) -> None:
    current_ui_state: dict | None = None
    pending_ui_actions: dict[str, tuple[asyncio.Future, str, dict, int]] = {}
    worker: PipelineWorker | None = None
    active_session_id: str | None = None
    session_generation = 0

    if profile:
        profile.start_run(
            {
                "stt_model": STT_MODEL,
                "stt_language": str(STT_LANGUAGE),
                "stt_no_speech_prob": STT_NO_SPEECH_PROB,
                "llm_model": LLM_MODEL,
                "tts_voice": TTS_VOICE,
                "tts_language": str(TTS_LANGUAGE),
                "user_speech_timeout_secs": USER_SPEECH_TIMEOUT_SECS,
                "ui_action_timeout_secs": UI_ACTION_TIMEOUT_SECS,
                "ui_contract_version": UI_CATALOG.contract_version,
            }
        )

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
            no_speech_prob=STT_NO_SPEECH_PROB,
        ),
    )

    voice_input_guard = BotEchoSuppressionProcessor()

    llm = OLLamaLLMService(
        settings=OLLamaLLMService.Settings(
            model=LLM_MODEL,
            system_instruction=instruction_with_ui_state(),
            temperature=0.3,
            max_tokens=150,
        ),
    )

    tts = KokoroTTSService(
        text_filters=[MarkdownTextFilter()],
        settings=KokoroTTSService.Settings(
            voice=TTS_VOICE,
            language=TTS_LANGUAGE,
        ),
    )

    async def execute_ui_action(command: str, payload: dict) -> dict:
        """Send one semantic command and wait for the React confirmation."""
        action_id = uuid4().hex
        command_id: str | None = None
        started_at = time.monotonic()

        def profile_result(outcome: str, code: str | None = None) -> None:
            if profile:
                profile.ui_action_finished(
                    action_id,
                    command_id,
                    command,
                    payload,
                    outcome,
                    (time.monotonic() - started_at) * 1000,
                    code,
                )

        if (
            worker is None
            or active_session_id is None
            or not UI_CATALOG.supports_action(current_ui_state, command, payload)
        ):
            if profile:
                profile.ui_action_started(action_id, None, command, payload)
            profile_result("error", "ui_state_unavailable")
            return {"status": "error", "code": "ui_state_unavailable"}

        command_id = uuid4().hex
        command_generation = session_generation
        ui_session_id = current_ui_state["uiSessionId"]
        confirmation = asyncio.get_running_loop().create_future()
        pending_ui_actions[command_id] = (
            confirmation,
            command,
            {**payload, "uiSessionId": ui_session_id},
            command_generation,
        )
        if profile:
            profile.ui_action_started(action_id, command_id, command, payload)
        logger.opt(colors=True).info(
            "<yellow>[UI]</yellow> Sending {} {}: {}", command, command_id, payload
        )

        try:
            await worker.queue_frame(
                RTVIUICommandFrame(
                    command=command,
                    payload={
                        "commandId": command_id,
                        "uiSessionId": ui_session_id,
                        **payload,
                    },
                )
            )
            result = await asyncio.wait_for(confirmation, timeout=UI_ACTION_TIMEOUT_SECS)
        except TimeoutError:
            logger.opt(colors=True).warning("<yellow>[UI]</yellow> Command {} timed out", command_id)
            profile_result("timeout", "action_timeout")
            return {"status": "timeout", "action": command}
        except Exception as error:
            logger.opt(colors=True).warning("<yellow>[UI]</yellow> Command {} failed: {}", command_id, error)
            profile_result("error", "command_failed")
            return {"status": "error", "action": command, "code": "command_failed"}
        finally:
            pending_ui_actions.pop(command_id, None)

        if result.get("status") != "ok":
            logger.opt(colors=True).warning("<yellow>[UI]</yellow> {} failed: {}", command, result)
            profile_result("error", result.get("code", "action_failed"))
            return {
                "status": "error",
                "action": command,
                "code": result.get("code", "action_failed"),
            }

        logger.opt(colors=True).info(
            "<yellow>[UI]</yellow> {} {} confirmed", command, command_id
        )
        profile_result("confirmed")
        return {
            "status": "confirmed",
            "action": command,
            **{
                key: value
                for key, value in result.items()
                if key not in {"commandId", "uiSessionId", "status"}
            },
        }

    context = LLMContext()
    ui_actions = UIActionExecutor(
        UI_CATALOG,
        execute_ui_action,
        lambda: current_ui_state,
        lambda: session_generation,
    )

    def record_resolution(resolution: str, details: dict) -> None:
        if profile:
            profile.resolution(resolution, details)

    async def emit_demo_speech(text: str, user_text: str | None) -> None:
        if worker is None:
            return
        context.set_tools([])
        context.set_tool_choice("auto")
        await worker.queue_frame(TTSSpeakFrame(text))

    def record_demo_control(text: str) -> None:
        context.add_message({"role": "user", "content": text.strip()})

    demo_controller = GuidedDemoController(
        GUIDED_DEMO,
        ui_actions.execute,
        emit_demo_speech,
    )

    async def on_external_action(command: str, payload: dict, result: dict) -> None:
        await demo_controller.on_external_action(command, payload, result)

    guided_demo_gate = GuidedDemoProcessor(
        demo_controller,
        GUIDED_DEMO,
        record_resolution,
        record_demo_control,
    )
    ui_tools = ui_actions.build_tools(
        context,
        record_resolution,
        on_external_action,
        lambda: session_generation,
    )
    ui_intent_gate = UIActionToolChoiceProcessor(
        context,
        UI_CATALOG,
        ui_tools,
        ui_actions.execute,
        ui_actions.response_for,
        record_resolution,
        on_external_action,
        lambda: guided_demo_gate.user_turn_generation,
        lambda: session_generation,
        lambda: (current_ui_state or {}).get("currentPage"),
    )
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(),
            user_turn_strategies=UserTurnStrategies(
                start=[
                    TranscriptionUserTurnStartStrategy(
                        use_interim=False,
                        enable_interruptions=False,
                    )
                ],
                stop=[
                    ConsumedUserTurnStopStrategy(),
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
        transcription_source=voice_input_guard,
        bot_speech_listener=voice_input_guard,
        profile=profile,
    )
    demo_observer = GuidedDemoPlaybackObserver(demo_controller)

    @latency_observer.event_handler("on_first_bot_speech_latency")
    async def on_first_bot_speech_latency(observer, latency):
        logger.debug(f"[LATENCY] Client connected -> first bot audio: {latency:.3f}s")
        if profile:
            profile.first_bot_audio(latency * 1000, startup=True)

    @latency_observer.event_handler("on_latency_measured")
    async def on_latency_measured(observer, latency):
        logger.debug(f"[LATENCY] End of user speech -> first bot audio: {latency:.3f}s")
        if profile:
            profile.first_bot_audio(latency * 1000, startup=False)

    @latency_observer.event_handler("on_latency_breakdown")
    async def on_latency_breakdown(observer, breakdown):
        events = breakdown.chronological_events()
        if events:
            logger.debug(f"[LATENCY] Breakdown: {' | '.join(events)}")

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            voice_input_guard,
            guided_demo_gate,
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
        observers=[latency_observer, metrics_observer, demo_observer],
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
        if active_session_id is None:
            return
        if message.type != "ui-event" or not isinstance(message.data.payload, dict):
            return

        payload = message.data.payload
        message_generation = session_generation
        if message.data.event == "ui_state":
            current_page = payload.get("currentPage")
            revision = payload.get("revision")
            ui_session_id = payload.get("uiSessionId")
            if (
                current_page not in {"home", *UI_CATALOG.view_ids}
                or not isinstance(revision, int)
                or not isinstance(ui_session_id, str)
                or not ui_session_id
            ):
                logger.opt(colors=True).warning("<yellow>[UI]</yellow> Ignoring invalid state: {}", payload)
                return
            if (
                current_ui_state is not None
                and ui_session_id == current_ui_state.get("uiSessionId")
                and revision <= current_ui_state.get("revision", -1)
            ):
                logger.opt(colors=True).debug("<yellow>[UI]</yellow> Ignoring stale state revision {}", revision)
                return
            if current_ui_state is not None and ui_session_id != current_ui_state.get("uiSessionId"):
                reject_pending_ui_actions(pending_ui_actions, "ui_session_replaced")

            visible_elements = payload.get("visibleElements", [])
            current_ui_state = {
                "revision": revision,
                "uiSessionId": ui_session_id,
                "uiContractVersion": payload.get("uiContractVersion"),
                "currentPage": current_page,
                "selectedTab": payload.get("selectedTab"),
                "visibleElements": visible_elements[:10]
                if isinstance(visible_elements, list)
                else [],
                "supportedCommands": payload.get("supportedCommands", [])[:10]
                if isinstance(payload.get("supportedCommands"), list)
                else [],
                "supportedViews": payload.get("supportedViews", [])[:10]
                if isinstance(payload.get("supportedViews"), list)
                else [],
                "supportedTargets": payload.get("supportedTargets", [])[:50]
                if isinstance(payload.get("supportedTargets"), list)
                else [],
            }
            current_ui_state["contractCompatible"] = UI_CATALOG.is_contract_compatible(current_ui_state)
            if not current_ui_state["contractCompatible"]:
                logger.opt(colors=True).warning(
                    "<yellow>[UI]</yellow> Contract capabilities are incompatible: {}", current_ui_state
                )
            logger.opt(colors=True).debug("<yellow>[UI]</yellow> State updated: {}", current_ui_state)
            await update_ui_instruction()
            if message_generation != session_generation:
                return
            await demo_controller.set_ui_ready(current_ui_state["contractCompatible"])
        elif message.data.event == "ui_action_result":
            command_id = payload.get("commandId")
            pending_action = pending_ui_action_for_session(
                pending_ui_actions,
                command_id,
                session_generation,
            )
            if pending_action is None:
                logger.opt(colors=True).debug(
                    "<yellow>[UI]</yellow> Ignoring late or unknown confirmation: {}", command_id
                )
                return

            confirmation, command, expected_payload, _ = pending_action
            if not UI_CATALOG.confirmation_matches(command, expected_payload, payload):
                payload = {
                    "status": "error",
                    "code": "confirmation_mismatch",
                    "commandId": command_id,
                }
                logger.opt(colors=True).warning(
                    "<yellow>[UI]</yellow> Rejected incoherent confirmation for {}", command_id
                )
            elif payload.get("status") == "ok":
                current_ui_state = {
                    **(current_ui_state or {}),
                    "currentPage": payload["currentPage"],
                }
            if not confirmation.done():
                confirmation.set_result(payload)
            logger.opt(colors=True).debug(
                "<yellow>[UI]</yellow> Confirmation received for {}: {}", command_id, payload
            )
            if payload.get("status") == "ok" and message_generation == session_generation:
                await update_ui_instruction()

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        nonlocal active_session_id, current_ui_state, session_generation
        session_generation += 1
        active_session_id = uuid4().hex
        current_ui_state = None
        reject_pending_ui_actions(pending_ui_actions, "session_replaced")
        context.set_messages([])
        context.set_tools([])
        context.set_tool_choice("auto")
        ui_intent_gate.reset_session()
        logger.opt(colors=True).info(
            "<magenta>[SESSION]</magenta> Connected {} generation {}",
            active_session_id,
            session_generation,
        )
        if profile:
            profile.start_session()
        await demo_controller.connect(active_session_id)

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        nonlocal active_session_id, current_ui_state, session_generation
        disconnected_session = active_session_id
        active_session_id = None
        session_generation += 1
        current_ui_state = None
        rejected_actions = reject_pending_ui_actions(
            pending_ui_actions, "client_disconnected"
        )
        context.set_messages([])
        context.set_tools([])
        context.set_tool_choice("auto")
        await demo_controller.disconnect()
        logger.opt(colors=True).info(
            "<magenta>[SESSION]</magenta> Disconnected {} generation {}; settled {} actions",
            disconnected_session,
            session_generation,
            rejected_actions,
        )
        if profile:
            profile.end_session("disconnected")
        await worker.cancel()

    runner = WorkerRunner(handle_sigint=runner_args.handle_sigint)
    await runner.add_workers(worker)
    await runner.run()


async def bot(runner_args: RunnerArguments) -> None:
    configure_logging(debug_logs_enabled(runner_args))
    profile = create_local_profile()
    try:
        transport = await create_transport(runner_args, TRANSPORT_PARAMS)
        await run_bot(transport, runner_args, profile)
    finally:
        if profile:
            profile.close()


if __name__ == "__main__":
    from pipecat.runner.run import main

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--debug-logs",
        action="store_true",
        help="Show detailed conversation, UI, and latency diagnostics.",
    )
    main(parser)
