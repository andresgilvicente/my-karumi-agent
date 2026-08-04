"""Latency instrumentation.

Records one span per stage of a turn, writes them as JSONL, and reports
distributions rather than a single flattering average. P50/P90/P95 because the
worst turns are what an audience notices: on this hardware a cold prompt prefix
costs ~7 s against a warm ~1 s, and a mean would hide that entirely.
"""

from __future__ import annotations

import json
import statistics
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# The stages we care about, in the order they occur within a turn.
STAGES = (
    "vad_stop",  # user stopped speaking
    "stt",  # audio -> text
    "snapshot",  # observing the page
    "llm_ttft",  # first token from the model
    "llm_total",  # complete structured response
    "action",  # performing it
    "verify",  # confirming it
    "tts_ttfa",  # first audio out
    "turn_total",  # end of user speech -> everything done
)


@dataclass(slots=True)
class Span:
    """One measured stage."""

    stage: str
    seconds: float
    turn_id: str
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Percentiles:
    """A distribution summary."""

    count: int
    p50: float
    p90: float
    p95: float
    minimum: float
    maximum: float
    mean: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "p50": round(self.p50, 3),
            "p90": round(self.p90, 3),
            "p95": round(self.p95, 3),
            "min": round(self.minimum, 3),
            "max": round(self.maximum, 3),
            "mean": round(self.mean, 3),
        }


def percentiles(values: list[float]) -> Percentiles | None:
    """Summarise a sample.

    Uses nearest-rank rather than interpolation: with the handful of turns a demo
    benchmark produces, an interpolated P95 invents a value nobody observed.
    """
    if not values:
        return None
    ordered = sorted(values)

    def at(fraction: float) -> float:
        index = max(0, min(len(ordered) - 1, round(fraction * len(ordered) + 0.5) - 1))
        return ordered[index]

    return Percentiles(
        count=len(ordered),
        p50=at(0.50),
        p90=at(0.90),
        p95=at(0.95),
        minimum=ordered[0],
        maximum=ordered[-1],
        mean=statistics.fmean(ordered),
    )


class Timeline:
    """Collects spans across a session."""

    def __init__(self, *, path: Path | None = None) -> None:
        self._spans: list[Span] = []
        self._path = path
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def spans(self) -> list[Span]:
        return list(self._spans)

    def record(self, stage: str, seconds: float, *, turn_id: str = "", detail: str = "") -> None:
        span = Span(stage=stage, seconds=seconds, turn_id=turn_id, detail=detail)
        self._spans.append(span)
        if self._path is not None:
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(span.as_dict()) + "\n")

    @contextmanager
    def measure(self, stage: str, *, turn_id: str = "", detail: str = "") -> Iterator[None]:
        """Time a block and record it."""
        started = time.perf_counter()
        try:
            yield
        finally:
            self.record(stage, time.perf_counter() - started, turn_id=turn_id, detail=detail)

    def by_stage(self) -> dict[str, list[float]]:
        grouped: dict[str, list[float]] = {}
        for span in self._spans:
            grouped.setdefault(span.stage, []).append(span.seconds)
        return grouped

    def report(self) -> dict[str, dict[str, Any]]:
        """Percentiles per stage, in canonical stage order."""
        grouped = self.by_stage()
        report: dict[str, dict[str, Any]] = {}
        for stage in STAGES:
            values = grouped.get(stage)
            if not values:
                continue
            summary = percentiles(values)
            if summary is not None:
                report[stage] = summary.as_dict()
        # Anything recorded that is not a canonical stage still gets reported.
        for stage, values in grouped.items():
            if stage not in report:
                summary = percentiles(values)
                if summary is not None:
                    report[stage] = summary.as_dict()
        return report


@dataclass(slots=True)
class BenchmarkResult:
    """Outcome of a benchmark run, ready to print or serialise."""

    machine: str
    model: str
    turns: int
    stages: dict[str, dict[str, Any]] = field(default_factory=dict)
    successes: int = 0
    verified: int = 0
    invalid_refs: int = 0
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "machine": self.machine,
            "model": self.model,
            "turns": self.turns,
            "successes": self.successes,
            "verified": self.verified,
            "invalid_refs": self.invalid_refs,
            "stages": self.stages,
            "notes": self.notes,
        }
