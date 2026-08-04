"""The demo session: everything wired together.

Owns the browser, the observation loop, the planner, background site learning and
the autonomous tour, and exposes one method the voice layer or the CLI can drive:
``say(utterance) -> Turn``.

The startup sequence is the part worth reading. Learning, browser launch and
model warmup all begin **before** the greeting finishes, because the greeting is
free wall-clock time that would otherwise be spent staring at a blank page. By
the time the operator's first question lands, the site map has pages in it and
the model's prompt prefix is hot.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from karumi_agent.agent import prompts
from karumi_agent.agent.knowledge import KnowledgeWorker
from karumi_agent.agent.planner import Planner, Turn
from karumi_agent.browser.executor import ActionExecutor
from karumi_agent.browser.overlay import OverlayState
from karumi_agent.browser.session import BrowserSession
from karumi_agent.browser.snapshotter import Snapshotter
from karumi_agent.config import Settings
from karumi_agent.domain.demo import EditorialConfig, load_editorial_config
from karumi_agent.llm.ollama import OllamaClient
from karumi_agent.logging import get_logger
from karumi_agent.metrics.timeline import Timeline

log = get_logger("session")

SpeechSink = Callable[[str], Any]


@dataclass(slots=True)
class SessionStatus:
    """A snapshot of what the session is doing, for the control page."""

    url: str = ""
    title: str = ""
    state: str = "idle"
    learned_pages: int = 0
    learning: bool = False
    awaiting_login: bool = False
    last_say: str = ""
    pending_confirmation: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "state": self.state,
            "learned_pages": self.learned_pages,
            "learning": self.learning,
            "awaiting_login": self.awaiting_login,
            "last_say": self.last_say,
            "pending_confirmation": self.pending_confirmation,
        }


class DemoSession:
    """One live demo."""

    def __init__(
        self,
        settings: Settings,
        *,
        url: str | None = None,
        editorial: EditorialConfig | None = None,
        timeline: Timeline | None = None,
        headless: bool | None = None,
    ) -> None:
        self.settings = settings
        self.url = url or settings.demo.url
        self.timeline = timeline or Timeline(path=None)
        self.status = SessionStatus(url=self.url)

        if headless is not None:
            settings.browser.headless = headless

        self.browser = BrowserSession(
            settings.browser,
            allowed_origins=[self.url, *settings.browser.extra_allowed_origins],
            restrict_to_origin=settings.safety.restrict_to_origin,
        )
        self.snapshotter = Snapshotter(self.browser, settings.snapshot)
        self.executor = ActionExecutor(self.browser, self.snapshotter, settings.safety)
        self.llm = OllamaClient(settings.llm)
        self.knowledge = KnowledgeWorker(
            max_pages=settings.demo.learn_max_pages,
            max_depth=settings.demo.learn_max_depth,
            budget_s=settings.demo.learn_budget_s,
        )
        self.editorial = (
            editorial
            if editorial is not None
            else load_editorial_config(settings.demo.editorial_config)
        )
        self.planner = Planner(
            settings=settings,
            llm=self.llm,
            snapshotter=self.snapshotter,
            executor=self.executor,
            editorial=self.editorial,
        )
        self._tour_index = 0
        #: Built once at startup. Loading Parakeet takes ~15 s warm (and minutes
        #: on the very first run, while it downloads), so building it lazily
        #: inside the WebRTC offer handler made "Start talking" hang with no
        #: feedback. See start().
        self.stt: Any = None
        self.tts: Any = None
        self._login_watch: asyncio.Task[None] | None = None

    # ------------------------------------------------------------- lifecycle
    async def start(self, *, on_speech: SpeechSink | None = None) -> None:
        """Open the demo.

        Learning and warmup run concurrently with the greeting, which is the
        whole point of greeting first.
        """
        started = time.perf_counter()

        # Kick off site learning immediately: it needs no browser and no model,
        # so it can run while everything else is still starting.
        self.knowledge.start(self.url)

        await self.browser.start()
        warm = asyncio.create_task(self.planner.warmup())

        if on_speech is not None:
            await _emit(on_speech, prompts.GREETING)
        self.status.last_say = prompts.GREETING

        await self.browser.goto(self.url)
        await self.snapshotter.capture()
        await _await_quietly(warm)

        # Prefill the first page's state so the opening question is not the one
        # that pays cold prefill.
        await self.planner.prewarm_page()

        # Load the speech models now, while the operator is still reading the
        # console, rather than when they click "Start talking".
        await self._load_speech_models()

        self._note_login_wall(on_speech)
        self.refresh_status()
        self.timeline.record("session_start", time.perf_counter() - started)
        log.info(
            "session ready",
            url=self.browser.page.url,
            learned=self.knowledge.knowledge.page_count if self.knowledge.knowledge else 0,
            seconds=round(time.perf_counter() - started, 2),
        )

    async def stop(self) -> None:
        if self._login_watch is not None and not self._login_watch.done():
            self._login_watch.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._login_watch
        await self.knowledge.stop()
        await self.planner.aclose()
        await self.llm.aclose()
        await self.browser.stop()

    async def _load_speech_models(self) -> None:
        """Build the speech services up front.

        Failures are not fatal: the console still works by typing, which is a far
        better outcome than refusing to start because a model is missing.
        """
        from karumi_agent.stt.factory import build_stt
        from karumi_agent.tts.factory import build_tts

        started = time.perf_counter()
        try:
            self.stt = await asyncio.to_thread(build_stt, self.settings.stt)
            self.tts = await asyncio.to_thread(build_tts, self.settings.tts)
            log.info("speech models ready", seconds=round(time.perf_counter() - started, 1))
        except Exception as exc:  # degrade to text, do not die
            log.warning(f"speech models unavailable, voice disabled: {exc}")
            self.stt = self.tts = None

    @property
    def voice_ready(self) -> bool:
        return self.stt is not None and self.tts is not None

    def _note_login_wall(self, on_speech: SpeechSink | None) -> None:
        """Notice a sign-in wall without blocking on it.

        This used to await the operator signing in, for up to ten minutes,
        *before* the console started serving -- so a site with a login page made
        the whole app look hung. Now we say what we see, carry on, and let a
        background watcher notice when the wall clears.

        The agent still never types a credential.
        """
        if not self.snapshotter.store.current:
            return
        if not self.snapshotter.store.current.has_password_field:
            return

        self.status.awaiting_login = True
        log.info("sign-in wall detected; continuing without blocking")
        if on_speech is not None:
            asyncio.create_task(_emit(on_speech, prompts.LOGIN_WALL))  # noqa: RUF006
        self._login_watch = asyncio.create_task(self._watch_for_login(on_speech))

    async def _watch_for_login(self, on_speech: SpeechSink | None) -> None:
        """Poll quietly until the sign-in wall goes away."""
        try:
            await self.browser.overlay.set_state(self.browser.page, OverlayState.WAITING_LOGIN)
            cleared = await self.browser.wait_for_login(timeout_s=1800.0)
            self.status.awaiting_login = False
            if not cleared:
                return
            await self.snapshotter.capture()
            await self.planner.prewarm_page()
            if on_speech is not None:
                await _emit(on_speech, prompts.LOGIN_CLEARED)
            await self.browser.overlay.set_caption(self.browser.page, "")
            self.refresh_status()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # background watcher
            log.debug(f"login watch ended: {exc}")

    # ------------------------------------------------------------------ turn
    async def say(self, utterance: str, *, on_speech: SpeechSink | None = None) -> Turn:
        """Handle one operator turn."""
        page = self.browser.page
        await self.browser.overlay.set_state(page, OverlayState.THINKING)

        turn_started = time.perf_counter()
        with self.timeline.measure("snapshot"):
            await self.snapshotter.capture()

        first_speech: list[float] = []

        async def sink(text: str) -> None:
            if not first_speech:
                first_speech.append(time.perf_counter() - turn_started)
                await self.browser.overlay.set_state(page, OverlayState.SPEAKING)
            if on_speech is not None:
                await _emit(on_speech, text)

        turn = await self.planner.handle(utterance, on_speech=sink)

        if first_speech:
            self.timeline.record("tts_ttfa", first_speech[0], detail="time to first speech")
        if turn.timing.ttft_s is not None:
            self.timeline.record("llm_ttft", turn.timing.ttft_s)
        self.timeline.record("llm_total", turn.timing.total_s)
        self.timeline.record("turn_total", time.perf_counter() - turn_started)

        await self.browser.overlay.set_caption(page, turn.say)
        self.refresh_status(turn)
        return turn

    async def next_tour_beat(self, *, on_speech: SpeechSink | None = None) -> Turn:
        """Advance the autonomous tour by one beat.

        Beats are chosen from the live page, never from a script. Editorial
        themes act as a soft prior; if a theme is not present on the page, the
        model has nothing to point at and moves on.
        """
        self._tour_index += 1
        await self.browser.overlay.set_state(self.browser.page, OverlayState.THINKING)
        await self.snapshotter.capture()
        turn = await self.planner.handle("", on_speech=on_speech, autonomous=True)
        await self.browser.overlay.set_caption(self.browser.page, turn.say)
        self.refresh_status(turn)
        return turn

    # ---------------------------------------------------------------- status
    def refresh_status(self, turn: Turn | None = None) -> None:
        snapshot = self.snapshotter.store.current
        if snapshot is not None:
            self.status.url = snapshot.url
            self.status.title = snapshot.title
        if self.knowledge.knowledge is not None:
            self.status.learned_pages = self.knowledge.knowledge.page_count
            self.status.learning = self.knowledge.is_running
        if turn is not None:
            self.status.last_say = turn.say
        pending = self.planner.pending_confirmation
        self.status.pending_confirmation = pending.describe() if pending else None

    def site_knowledge_block(self) -> str:
        return self.knowledge.knowledge.as_prompt_block() if self.knowledge.knowledge else ""


async def _emit(sink: SpeechSink, text: str) -> None:
    result = sink(text)
    if hasattr(result, "__await__"):
        await result


async def _await_quietly(task: asyncio.Task[Any]) -> None:
    """Await a task, swallowing failures that must not stop a demo."""
    with contextlib.suppress(Exception):
        await task
