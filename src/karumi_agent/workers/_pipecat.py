"""Import shim for Pipecat, and the single place that knows why it exists.

NLTK 3.10 installs a meta-path finder (`nltk/inisec.py`, a CWE-427 mitigation)
that blocks any NLTK-initiated import of a module resolving *inside the current
working directory*. `uv` creates its virtualenv at `.venv/` inside the project,
so from the project root every installed package resolves inside the cwd — and
Pipecat imports NLTK, which imports `regex`, which is duly blocked:

    ImportError: Blocked import of regex from current working directory
    for security reasons.

Nothing is actually being hijacked; the guard cannot distinguish a project-local
site-packages from an attacker-controlled cwd module. Pipecat requires
`nltk>=3.10`, so there is no version to pin away from, and relocating the
virtualenv outside the project would impose an unusual layout on everyone who
checks this repo out.

The fix is to import the affected module ourselves first. A module already in
`sys.modules` is returned from cache without consulting meta-path finders, so
NLTK's guard never sees it. Importing Pipecat *through* this module makes the
ordering explicit and impossible to break by reordering imports elsewhere.

`tests/unit/test_pipecat_compat.py` imports this from the project root, so if a
future NLTK release blocks something else the failure surfaces as a red test
rather than as a voice pipeline that will not start.
"""

from __future__ import annotations

import regex as _regex  # noqa: F401  # must precede pipecat; see module docstring
from pipecat.bus.ui.messages import (
    _UI_SNAPSHOT_BUS_EVENT_NAME,
    BusUICommandMessage,
    BusUIEventMessage,
)
from pipecat.pipeline.job_context import JobStatus
from pipecat.pipeline.job_decorator import job
from pipecat.workers.base_worker import BaseWorker
from pipecat.workers.llm.tool_decorator import tool
from pipecat.workers.runner import WorkerRunner
from pipecat.workers.ui import UI_STATE_PROMPT_GUIDE, ReplyToolMixin, UIWorker

#: Re-exported so the rest of the codebase never imports the private name from
#: Pipecat directly. This is the one place that depends on it, which keeps the
#: blast radius of an upstream rename to a single module and one failing test.
UI_SNAPSHOT_EVENT: str = _UI_SNAPSHOT_BUS_EVENT_NAME

__all__ = [
    "UI_SNAPSHOT_EVENT",
    "UI_STATE_PROMPT_GUIDE",
    "BaseWorker",
    "BusUICommandMessage",
    "BusUIEventMessage",
    "JobStatus",
    "ReplyToolMixin",
    "UIWorker",
    "WorkerRunner",
    "job",
    "tool",
]
