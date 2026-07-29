import unittest

from pipecat.clocks.system_clock import SystemClock
from pipecat.frames.frames import InterruptionFrame, TranscriptionFrame, TTSSpeakFrame
from pipecat.processors.frame_processor import FrameDirection

from voice_input import BotEchoSuppressionProcessor


class BotEchoSuppressionProcessorTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = 100.0
        self.processor = BotEchoSuppressionProcessor(monotonic=lambda: self.now)
        self.pushed = []

        async def push_frame(frame, direction=FrameDirection.DOWNSTREAM):
            self.pushed.append((frame, direction))

        self.processor.push_frame = push_frame

    async def introduce_agent(self):
        await self.processor.process_frame(
            TTSSpeakFrame(
                "Hi, I'm Karumi Agent. You can interrupt or ask questions at any time."
            ),
            FrameDirection.DOWNSTREAM,
        )
        self.processor.on_bot_started_speaking()

    def transcription(self, text):
        return TranscriptionFrame(
            text=text,
            user_id="user",
            timestamp="2026-07-29T00:00:00Z",
        )

    async def test_suppresses_an_exact_bot_echo(self):
        await self.introduce_agent()
        await self.processor.process_frame(
            self.transcription("You can interrupt or ask questions at any time."),
            FrameDirection.DOWNSTREAM,
        )
        self.assertEqual(len(self.pushed), 1)

    async def test_suppresses_a_near_match_from_whisper(self):
        await self.introduce_agent()
        await self.processor.process_frame(
            self.transcription("were asked questions at any time"),
            FrameDirection.DOWNSTREAM,
        )
        self.assertEqual(len(self.pushed), 1)

    async def test_preserves_real_and_short_user_speech(self):
        await self.introduce_agent()
        for text in ("What does the dashboard show?", "Continue"):
            await self.processor.process_frame(
                self.transcription(text),
                FrameDirection.DOWNSTREAM,
            )
        self.assertIsInstance(self.pushed[1][0], InterruptionFrame)
        self.assertEqual(self.pushed[2][0].text, "What does the dashboard show?")
        self.assertIsInstance(self.pushed[3][0], InterruptionFrame)
        self.assertEqual(self.pushed[4][0].text, "Continue")

    async def test_echo_filter_expires(self):
        await self.introduce_agent()
        self.processor.on_bot_stopped_speaking()
        self.now += 9
        echo = self.transcription("You can interrupt or ask questions at any time.")
        await self.processor.process_frame(echo, FrameDirection.DOWNSTREAM)
        self.assertIs(self.pushed[-1][0], echo)
        self.assertIsInstance(self.pushed[-2][0], InterruptionFrame)

    async def test_pipecat_system_clock_does_not_replace_the_monotonic_source(self):
        await self.introduce_agent()
        self.processor._clock = SystemClock()

        self.processor.on_bot_stopped_speaking()
        self.now += 9
        echo = self.transcription("You can interrupt or ask questions at any time.")
        await self.processor.process_frame(echo, FrameDirection.DOWNSTREAM)

        self.assertIs(self.pushed[-1][0], echo)
