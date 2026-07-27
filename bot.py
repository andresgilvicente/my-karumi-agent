import time

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import LLMRunFrame, LLMTextFrame, TranscriptionFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.services.kokoro.tts import KokoroTTSService
from pipecat.services.ollama.llm import OLLamaLLMService
from pipecat.services.whisper.stt import WhisperSTTService
from pipecat.transcriptions.language import Language
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.workers.runner import WorkerRunner

# ---------------------------------------------------------------------------
# Config — change these to swap models / voices without touching the pipeline.
# ---------------------------------------------------------------------------
STT_MODEL = "base"
STT_LANGUAGE = Language.EN

LLM_MODEL = "qwen2.5:1.5b"

TTS_VOICE = "af_heart"
TTS_LANGUAGE = Language.EN

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

The interface-control tools are not connected yet. Do not claim to navigate or change the screen.
If asked to do so, explain briefly that voice conversation is available and screen control is the
next capability being connected. Always respond in English."""

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
# Latency logger — lightweight timing for STT → LLM first token.
# ---------------------------------------------------------------------------
class LatencyLogger(FrameProcessor):
    """Prints timing between transcription and first LLM token."""

    def __init__(self):
        super().__init__()
        self._stt_time: float | None = None

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)

        if isinstance(frame, TranscriptionFrame):
            self._stt_time = time.monotonic()
            print(f"[STT] '{frame.text}'")

        elif isinstance(frame, LLMTextFrame) and self._stt_time is not None:
            elapsed = time.monotonic() - self._stt_time
            print(f"[LLM] First token in {elapsed:.2f}s")
            self._stt_time = None

        await self.push_frame(frame, direction)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------
async def run_bot(transport: BaseTransport, runner_args: RunnerArguments) -> None:
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
            system_instruction=SYSTEM_INSTRUCTION,
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

    context = LLMContext()
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()),
    )

    latency_logger = LatencyLogger()

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            latency_logger,
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
        idle_timeout_secs=runner_args.pipeline_idle_timeout_secs,
    )

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        context.add_message(
            {
                "role": "system",
                "content": (
                    "Introduce yourself as Karumi Agent and offer a brief interactive tour of "
                    "Metric Master Suite. Invite the user to interrupt or ask questions."
                ),
            }
        )
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
