"""Karumi Agent: a fully local, free voice agent that demos websites live.

Importing this package applies process-level environment fixes (see
``_bootstrap``) before any third-party module is touched, so import order here
is deliberate: keep ``bootstrap()`` first.
"""

from __future__ import annotations

from karumi_agent._bootstrap import bootstrap

bootstrap()

__version__ = "0.1.0"

__all__ = ["__version__"]
