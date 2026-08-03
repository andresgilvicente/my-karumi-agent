"""Command-line entry point.

Deliberately thin: argparse plus a call into the module that does the work, so
there is one obvious way to run each phase and nothing clever in between.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import http.server
import socketserver
import sys
from pathlib import Path

from karumi_agent.observability.logging import configure, get_logger
from karumi_agent.settings import get_settings

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "site"
DEFAULT_FIXTURE_PORT = 8899


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="karumi",
        description="Voice agent for live demos of web products it has no source code for.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    spike = sub.add_parser(
        "spike",
        help="Phase 1: drive a real browser and verify observation and action end to end.",
    )
    spike.add_argument("--url", required=True, help="Page to demo.")
    spike.add_argument(
        "--headless", action="store_true", help="Run without a visible window (CI)."
    )
    spike.add_argument(
        "--samples",
        type=int,
        default=20,
        help="Snapshots taken during the ref-stability probe (default: 20).",
    )
    spike.add_argument(
        "--show-ui-state",
        action="store_true",
        help="Print the <ui_state> block exactly as the model would receive it.",
    )

    fixture = sub.add_parser(
        "serve-fixture", help="Serve the bundled test site on localhost."
    )
    fixture.add_argument("--port", type=int, default=DEFAULT_FIXTURE_PORT)

    return parser


async def _run_spike(args: argparse.Namespace) -> int:
    from karumi_agent.spike import run_spike

    settings = get_settings()
    browser = settings.browser.model_copy(
        update={"headless": args.headless or settings.browser.headless}
    )

    report = await run_spike(
        args.url,
        browser,
        stability_samples=args.samples,
        show_ui_state=args.show_ui_state,
    )
    print(report.render())
    return 0 if report.passed else 1


def _serve_fixture(port: int) -> int:
    if not FIXTURE_DIR.exists():
        print(f"fixture directory not found: {FIXTURE_DIR}", file=sys.stderr)
        return 1

    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(FIXTURE_DIR))
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", port), handler) as httpd:
        print(f"fixture site on http://127.0.0.1:{port}  (ctrl-c to stop)")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    settings = get_settings()

    if args.command == "serve-fixture":
        return _serve_fixture(args.port)

    events_path = configure(
        level=settings.log.level, log_dir=settings.log.dir, console=settings.log.console
    )
    get_logger(__name__).info("cli.start", command=args.command, events_log=str(events_path))

    if args.command == "spike":
        return asyncio.run(_run_spike(args))

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
