"""Command line interface.

``karumi doctor`` is the entry point an operator should reach for first: it reports
what is present, what is missing, and the exact command to fix each gap.
"""

from __future__ import annotations

import contextlib
import sys

import typer
from rich.console import Console
from rich.table import Table

from karumi_agent.config import Settings, load_settings
from karumi_agent.hardware import Check, CheckStatus, detect_hardware, run_all_checks
from karumi_agent.logging import configure_logging

app = typer.Typer(
    name="karumi",
    help="Fully local voice agent that gives live demos of websites.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

_STATUS_LABEL = {
    CheckStatus.OK: "[green]OK[/green]",
    CheckStatus.WARN: "[yellow]WARN[/yellow]",
    CheckStatus.FAIL: "[red]FAIL[/red]",
}


def _bootstrap_settings(log_level: str | None = None) -> Settings:
    settings = load_settings()
    configure_logging(level=log_level or settings.log_level, json_path=settings.log_file)
    return settings


def _render_checks(checks: list[Check]) -> None:
    table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    table.add_column("", width=6)
    table.add_column("Check", style="cyan", no_wrap=True)
    table.add_column("Detail")

    for check in checks:
        table.add_row(_STATUS_LABEL[check.status], check.name, check.detail)
    console.print(table)

    remedies = [c for c in checks if c.remedy and c.status is not CheckStatus.OK]
    if remedies:
        console.print("\n[bold]To fix:[/bold]")
        for check in remedies:
            console.print(f"  [cyan]{check.name}[/cyan]: {check.remedy}")


@app.command()
def doctor(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Check hardware and local dependencies, and say how to fix what is missing."""
    settings = _bootstrap_settings()
    profile = detect_hardware(settings.state_dir)

    wanted_models = sorted({settings.llm.model, settings.llm.effective_ui_model})
    checks = run_all_checks(
        profile,
        ollama_url=settings.llm.native_url,
        models=wanted_models,
        stt_engine=settings.stt.engine,
    )

    if json_output:
        import json

        console.print_json(
            json.dumps({"hardware": profile.as_dict(), "checks": [c.as_dict() for c in checks]})
        )
    else:
        console.print("\n[bold]Hardware[/bold]")
        hw = Table(show_header=False, box=None, pad_edge=False)
        hw.add_column(style="cyan", no_wrap=True)
        hw.add_column()
        hw.add_row("platform", f"{profile.platform}{' (WSL)' if profile.is_wsl else ''}")
        hw.add_row(
            "cpu", f"{profile.cpu_model} - {profile.physical_cores}C/{profile.logical_cores}T"
        )
        hw.add_row(
            "memory",
            f"{profile.available_ram_mb} MB available / {profile.total_ram_mb} MB total",
        )
        hw.add_row("disk", f"{profile.free_disk_gb:.1f} GB free")
        hw.add_row(
            "gpu",
            ", ".join(f"{g.name} ({g.total_memory_mb} MB)" for g in profile.gpus)
            or "none detected",
        )
        hw.add_row("profile", f"[bold]{profile.profile_name}[/bold]")
        console.print(hw)

        console.print("\n[bold]Checks[/bold]")
        _render_checks(checks)

    failures = [c for c in checks if c.status is CheckStatus.FAIL]
    if failures:
        console.print(f"\n[red]{len(failures)} check(s) failed.[/red]")
        raise typer.Exit(code=1)
    console.print("\n[green]All required checks passed.[/green]")


@app.command()
def fixture(
    port: int = typer.Option(0, help="Port to serve on. 0 picks a free one."),
) -> None:
    """Serve the local hostile test site, for offline development."""
    from karumi_agent.server.fixture import FixtureServer

    _bootstrap_settings()
    server = FixtureServer(port=port)
    url = server.start()
    console.print(f"Hostile fixture serving at [cyan]{url}[/cyan]")
    console.print("Point a demo at it with:")
    console.print(f"  [white]uv run karumi demo --url {url}[/white]")
    console.print("\nPress Ctrl+C to stop.")
    try:
        import time as _time

        while True:
            _time.sleep(3600)
    except KeyboardInterrupt:
        server.stop()


@app.command()
def chat(
    url: str = typer.Option(None, help="URL to demo. Defaults to the configured one."),
    headless: bool = typer.Option(False, help="Hide the browser. Off for a real demo."),
) -> None:
    """Drive the agent by typing instead of speaking.

    The fastest way to see the whole loop -- observation, planning, action,
    verification -- without audio in the way.
    """
    import asyncio

    from karumi_agent.agent.session import DemoSession
    from karumi_agent.metrics.timeline import Timeline

    settings = _bootstrap_settings()
    timeline = Timeline(path=settings.state_dir / "logs" / "latency.jsonl")

    async def run() -> None:
        session = DemoSession(settings, url=url, timeline=timeline, headless=headless)
        try:
            console.print("[dim]Starting browser and learning the site...[/dim]")
            await session.start(on_speech=lambda t: console.print(f"[green]agent[/green] {t}"))
            console.print(
                f"\n[dim]Watching {session.status.url}[/dim]\n"
                "[dim]Type a question, 'tour' for a demo beat, or 'quit'.[/dim]\n"
            )
            while True:
                try:
                    line = console.input("[cyan]you[/cyan] ").strip()
                except (EOFError, KeyboardInterrupt):
                    break
                if not line:
                    continue
                if line.lower() in {"quit", "exit"}:
                    break

                turn = (
                    await session.next_tour_beat()
                    if line.lower() == "tour"
                    else await session.say(line)
                )
                console.print(f"[green]agent[/green] {turn.say}")
                if turn.action is not None and turn.result is not None:
                    mark = (
                        "[green]verified[/green]"
                        if turn.verified
                        else "[yellow]unverified[/yellow]"
                    )
                    console.print(
                        f"  [dim]{turn.action.describe()} -> {turn.result.status.value} "
                        f"({mark}, {turn.total_s:.1f}s)[/dim]"
                    )
                else:
                    console.print(f"  [dim]spoke only ({turn.total_s:.1f}s)[/dim]")
        finally:
            await session.stop()

    asyncio.run(run())


@app.command()
def demo(
    url: str = typer.Option(None, help="URL to demo. Defaults to the configured one."),
    autonomous: bool = typer.Option(False, help="Start the self-guided tour immediately."),
    headless: bool = typer.Option(False, help="Hide the browser. Off for a real demo."),
    port: int = typer.Option(None, help="Port for the operator console."),
) -> None:
    """Run the voice demo: real browser plus an operator console with a mic."""
    import asyncio

    import uvicorn

    from karumi_agent.agent.session import DemoSession
    from karumi_agent.metrics.timeline import Timeline
    from karumi_agent.server.app import build_app

    settings = _bootstrap_settings()
    if port is not None:
        settings.server.port = port
    timeline = Timeline(path=settings.state_dir / "logs" / "latency.jsonl")

    async def run() -> None:
        session = DemoSession(settings, url=url, timeline=timeline, headless=headless)
        console.print("[dim]Starting browser, warming the model, learning the site...[/dim]")
        await session.start(on_speech=lambda t: console.print(f"[green]agent[/green] {t}"))

        console.print(
            f"\n[bold]Operator console:[/bold] "
            f"[cyan]http://{settings.server.host}:{settings.server.port}[/cyan]"
        )
        console.print("[dim]Open it, click 'Start talking', and share the Chromium window.[/dim]")
        console.print("[dim]Press Ctrl+C to end the demo.[/dim]\n")

        api = build_app(settings, session)
        config = uvicorn.Config(
            api,
            host=settings.server.host,
            port=settings.server.port,
            log_level="warning",
        )
        server = uvicorn.Server(config)

        if autonomous:
            asyncio.create_task(_autonomous_loop(session))  # noqa: RUF006

        try:
            await server.serve()
        finally:
            # Stop the voice pipeline before the browser: a running pipeline
            # keeps the loop busy and makes Ctrl+C look like a hang.
            close_voice = getattr(api.state, "close_voice", None)
            if close_voice is not None:
                with contextlib.suppress(Exception):
                    await close_voice()
            await session.stop()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        console.print("\n[yellow]Demo ended.[/yellow]")


async def _autonomous_loop(session: object) -> None:
    """Walk the page unattended until someone interrupts.

    Beats come from the live page, never from a script; the pause between them
    is what leaves room for a question.
    """
    import asyncio

    await asyncio.sleep(2.0)
    for _ in range(12):
        try:
            turn = await session.next_tour_beat()  # type: ignore[attr-defined]
            console.print(f"[green]agent[/green] {turn.say}")
        except Exception as exc:  # the tour is best effort
            console.print(f"[dim]tour stopped: {exc}[/dim]")
            return
        await asyncio.sleep(6.0)


@app.command()
def bench(
    url: str = typer.Option(None, help="URL to benchmark against. Defaults to the fixture."),
    turns: int = typer.Option(3, help="Repetitions of the question set."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Measure latency and report P50/P90/P95.

    Runs against the local fixture by default so the numbers are reproducible and
    need no network.
    """
    import asyncio

    from karumi_agent.bench import run_benchmark

    settings = _bootstrap_settings()
    result = asyncio.run(run_benchmark(settings, url=url, repetitions=turns))

    if json_output:
        import json

        console.print_json(json.dumps(result.as_dict()))
        return

    console.print(f"\n[bold]Benchmark[/bold]  model={result.model}")
    console.print(f"[dim]{result.machine}[/dim]\n")
    table = Table(show_header=True, header_style="bold", box=None)
    table.add_column("Stage", style="cyan")
    for column in ("n", "P50", "P90", "P95", "min", "max"):
        table.add_column(column, justify="right")
    for stage, stats in result.stages.items():
        table.add_row(
            stage,
            str(stats["count"]),
            f"{stats['p50']:.2f}",
            f"{stats['p90']:.2f}",
            f"{stats['p95']:.2f}",
            f"{stats['min']:.2f}",
            f"{stats['max']:.2f}",
        )
    console.print(table)
    console.print(
        f"\nturns={result.turns}  verified={result.verified}  invalid_refs={result.invalid_refs}"
    )
    for note in result.notes:
        console.print(f"[dim]{note}[/dim]")


@app.command()
def version() -> None:
    """Print the version."""
    from karumi_agent import __version__

    console.print(__version__)


def main() -> None:
    """Console-script entry point."""
    try:
        app()
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        console.print("\n[yellow]Interrupted.[/yellow]")
        sys.exit(130)


if __name__ == "__main__":  # pragma: no cover
    main()
