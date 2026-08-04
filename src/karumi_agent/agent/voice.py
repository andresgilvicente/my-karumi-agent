"""The voice pipeline.

Pipecat handles the parts that are genuinely hard: WebRTC transport, voice
activity detection, semantic turn detection, barge-in, and streaming synthesis.
What it does *not* handle is reasoning over a third-party page, so the pipeline
here is deliberately unusual: there is no LLM service in it.

Instead a small processor intercepts each finished transcription, hands it to the
``DemoSession`` (browser observation, schema-constrained planning, action,
verification), and pushes the resulting speech straight to text-to-speech. That
keeps the design's central property -- **one model inference per turn** -- which
at ~13 tok/s on this CPU is the difference between a conversation and a wait.

Barge-in works because Pipecat emits an ``InterruptionFrame`` when the user
starts speaking, and we cancel the in-flight turn on it. Both VAD and Smart Turn
v3 run locally from models bundled inside the Pipecat wheel: no downloads, no
keys, ~11 MB in total.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

from karumi_agent.agent.session import DemoSession
from karumi_agent.browser.overlay import OverlayState
from karumi_agent.config import Settings
from karumi_agent.logging import get_logger
from karumi_agent.stt.factory import build_stt
from karumi_agent.tts.factory import build_tts

if TYPE_CHECKING:
    from pipecat.frames.frames import Frame
    from pipecat.processors.frame_processor import FrameDirection

log = get_logger("voice")


def _pipecat() -> dict[str, Any]:
    """Import Pipecat lazily and return the pieces we use.

    Deferred so `karumi doctor` and the text-only paths still work on a machine
    where the voice extras are missing, rather than failing at import time.
    """
    from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
    from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.audio.vad.vad_analyzer import VADParams
    from pipecat.frames.frames import InterruptionFrame, TranscriptionFrame, TTSSpeakFrame
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.worker import PipelineParams, PipelineWorker
    from pipecat.processors.audio.vad_processor import VADProcessor
    from pipecat.processors.frame_processor import FrameProcessor
    from pipecat.turns.user_start import VADUserTurnStartStrategy
    from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy
    from pipecat.turns.user_turn_processor import UserTurnProcessor
    from pipecat.turns.user_turn_strategies import UserTurnStrategies

    return {
        "SmartTurnParams": SmartTurnParams,
        "LocalSmartTurnAnalyzerV3": LocalSmartTurnAnalyzerV3,
        "SileroVADAnalyzer": SileroVADAnalyzer,
        "VADParams": VADParams,
        "InterruptionFrame": InterruptionFrame,
        "TranscriptionFrame": TranscriptionFrame,
        "TTSSpeakFrame": TTSSpeakFrame,
        "Pipeline": Pipeline,
        "PipelineParams": PipelineParams,
        "PipelineWorker": PipelineWorker,
        "VADProcessor": VADProcessor,
        "FrameProcessor": FrameProcessor,
        "VADUserTurnStartStrategy": VADUserTurnStartStrategy,
        "TurnAnalyzerUserTurnStopStrategy": TurnAnalyzerUserTurnStopStrategy,
        "UserTurnProcessor": UserTurnProcessor,
        "UserTurnStrategies": UserTurnStrategies,
    }


def build_turn_processor(session: DemoSession, pc: dict[str, Any]) -> Any:
    """Create the processor that turns transcriptions into demo turns."""
    # These are classes, so CamelCase is correct despite being locals.
    FrameProcessor = pc["FrameProcessor"]  # noqa: N806
    TranscriptionFrame = pc["TranscriptionFrame"]  # noqa: N806
    TTSSpeakFrame = pc["TTSSpeakFrame"]  # noqa: N806
    InterruptionFrame = pc["InterruptionFrame"]  # noqa: N806

    class _TurnProcessor(FrameProcessor):  # type: ignore[misc,valid-type]
        def __init__(self) -> None:
            super().__init__()
            self._session = session
            self._task: asyncio.Task[None] | None = None

        async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
            await super().process_frame(frame, direction)

            # Barge-in: drop whatever we were about to say and stop working on it.
            # Cancelling here is what makes interruption feel immediate rather
            # than "finishes the sentence first".
            if isinstance(frame, InterruptionFrame):
                await self._cancel()
                await self.push_frame(frame, direction)
                return

            if isinstance(frame, TranscriptionFrame):
                text = (frame.text or "").strip()
                if text:
                    await self._cancel()
                    self._task = asyncio.create_task(self._handle(text))
                return

            await self.push_frame(frame, direction)

        async def _handle(self, text: str) -> None:
            log.info("heard", text=text[:160])
            try:
                await self._session.browser.overlay.set_state(
                    self._session.browser.page, OverlayState.THINKING
                )
                turn = await self._session.say(text)
                if turn.say:
                    await self.push_frame(TTSSpeakFrame(turn.say))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # a bad turn must not kill the call
                log.warning(f"turn failed: {type(exc).__name__}: {exc}")
                await self.push_frame(
                    TTSSpeakFrame("Sorry, something went wrong there. Could you try again?")
                )

        async def _cancel(self) -> None:
            if self._task is not None and not self._task.done():
                self._task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._task
            self._task = None

    return _TurnProcessor()


def build_vad_processor(settings: Settings, pc: dict[str, Any]) -> Any:
    """Voice activity detection.

    **This is load-bearing, not optional.** ``SegmentedSTTService`` reacts to
    exactly two frames -- ``VADUserStartedSpeakingFrame`` and
    ``VADUserStoppedSpeakingFrame`` -- and nothing else produces them. Without a
    VAD processor in the pipeline the microphone works, audio flows, and
    absolutely nothing is ever transcribed. That was a real bug here.

    Note that in Pipecat 1.7.0 ``TransportParams`` has no ``vad_analyzer``
    field; VAD is a pipeline stage, or lives on an LLM context aggregator we
    deliberately do not have.

    The Silero model is bundled in the wheel: 2.3 MB, no download, CPU only.
    """
    voice = settings.voice
    return pc["VADProcessor"](
        vad_analyzer=pc["SileroVADAnalyzer"](
            params=pc["VADParams"](
                confidence=voice.vad_confidence,
                start_secs=voice.vad_start_secs,
                stop_secs=voice.vad_stop_secs,
                min_volume=voice.vad_min_volume,
            )
        ),
    )


def build_user_turn_processor(settings: Settings, pc: dict[str, Any]) -> Any:
    """Semantic turn taking and barge-in.

    Starting a turn on voice activity rather than on a completed transcription
    is what makes interruption feel immediate. Stopping is handed to Smart Turn
    v3, which decides whether the user has actually finished a thought rather
    than just paused -- 8.7 MB, bundled in the wheel, ~12 ms per turn on CPU,
    and it removes the fixed silence tax that pure VAD imposes.
    """
    voice = settings.voice
    return pc["UserTurnProcessor"](
        user_turn_strategies=pc["UserTurnStrategies"](
            start=[pc["VADUserTurnStartStrategy"](enable_interruptions=voice.allow_interruptions)],
            stop=[
                pc["TurnAnalyzerUserTurnStopStrategy"](
                    turn_analyzer=pc["LocalSmartTurnAnalyzerV3"](
                        cpu_count=voice.turn_cpu_count,
                        params=pc["SmartTurnParams"](stop_secs=voice.turn_stop_secs),
                    )
                )
            ],
        ),
    )


def build_voice_pipeline(
    session: DemoSession,
    settings: Settings,
    transport: Any,
    *,
    stt: Any = None,
    tts: Any = None,
) -> Any:
    """Assemble the Pipecat pipeline and worker.

    Stage order matters:

        input -> VAD -> turn taking -> STT -> demo turn -> TTS -> output

    VAD must precede STT because it is what tells STT where an utterance begins
    and ends. Turn taking sits between them so an interruption is raised as
    early as possible.

    Note what is absent: no LLM service and no context aggregator. The demo
    session already owns the model, the page and the conversation, and routing
    the turn through a second Pipecat LLM would double the inference cost.

    ``stt`` and ``tts`` can be passed in already-constructed. The demo builds
    them at startup so that answering a WebRTC offer does not have to wait for
    model weights to load.
    """
    pc = _pipecat()

    stt = stt if stt is not None else build_stt(settings.stt)
    tts = tts if tts is not None else build_tts(settings.tts)

    pipeline = pc["Pipeline"](
        [
            transport.input(),
            build_vad_processor(settings, pc),
            build_user_turn_processor(settings, pc),
            stt,
            build_turn_processor(session, pc),
            tts,
            transport.output(),
        ]
    )

    return pc["PipelineWorker"](
        pipeline,
        name="karumi-voice",
        params=pc["PipelineParams"](
            enable_metrics=True,
            enable_usage_metrics=True,
            audio_in_sample_rate=16000,
            audio_out_sample_rate=settings.tts.sample_rate,
        ),
    )
