"""Regression guard for the NLTK/uv import collision.

NLTK 3.10 blocks NLTK-initiated imports of modules that resolve inside the
current working directory. `uv` puts its virtualenv at `.venv/` inside the
project, so from the project root that describes every installed package —
and Pipecat imports NLTK. Without the shim, importing any worker fails.

pytest runs from the project root, which is precisely the condition that
triggers it, so simply importing the shim here is the test. If a future NLTK
release blocks something beyond `regex`, this goes red instead of the voice
pipeline failing to start in front of an audience.
"""

from __future__ import annotations


def test_pipecat_imports_from_the_project_root() -> None:
    from karumi_agent.workers import _pipecat

    assert _pipecat.UIWorker is not None
    assert _pipecat.WorkerRunner is not None
    assert _pipecat.BaseWorker is not None


def test_snapshot_event_name_is_what_uiworker_listens_for() -> None:
    """The private constant we publish under. If upstream renames it, snapshots
    would still be sent, still be accepted by the bus, and silently never reach
    the UI worker — so it is asserted rather than trusted."""
    from karumi_agent.workers._pipecat import UI_SNAPSHOT_EVENT

    assert UI_SNAPSHOT_EVENT == "__ui_snapshot"


def test_workers_import_without_an_explicit_shim_import() -> None:
    """Importing a worker must be enough; ordering is the shim's problem."""
    from karumi_agent.workers.browser_worker import BrowserWorker
    from karumi_agent.workers.ui_worker import KarumiUIWorker

    assert BrowserWorker is not None
    assert KarumiUIWorker is not None
