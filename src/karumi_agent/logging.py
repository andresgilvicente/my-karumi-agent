"""Structured JSON logging.

Pipecat logs through loguru, so we configure loguru rather than stdlib logging and
get the framework's output in the same stream and the same format.

Two sinks:

* stderr, human-readable and colourised, for the operator watching a demo;
* an optional JSONL file, one object per line, for benchmarking and post-mortems.

The JSONL file is the artifact to send back when a validation run fails.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from loguru import Message

_HUMAN_FORMAT = (
    "<green>{time:HH:mm:ss.SSS}</green> "
    "<level>{level: <8}</level> "
    "<cyan>{extra[component]: <12}</cyan> "
    "<level>{message}</level>"
)


def _json_sink(path: Path) -> Any:
    """Build a loguru sink writing one JSON object per line."""

    def write(message: Message) -> None:
        record = message.record
        extra = dict(record["extra"])
        payload: dict[str, Any] = {
            "ts": record["time"].isoformat(),
            "level": record["level"].name,
            "component": extra.pop("component", "karumi"),
            "message": record["message"],
            "module": record["name"],
            "line": record["line"],
        }
        # Anything bound with logger.bind(...) becomes a top-level field, so
        # action_id / snapshot_id / duration_ms are queryable without parsing text.
        payload.update(extra)
        if record["exception"] is not None:
            exc = record["exception"]
            payload["exception"] = {
                "type": exc.type.__name__ if exc.type else None,
                "value": str(exc.value) if exc.value else None,
            }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, default=str) + "\n")

    return write


def configure_logging(
    *,
    level: str = "INFO",
    json_path: Path | None = None,
    quiet_console: bool = False,
) -> None:
    """Install our sinks, replacing loguru's default.

    Args:
        level: Minimum level for the console sink.
        json_path: When set, also write JSONL here. Parent dirs are created.
        quiet_console: Raise the console threshold to WARNING. Used by the voice
            demo so log spam does not scroll over the operator's transcript.
    """
    logger.remove()

    console_level = "WARNING" if quiet_console else level
    logger.configure(extra={"component": "karumi"})
    logger.add(
        sys.stderr,
        level=console_level,
        format=_HUMAN_FORMAT,
        backtrace=False,
        diagnose=False,
    )

    if json_path is not None:
        json_path.parent.mkdir(parents=True, exist_ok=True)
        # DEBUG on the file sink: the console stays readable while the artifact
        # keeps everything needed to reconstruct a failed run.
        logger.add(_json_sink(json_path), level="DEBUG", backtrace=False, diagnose=False)


def get_logger(component: str) -> Any:
    """Return a logger tagged with a component name."""
    return logger.bind(component=component)
