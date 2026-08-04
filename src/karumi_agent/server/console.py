"""The diagnostic console (milestone 0).

Deliberately owns **no** ``DemoSession``. It starts instantly, launches nothing,
and loads no model until the operator clicks a specific Test button. That is the
whole correction: the previous console started a browser, warmed a model, loaded
speech engines and checked for a login wall before it served a single byte, so
any failure among them looked identical — nothing happening.

Agent endpoints exist here only to say, explicitly, that they are not part of
this milestone. A 503 with a sentence is a better answer than a 404.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

# Module scope on purpose: `from __future__ import annotations` makes every
# annotation a string, and FastAPI resolves handler annotations against module
# globals. Importing Request inside the factory made every POST fail with
# "422 Field required: query.request".
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from karumi_agent import diagnostics
from karumi_agent.config import Settings
from karumi_agent.diagnostics import ProbeResult
from karumi_agent.logging import get_logger

log = get_logger("console")

_STATIC = Path(__file__).parent / "static"

_NOT_IN_MILESTONE = (
    "Not available in milestone 0. This console currently proves each subsystem "
    "in isolation; the agent is wired up in later milestones."
)


def build_console_app(settings: Settings) -> FastAPI:
    """Create the diagnostic console.

    Constructing this must not launch a browser, load a model, or initialise a
    speech engine. A test asserts exactly that.
    """
    app = FastAPI(title="Karumi Agent — diagnostics", docs_url=None, redoc_url=None)

    # Last result per probe, so the export and the UI can show what was tried.
    probes: dict[str, ProbeResult] = {}
    # One probe at a time: they load models and launch browsers, and two at once
    # on this hardware would just make both look broken.
    lock = asyncio.Lock()

    def _record(result: ProbeResult) -> ProbeResult:
        probes[result.name] = result
        return result

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(_STATIC / "console.html")

    @app.get("/api/diag")
    async def diag() -> JSONResponse:
        """Cheap availability and configuration checks only.

        No model loads here, no browser launches, nothing is generated. The
        console polls this, so it has to stay fast.
        """
        checks = await asyncio.to_thread(diagnostics.cheap_checks, settings)
        return JSONResponse(
            {
                "checks": [check.as_dict() for check in checks],
                "probes": {name: result.as_dict() for name, result in probes.items()},
            }
        )

    @app.post("/api/diag/browser")
    async def diag_browser() -> JSONResponse:
        async with lock:
            result = _record(await diagnostics.probe_browser(settings))
        return JSONResponse(result.as_dict())

    @app.post("/api/diag/llm")
    async def diag_llm(request: Request) -> JSONResponse:
        body = await _json_body(request)
        simulate = bool(body.get("simulate_unreachable"))
        async with lock:
            result = _record(await diagnostics.probe_llm(settings, simulate_unreachable=simulate))
        payload = result.as_dict()
        if simulate:
            payload["name"] = "llm (failure path)"
            # A failure is the expected outcome here, so do not let it linger in
            # the export as if the real endpoint were broken.
            probes.pop("llm", None)
        return JSONResponse(payload)

    @app.post("/api/diag/tts")
    async def diag_tts() -> Response:
        """Return spoken audio the browser can play, or a JSON failure."""
        async with lock:
            result, wav = await asyncio.to_thread(diagnostics.probe_tts, settings)
        _record(result)
        if not wav:
            return JSONResponse(result.as_dict(), status_code=503)
        return Response(
            content=wav,
            media_type="audio/wav",
            headers={
                "X-Probe-Detail": result.detail[:200],
                "X-Probe-Elapsed-Ms": str(round(result.elapsed_ms, 1)),
                "X-Probe-Seconds": str(result.data.get("seconds", 0)),
            },
        )

    @app.post("/api/diag/stt")
    async def diag_stt(request: Request) -> JSONResponse:
        """Transcribe a WAV recorded from the operator's real microphone."""
        wav = await request.body()
        if not wav:
            return JSONResponse(
                {
                    "name": "stt",
                    "status": "fail",
                    "detail": "no audio was uploaded",
                    "remedy": "Record again and make sure the microphone is not muted.",
                },
                status_code=400,
            )
        async with lock:
            result = _record(await asyncio.to_thread(diagnostics.probe_stt, settings, wav))
        return JSONResponse(result.as_dict())

    @app.get("/api/diag/export")
    async def diag_export() -> JSONResponse:
        """Everything needed to diagnose this machine, with secrets redacted."""
        payload = await asyncio.to_thread(diagnostics.build_export, settings, probes)
        return JSONResponse(
            payload,
            headers={"Content-Disposition": 'attachment; filename="karumi-diagnostics.json"'},
        )

    # ---------------------------------------------------------------- later
    @app.post("/api/say")
    @app.post("/api/tour")
    @app.post("/api/navigate")
    @app.post("/api/offer")
    async def not_yet() -> JSONResponse:
        return JSONResponse({"error": _NOT_IN_MILESTONE}, status_code=503)

    return app


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:  # an empty or malformed body just means defaults
        return {}
    return body if isinstance(body, dict) else {}
