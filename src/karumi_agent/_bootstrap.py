"""Process-level environment fixes that must run before third-party imports.

Import this first, from ``karumi_agent/__init__.py``, so that anything reaching
Pipecat (and through it, nltk) has already been set up.
"""

from __future__ import annotations

import os


def _relax_nltk_cwd_guard() -> None:
    """Work around nltk >= 3.10's CWD import guard misfiring on uv layouts.

    nltk 3.10 installs a meta-path finder (``nltk/inisec.py``) that blocks any
    nltk-initiated import whose module file resolves *inside* the current working
    directory. The intent is to stop a malicious ``regex.py`` in your project
    directory from being picked up.

    The check is purely path-containment based, so it cannot tell "found via the
    CWD entry on sys.path" from "found in a virtualenv that happens to live in the
    project directory". uv puts the environment at ``<project>/.venv`` by default,
    which means every legitimate site-packages module is inside the CWD and gets
    blocked. Pipecat imports nltk at module scope (``pipecat/utils/string.py``), so
    ``import pipecat.workers.ui`` fails outright with:

        ImportError: Blocked import of regex from current working directory

    We keep the property that actually matters and drop only the heuristic:

    * ``PYTHONSAFEPATH=1`` removes the CWD from ``sys.path`` for real. This is the
      genuine protection, and it is what nltk's own error message recommends.
    * ``NLTK_DISABLE_IMPORT_SECURITY=1`` (nltk's documented opt-out) disables the
      containment heuristic that produces the false positive.

    Both are set only if the caller has not already chosen a value, so an operator
    can still override either one.

    Note that ``PYTHONSAFEPATH`` only affects ``sys.path`` construction at
    interpreter startup. Setting it here influences child processes, not our own
    already-built ``sys.path``; the nltk opt-out is what unblocks the current
    process. Both are set so parent and child behave consistently.
    """
    os.environ.setdefault("PYTHONSAFEPATH", "1")
    os.environ.setdefault("NLTK_DISABLE_IMPORT_SECURITY", "1")


def bootstrap() -> None:
    """Apply all process-level fixes. Safe to call more than once."""
    _relax_nltk_cwd_guard()
