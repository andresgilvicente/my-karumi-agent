"""Local control page and WebRTC signalling.

Two windows during a demo: the real Chromium the audience watches, and this
page, which supplies the microphone and shows what the agent is doing. The
microphone lives here rather than in Python because browser WebRTC capture is
simply more reliable than PyAudio under WSL2, and it costs one extra window.

Everything is served from localhost. No CDN, no external asset, no account.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

# Imported at module scope on purpose. With `from __future__ import annotations`
# every annotation is a string, and FastAPI resolves handler annotations against
# the *module* globals. Importing Request inside build_app() left FastAPI unable
# to resolve it, so it treated `request: Request` as a query parameter and every
# POST returned "422 Field required: query.request" -- which is what made the
# microphone button silently do nothing.
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from karumi_agent.agent.session import DemoSession
from karumi_agent.config import Settings
from karumi_agent.logging import get_logger

log = get_logger("server")

_STATIC = Path(__file__).parent / "static"


def build_app(settings: Settings, session: DemoSession) -> FastAPI:
    """Create the FastAPI app for the control page and signalling."""
    app = FastAPI(title="Karumi Agent", docs_url=None, redoc_url=None)

    # One connection at a time: this is a single operator's demo, and a second
    # peer would fight for the same microphone and the same browser.
    state: dict[str, Any] = {"connection": None, "worker": None, "task": None}

    async def close_voice() -> None:
        """Tear down any live voice session.

        Without this, a running pipeline keeps the process alive and Ctrl+C
        appears to hang.
        """
        task = state.get("task")
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        connection = state.get("connection")
        if connection is not None:
            with contextlib.suppress(Exception):
                await connection.disconnect()
        state["connection"] = state["worker"] = state["task"] = None

    # Exposed so the CLI can shut the pipeline down before closing the browser.
    app.state.close_voice = close_voice

    @app.on_event("shutdown")
    async def _on_shutdown() -> None:
        await close_voice()

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(_STATIC / "index.html")

    @app.get("/api/status")
    async def status() -> JSONResponse:
        session.refresh_status()
        payload = session.status.as_dict()
        payload["connected"] = state["connection"] is not None
        payload["voice_ready"] = session.voice_ready
        return JSONResponse(payload)

    @app.post("/api/say")
    async def say(request: Request) -> JSONResponse:
        """Text fallback, so the demo still works without a microphone."""
        body = await request.json()
        text = str(body.get("text") or "").strip()
        if not text:
            return JSONResponse({"error": "empty"}, status_code=400)
        turn = await session.say(text)
        return JSONResponse(
            {
                "say": turn.say,
                "action": turn.action.describe() if turn.action else None,
                "verified": turn.verified,
                "seconds": round(turn.total_s, 2),
            }
        )

    @app.post("/api/tour")
    async def tour() -> JSONResponse:
        turn = await session.next_tour_beat()
        return JSONResponse({"say": turn.say, "seconds": round(turn.total_s, 2)})

    @app.post("/api/navigate")
    async def navigate(request: Request) -> JSONResponse:
        """Point the demo at a different URL mid-session."""
        body = await request.json()
        url = str(body.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            return JSONResponse({"error": "url must be http(s)"}, status_code=400)
        session.browser.navigation.allow(url)
        await session.browser.goto(url, allow_new_origin=True)
        await session.snapshotter.capture()
        session.knowledge.start(url)
        await session.planner.prewarm_page()
        session.refresh_status()
        return JSONResponse(session.status.as_dict())

    @app.post("/api/offer")
    async def offer(request: Request) -> JSONResponse:
        """WebRTC signalling for the microphone connection.

        Returns a structured error rather than a 500 with no body: the console
        shows whatever comes back, and a silent failure here previously left the
        microphone indicator stuck on "off" with nothing explaining why.
        """
        # Pipecat imports stay function-local: they are heavy, and only this
        # endpoint needs them. Unlike FastAPI's, these annotations are never
        # resolved by a framework.
        from pipecat.transports.base_transport import TransportParams
        from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
        from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport

        from karumi_agent.agent.voice import build_voice_pipeline

        if not session.voice_ready:
            return JSONResponse(
                {
                    "error": (
                        "Speech models are not available, so voice is disabled. "
                        "Run `uv run karumi doctor` to see what is missing. "
                        "You can still type in the box below."
                    )
                },
                status_code=503,
            )

        try:
            body = await request.json()
            # Retire any previous session *before* creating the new one. Doing it
            # afterwards tore down the connection we had just built.
            await close_voice()

            connection = SmallWebRTCConnection()
            await connection.initialize(sdp=body["sdp"], type=body["type"])

            transport = SmallWebRTCTransport(
                webrtc_connection=connection,
                params=TransportParams(audio_in_enabled=True, audio_out_enabled=True),
            )
            # STT and TTS were built at startup; passing them in keeps this
            # handler fast instead of loading model weights inside a fetch().
            worker = build_voice_pipeline(
                session, settings, transport, stt=session.stt, tts=session.tts
            )
            state["connection"] = connection
            state["worker"] = worker

            # Run the pipeline alongside the web server rather than blocking.
            # Keep the handle so shutdown can cancel it.
            state["task"] = asyncio.create_task(_run_worker(worker, state))

            # Pipecat's connection object is untyped; the shape is {sdp, type}.
            answer: dict[str, Any] = connection.get_answer()  # type: ignore[no-untyped-call]
            return JSONResponse(answer)
        except Exception as exc:  # reported to the operator
            log.exception("voice connection failed")
            state["connection"] = state["worker"] = None
            return JSONResponse(
                {"error": f"Could not start the voice session: {type(exc).__name__}: {exc}"},
                status_code=500,
            )

    return app


async def _run_worker(worker: Any, state: dict[str, Any]) -> None:
    from pipecat.workers.runner import WorkerRunner

    try:
        runner = WorkerRunner(handle_sigint=False)
        await runner.add_workers(worker)
        await runner.run()
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # a dropped call must not kill the server
        log.warning(f"voice pipeline stopped: {type(exc).__name__}: {exc}")
    finally:
        state["connection"] = None
        state["worker"] = None
