import re
import time
from difflib import SequenceMatcher

from loguru import logger
from pipecat.frames.frames import InterruptionFrame, TranscriptionFrame, TTSSpeakFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


class BotEchoSuppressionProcessor(FrameProcessor):
    """Drop transcriptions that are probable acoustic echoes of deterministic speech."""

    def __init__(self, *, echo_linger_secs: float = 8.0, monotonic=time.monotonic):
        super().__init__()
        self._echo_linger_secs = echo_linger_secs
        self._monotonic = monotonic
        self._bot_words: tuple[str, ...] = ()
        self._bot_speaking = False
        self._expires_at = 0.0

    def on_bot_started_speaking(self) -> None:
        self._bot_speaking = True

    def on_bot_stopped_speaking(self) -> None:
        self._bot_speaking = False
        self._expires_at = self._monotonic() + self._echo_linger_secs

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)

        if isinstance(frame, TTSSpeakFrame) and direction == FrameDirection.DOWNSTREAM:
            self._bot_words = _words(frame.text)
            self._expires_at = 0.0
        elif isinstance(frame, TranscriptionFrame) and direction == FrameDirection.DOWNSTREAM:
            if self._is_probable_echo(frame.text):
                logger.opt(colors=True).warning(
                    "<cyan>[STT]</cyan> Suppressed probable bot echo: {}", frame.text.strip()
                )
                return
            await self.push_frame(InterruptionFrame(), direction)

        await self.push_frame(frame, direction)

    def _is_probable_echo(self, text: str) -> bool:
        candidate = _words(text)
        echo_window_closed = not self._bot_speaking and self._monotonic() > self._expires_at
        if echo_window_closed or len(candidate) < 3 or not self._bot_words:
            return False

        candidate_text = " ".join(candidate)
        source_text = " ".join(self._bot_words)
        if candidate_text in source_text:
            return True

        candidate_size = len(candidate)
        for window_size in range(max(3, candidate_size - 1), candidate_size + 2):
            for start in range(len(self._bot_words) - window_size + 1):
                window = " ".join(self._bot_words[start : start + window_size])
                if SequenceMatcher(None, candidate_text, window).ratio() >= 0.78:
                    return True
        return False


def _words(text: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[a-z0-9']+", text.casefold()))
