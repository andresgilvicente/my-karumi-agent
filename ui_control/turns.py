from dataclasses import dataclass

from pipecat.frames.frames import (
    ControlFrame,
    Frame,
    TranscriptionFrame,
    UninterruptibleFrame,
    VADUserStartedSpeakingFrame,
)
from pipecat.turns.types import ProcessFrameResult
from pipecat.turns.user_stop.base_user_turn_stop_strategy import BaseUserTurnStopStrategy


@dataclass
class ConsumedUserTurnFrame(ControlFrame, UninterruptibleFrame):
    """Close a consumed deterministic turn without adding it to the LLM context."""

    generation: int


class ConsumedUserTurnStopStrategy(BaseUserTurnStopStrategy):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._pending_finalization = False
        self._vad_generation = 0

    async def handle_user_turn_stopped(self):
        self._pending_finalization = False
        await super().handle_user_turn_stopped()

    async def process_frame(self, frame: Frame) -> ProcessFrameResult:
        if isinstance(frame, VADUserStartedSpeakingFrame):
            self._vad_generation += 1
            return ProcessFrameResult.CONTINUE
        if isinstance(frame, ConsumedUserTurnFrame):
            if frame.generation != self._vad_generation:
                return ProcessFrameResult.STOP
            self._pending_finalization = True
            await self.trigger_user_turn_stopped()
            return ProcessFrameResult.STOP
        if self._pending_finalization and isinstance(frame, TranscriptionFrame):
            # A rapid new utterance may overtake the finalizer while VAD still marks the user as
            # speaking. Retry only after its transcript has reached the aggregator, preserving it.
            await self.trigger_user_turn_stopped()
        return ProcessFrameResult.CONTINUE
