import asyncio
from collections.abc import Callable

from loguru import logger
from pipecat.frames.frames import (
    InterruptionFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from ui_control.guided_demo import GuidedDemoController, GuidedDemoDefinition, resolve_demo_intent
from ui_control.turns import ConsumedUserTurnFrame


class GuidedDemoProcessor(FrameProcessor):
    """Consume only deterministic demo controls and pass all other turns through."""

    def __init__(
        self,
        controller: GuidedDemoController,
        definition: GuidedDemoDefinition,
        on_resolution: Callable[[str, dict], None] | None = None,
        on_control_text: Callable[[str], None] | None = None,
    ):
        super().__init__()
        self._controller = controller
        self._definition = definition
        self._on_resolution = on_resolution
        self._on_control_text = on_control_text
        self._transcript_timeout_task: asyncio.Task | None = None
        self._transcript_generation = 0
        self._user_turn_generation = 0

    @property
    def user_turn_generation(self) -> int:
        return self._user_turn_generation

    async def cleanup(self):
        await super().cleanup()
        self._invalidate_transcript_timeout()

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, VADUserStartedSpeakingFrame):
            self._user_turn_generation += 1
            self._invalidate_transcript_timeout()
            self._controller.on_user_started_speaking()
            await self.push_frame(frame, direction)
            return
        if isinstance(frame, VADUserStoppedSpeakingFrame):
            self._schedule_transcript_timeout()
            await self.push_frame(frame, direction)
            return
        if isinstance(frame, InterruptionFrame):
            self._controller.on_user_started_speaking()
            await self.push_frame(frame, direction)
            return
        if not isinstance(frame, TranscriptionFrame):
            await self.push_frame(frame, direction)
            return

        self._invalidate_transcript_timeout()
        intent = resolve_demo_intent(frame.text, self._definition)
        if intent and await self._controller.handle_control(intent, frame.text):
            logger.opt(colors=True).info(
                "<magenta>[DEMO]</magenta> Control intent: {}", intent
            )
            if self._on_resolution:
                self._on_resolution("deterministic", {"command": intent})
            if self._on_control_text:
                self._on_control_text(frame.text)
            await self.push_frame(
                ConsumedUserTurnFrame(generation=self._user_turn_generation),
                FrameDirection.DOWNSTREAM,
            )
            return

        self._controller.on_normal_transcription()
        await self.push_frame(frame, direction)

    def _schedule_transcript_timeout(self) -> None:
        self._invalidate_transcript_timeout()
        generation = self._transcript_generation
        turn_generation = self._user_turn_generation
        task = asyncio.create_task(
            self._handle_transcript_timeout(generation, turn_generation)
        )
        self._transcript_timeout_task = task

    def _invalidate_transcript_timeout(self) -> None:
        self._transcript_generation += 1
        if self._transcript_timeout_task and not self._transcript_timeout_task.done():
            self._transcript_timeout_task.cancel()
        self._transcript_timeout_task = None

    async def _handle_transcript_timeout(
        self,
        generation: int,
        turn_generation: int,
    ) -> None:
        try:
            await asyncio.sleep(self._definition.transcript_grace_secs)
            if (
                generation != self._transcript_generation
                or self._transcript_timeout_task is not asyncio.current_task()
            ):
                return
            await self.push_frame(
                ConsumedUserTurnFrame(generation=turn_generation),
                FrameDirection.DOWNSTREAM,
            )
            await self._controller.on_transcription_timeout()
        except asyncio.CancelledError:
            pass
        finally:
            if self._transcript_timeout_task is asyncio.current_task():
                self._transcript_timeout_task = None
