"""A local HTTP server for the hostile test fixture.

Exists so the whole browser layer can be exercised offline, deterministically,
with no dependency on a third-party site being up, logged in, or unchanged. It
serves the fixture with SPA-style fallback so client-side routes resolve.
"""

from __future__ import annotations

import contextlib
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


def fixture_root() -> Path:
    """Locate the fixture directory, whether running from a checkout or a wheel."""
    candidate = Path(__file__).resolve().parents[3] / "fixtures" / "hostile_site"
    if candidate.is_dir():
        return candidate
    # Installed layout: fixtures ship alongside the package.
    packaged = Path(__file__).resolve().parent / "fixtures" / "hostile_site"
    if packaged.is_dir():
        return packaged
    raise FileNotFoundError("Could not locate fixtures/hostile_site")


class _SPAHandler(SimpleHTTPRequestHandler):
    """Serves real files, falling back to index.html for unknown paths."""

    def translate_path(self, path: str) -> str:
        translated = Path(super().translate_path(path))
        if translated.is_file():
            return str(translated)
        if translated.is_dir() and (translated / "index.html").is_file():
            return str(translated / "index.html")
        # Client-side route: hand back the shell, exactly like a real SPA host.
        return str(Path(self.directory) / "index.html")

    def log_message(self, format: str, *args: Any) -> None:
        """Silence per-request logging; the test output stays readable."""


class FixtureServer:
    """A background HTTP server for the fixture site."""

    def __init__(self, *, host: str = "127.0.0.1", port: int = 0) -> None:
        self._root = fixture_root()
        handler = partial(_SPAHandler, directory=str(self._root))
        self._server = ThreadingHTTPServer((host, port), handler)
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def base_url(self) -> str:
        raw_host = self._server.server_address[0]
        host = raw_host.decode() if isinstance(raw_host, bytes) else str(raw_host)
        return f"http://{host}:{self.port}"

    def start(self) -> str:
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self.base_url

    def stop(self) -> None:
        with contextlib.suppress(Exception):
            self._server.shutdown()
        with contextlib.suppress(Exception):
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def __enter__(self) -> FixtureServer:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
