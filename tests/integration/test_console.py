"""The operator console actually works, including a real WebRTC handshake.

Regression test for the bug that made "Start talking" do nothing: FastAPI could
not resolve the ``Request`` annotation, because ``from __future__ import
annotations`` turns it into a string and it was imported *inside* ``build_app``.
Every POST therefore returned ``422 Field required: query.request``, the console
fed that error object into ``setRemoteDescription``, and the microphone
indicator sat on "off" with nothing explaining why.

Unit tests could not have caught it -- the handlers are correct in isolation.
Only exercising the running app over HTTP shows it, so that is what this does,
right down to establishing a genuine peer connection with aiortc.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from karumi_agent.agent.session import DemoSession
from karumi_agent.config import Settings
from karumi_agent.server.app import build_app
from karumi_agent.server.fixture import FixtureServer

pytestmark = [
    pytest.mark.requires_browser,
    pytest.mark.requires_model,
    pytest.mark.slow,
]

PORT = 7897


@pytest.fixture
async def console() -> AsyncIterator[tuple[httpx.AsyncClient, DemoSession]]:
    """Run the real app against the offline fixture site."""
    import uvicorn

    fixture = FixtureServer()
    base_url = fixture.start()

    profile = Path(tempfile.mkdtemp(prefix="karumi-console-"))
    downloads = Path(tempfile.mkdtemp(prefix="karumi-console-dl-"))
    settings = Settings()
    settings.server.port = PORT
    settings.browser.profile_dir = profile
    settings.browser.download_dir = downloads
    settings.browser.headless = True

    session = DemoSession(settings, url=base_url, headless=True)
    await session.start()

    app = build_app(settings, session)
    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error")
    server = uvicorn.Server(config)
    serve_task = asyncio.create_task(server.serve())
    await asyncio.sleep(1.0)

    client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{PORT}", timeout=180.0)
    try:
        yield client, session
    finally:
        await client.aclose()
        close_voice = getattr(app.state, "close_voice", None)
        if close_voice is not None:
            with contextlib.suppress(Exception):
                await close_voice()
        server.should_exit = True
        with contextlib.suppress(Exception):
            await asyncio.wait_for(serve_task, timeout=15)
        await session.stop()
        fixture.stop()
        shutil.rmtree(profile, ignore_errors=True)
        shutil.rmtree(downloads, ignore_errors=True)


class TestConsoleEndpoints:
    async def test_status_reports_a_usable_session(
        self, console: tuple[httpx.AsyncClient, DemoSession]
    ) -> None:
        client, _ = console
        response = await client.get("/api/status")
        assert response.status_code == 200
        payload = response.json()
        assert payload["title"]
        assert payload["voice_ready"] is True, "speech models did not load at startup"

    async def test_post_endpoints_accept_a_json_body(
        self, console: tuple[httpx.AsyncClient, DemoSession]
    ) -> None:
        """The exact failure mode: POST handlers 422'ing on their own body."""
        client, _ = console
        response = await client.post("/api/say", json={"text": "What is the revenue?"})
        assert response.status_code == 200, response.text
        assert response.json()["say"]

    async def test_index_page_is_served(
        self, console: tuple[httpx.AsyncClient, DemoSession]
    ) -> None:
        client, _ = console
        response = await client.get("/")
        assert response.status_code == 200
        assert "Karumi Agent" in response.text


class TestVoiceHandshake:
    async def test_webrtc_offer_is_answered_and_connects(
        self, console: tuple[httpx.AsyncClient, DemoSession]
    ) -> None:
        """A real peer connection, end to end."""
        from aiortc import RTCPeerConnection, RTCSessionDescription
        from aiortc.mediastreams import AudioStreamTrack

        client, _ = console
        pc = RTCPeerConnection()
        try:
            pc.addTrack(AudioStreamTrack())
            pc.addTransceiver("audio", direction="recvonly")
            offer = await pc.createOffer()
            await pc.setLocalDescription(offer)

            response = await client.post(
                "/api/offer",
                json={"sdp": pc.localDescription.sdp, "type": pc.localDescription.type},
            )
            assert response.status_code == 200, response.text
            answer = response.json()
            assert answer.get("type") == "answer", answer

            await pc.setRemoteDescription(
                RTCSessionDescription(sdp=answer["sdp"], type=answer["type"])
            )

            for _ in range(60):
                if pc.connectionState in {"connected", "failed", "closed"}:
                    break
                await asyncio.sleep(0.25)
            assert pc.connectionState == "connected", (
                f"peer connection ended in {pc.connectionState!r}; "
                "the microphone would appear to do nothing"
            )
        finally:
            await pc.close()

    async def test_offer_answers_promptly(
        self, console: tuple[httpx.AsyncClient, DemoSession]
    ) -> None:
        """Speech models load at startup, not inside this request.

        Building them here used to block the fetch for ~15 s warm, and minutes on
        a first run while weights downloaded.
        """
        import time

        from aiortc import RTCPeerConnection
        from aiortc.mediastreams import AudioStreamTrack

        client, _ = console
        pc = RTCPeerConnection()
        try:
            pc.addTrack(AudioStreamTrack())
            offer = await pc.createOffer()
            await pc.setLocalDescription(offer)

            started = time.perf_counter()
            response = await client.post(
                "/api/offer",
                json={"sdp": pc.localDescription.sdp, "type": pc.localDescription.type},
            )
            elapsed = time.perf_counter() - started
            assert response.status_code == 200, response.text
            assert elapsed < 5.0, f"offer took {elapsed:.1f}s; models are loading too late"
        finally:
            await pc.close()
