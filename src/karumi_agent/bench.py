"""Reproducible latency benchmark.

Runs a fixed question set against the local hostile fixture so results do not
depend on the network, on a third-party site being up, or on being signed in.

It reports **P50/P90/P95, not a mean.** On this hardware a turn whose prompt
prefix is warm costs about a second and one whose prefix is cold costs seven; an
average would report four and describe neither. The tail is what an audience
experiences.
"""

from __future__ import annotations

import platform
import shutil
import tempfile
import time
from pathlib import Path

from karumi_agent.agent.session import DemoSession
from karumi_agent.config import Settings
from karumi_agent.hardware import detect_hardware
from karumi_agent.logging import get_logger
from karumi_agent.metrics.timeline import BenchmarkResult, Timeline
from karumi_agent.server.fixture import FixtureServer

log = get_logger("bench")

# A deliberate mix: pure questions, a pointing request, a navigation, and a
# destructive request that must stop at the confirmation gate.
QUESTIONS = (
    "What does this page show?",
    "What is the revenue?",
    "Show me the conversion rate",
    "Take me to the analytics section",
    "Who is on the team?",
    "Click the delete workspace button",
)


def _machine_description() -> str:
    profile = detect_hardware()
    gpu = ", ".join(g.name for g in profile.gpus) or "no GPU"
    return (
        f"{profile.cpu_model}, {profile.physical_cores}C/{profile.logical_cores}T, "
        f"{profile.total_ram_mb} MB RAM, {gpu}, {profile.platform}"
    )


async def run_benchmark(
    settings: Settings,
    *,
    url: str | None = None,
    repetitions: int = 3,
) -> BenchmarkResult:
    """Run the benchmark and return a summary."""
    server: FixtureServer | None = None
    target = url
    if target is None:
        server = FixtureServer()
        target = server.start()

    profile_dir = Path(tempfile.mkdtemp(prefix="karumi-bench-profile-"))
    download_dir = Path(tempfile.mkdtemp(prefix="karumi-bench-downloads-"))
    settings.browser.profile_dir = profile_dir
    settings.browser.download_dir = download_dir

    timeline = Timeline(path=settings.state_dir / "logs" / "bench.jsonl")
    result = BenchmarkResult(
        machine=_machine_description(),
        model=settings.llm.effective_ui_model,
        turns=0,
    )

    session = DemoSession(settings, url=target, timeline=timeline, headless=True)
    try:
        started = time.perf_counter()
        await session.start()
        result.notes.append(f"session start: {time.perf_counter() - started:.2f}s")

        for _ in range(repetitions):
            for question in QUESTIONS:
                turn = await session.say(question)
                result.turns += 1
                if turn.result is not None:
                    if turn.result.succeeded:
                        result.successes += 1
                    if turn.verified:
                        result.verified += 1
                # The ref enum makes invalid refs inexpressible; count them so a
                # regression in that mechanism shows up as a number, not a vibe.
                if turn.repaired and "not on the page" in turn.repaired:
                    result.invalid_refs += 1

        result.stages = timeline.report()
        result.notes.append(
            "Turns whose prompt prefix was already warm are ~1s; a cold prefix "
            "costs ~7s on CPU. Both are in the distribution."
        )
    finally:
        await session.stop()
        if server is not None:
            server.stop()
        shutil.rmtree(profile_dir, ignore_errors=True)
        shutil.rmtree(download_dir, ignore_errors=True)

    log.info("benchmark complete", turns=result.turns, verified=result.verified)
    return result


def python_description() -> str:
    return f"Python {platform.python_version()}"
