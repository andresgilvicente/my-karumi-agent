"""Structured logging.

Two sinks with different jobs: a human-readable console stream so a developer
can watch a demo happen, and an append-only JSONL file that is the input to the
latency report. Observations, decisions and actions all land in the JSONL, which
is what makes a run reconstructible after the fact.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, cast

import structlog

_configured = False


def configure(level: str = "INFO", log_dir: str = "logs", console: bool = True) -> Path:
    """Configure structlog once. Returns the path of the JSONL event log."""
    global _configured

    directory = Path(log_dir)
    directory.mkdir(parents=True, exist_ok=True)
    events_path = directory / "events.jsonl"

    if _configured:
        return events_path

    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    # The file handler always gets JSON; the console gets whatever is readable.
    file_handler = logging.FileHandler(events_path, encoding="utf-8")
    file_handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=structlog.processors.JSONRenderer(),
            foreign_pre_chain=shared,
        )
    )

    handlers: list[logging.Handler] = [file_handler]

    if console:
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                processor=structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty()),
                foreign_pre_chain=shared,
            )
        )
        handlers.append(console_handler)

    root = logging.getLogger()
    root.handlers = handlers
    root.setLevel(level.upper())

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    _configured = True
    return events_path


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return cast(structlog.stdlib.BoundLogger, structlog.get_logger(name))
