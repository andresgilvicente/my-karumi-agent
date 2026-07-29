from collections.abc import Callable

from loguru import logger
from pipecat.frames.frames import TranscriptionFrame
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
        self._user_turn_generation = 0

    @property
    def user_turn_generation(self) -> int:
        return self._user_turn_generation

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if not isinstance(frame, TranscriptionFrame):
            await self.push_frame(frame, direction)
            return

        self._user_turn_generation += 1
        self._controller.on_user_started_speaking()
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
