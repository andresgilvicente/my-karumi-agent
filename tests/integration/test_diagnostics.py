"""Milestone 0: the diagnostic console.

Two properties matter more than any individual probe:

1. **Startup is genuinely cheap.** The console must serve before anything heavy
   exists. The previous version launched a browser, warmed a model and loaded
   speech engines before its first byte, which is why one failure among them
   looked exactly like nothing happening.
2. **Failures are returned, never raised.** A probe that throws produces a stack
   trace nobody sees. Every probe must come back with `status="fail"` and a
   remedy, so the operator reads it on screen.

Tests that genuinely need a model or a browser are marked, and skip with a
stated reason rather than passing quietly.
"""

from __future__ import annotations

import struct
from collections.abc import Iterator

import numpy as np
import pytest
from fastapi.testclient import TestClient

from karumi_agent.audio_wav import DEFAULT_SAMPLE_RATE, encode_wav
from karumi_agent.config import Settings
from karumi_agent.diagnostics import UNREACHABLE_URL, cheap_checks
from karumi_agent.server.console import build_console_app


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(build_console_app(Settings())) as test_client:
        yield test_client


class TestStartupIsCheap:
    """Amendment 2: `GET /api/diag` must not do expensive work."""

    def test_building_the_app_starts_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No browser, no model, no speech engine at construction time."""
        import playwright.async_api

        def explode(*args: object, **kwargs: object) -> object:
            raise AssertionError("the console must not launch a browser at startup")

        monkeypatch.setattr(playwright.async_api, "async_playwright", explode)
        app = build_console_app(Settings())
        assert app is not None

    def test_cheap_checks_do_not_import_model_libraries(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Availability is answered by looking, not by loading.

        `onnx_asr.load_model` and `kokoro_onnx.Kokoro` both cost hundreds of MB
        and tens of seconds; a polled endpoint must never touch them.
        """
        import builtins

        real_import = builtins.__import__
        forbidden = {"onnx_asr", "kokoro_onnx", "faster_whisper"}
        loaded: list[str] = []

        def watched(name: str, *args: object, **kwargs: object) -> object:
            if name.split(".")[0] in forbidden:
                loaded.append(name)
            return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(builtins, "__import__", watched)
        cheap_checks(Settings())
        assert loaded == [], f"cheap checks loaded heavy modules: {loaded}"

    def test_diag_endpoint_is_fast(self, client: TestClient) -> None:
        import time

        started = time.perf_counter()
        response = client.get("/api/diag")
        elapsed = time.perf_counter() - started
        assert response.status_code == 200
        # Generous: the only network call is Ollama's /api/tags.
        assert elapsed < 5.0, f"cheap checks took {elapsed:.1f}s"


class TestDiagPayload:
    def test_reports_every_expected_subsystem(self, client: TestClient) -> None:
        payload = client.get("/api/diag").json()
        names = {check["name"] for check in payload["checks"]}
        for expected in ("chromium", "display", "espeak-ng", "stt", "ollama", "page"):
            assert expected in names, f"missing subsystem row: {expected}"

    def test_every_check_has_a_status_and_detail(self, client: TestClient) -> None:
        for check in client.get("/api/diag").json()["checks"]:
            assert check["status"] in {"ok", "warn", "fail"}
            assert check["detail"]

    def test_failing_checks_carry_a_remedy(self, client: TestClient) -> None:
        """A failure the operator cannot act on is only half a diagnostic."""
        for check in client.get("/api/diag").json()["checks"]:
            if check["status"] == "fail":
                assert check.get("remedy"), f"{check['name']} failed with no remedy"

    def test_page_row_says_it_belongs_to_a_later_milestone(self, client: TestClient) -> None:
        checks = {c["name"]: c for c in client.get("/api/diag").json()["checks"]}
        assert "milestone 1" in checks["page"]["detail"]


class TestAgentEndpointsAreExplicitlyDeferred:
    @pytest.mark.parametrize("path", ["/api/say", "/api/tour", "/api/navigate", "/api/offer"])
    def test_returns_503_with_an_explanation(self, client: TestClient, path: str) -> None:
        """Not a 404, and not a stack trace."""
        response = client.post(path, json={})
        assert response.status_code == 503
        assert "milestone 0" in response.json()["error"]


class TestLLMProbe:
    """Amendment 3: the failure path must be deterministic."""

    def test_unreachable_endpoint_fails_cleanly(self, client: TestClient) -> None:
        """Points at a dead port rather than stopping Ollama.

        `ollama stop` only unloads the model; the next request reloads it, so it
        does not simulate an outage at all.
        """
        response = client.post("/api/diag/llm", json={"simulate_unreachable": True})
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "fail"
        assert payload["remedy"]
        assert "elapsed_ms" in payload

    def test_unreachable_url_is_a_dead_port(self) -> None:
        assert UNREACHABLE_URL.endswith(":1")

    @pytest.mark.requires_model
    def test_real_generation_reports_text_and_ttft(self, client: TestClient) -> None:
        response = client.post("/api/diag/llm", json={})
        payload = response.json()
        if payload["status"] != "ok":
            pytest.skip(f"Ollama unavailable: {payload['detail']}")
        assert payload["data"]["reply"]
        assert payload["data"]["ttft_s"] is not None


class TestBrowserProbe:
    @pytest.mark.requires_browser
    def test_launches_and_closes_chromium(self, client: TestClient) -> None:
        payload = client.post("/api/diag/browser").json()
        if payload["status"] != "ok":
            pytest.skip(f"Chromium unavailable: {payload['detail']}")
        assert "Chromium" in payload["detail"]
        assert payload["data"]["version"]
        assert payload["elapsed_ms"] > 0


class TestTTSProbe:
    @pytest.mark.requires_tts
    def test_returns_playable_audio(self, client: TestClient) -> None:
        response = client.post("/api/diag/tts")
        if response.status_code != 200:
            pytest.skip(f"TTS unavailable: {response.json()['detail']}")
        assert response.headers["content-type"] == "audio/wav"
        body = response.content
        assert body[:4] == b"RIFF" and body[8:12] == b"WAVE"
        channels = struct.unpack_from("<H", body, 22)[0]
        bits = struct.unpack_from("<H", body, 34)[0]
        assert channels == 1 and bits == 16
        assert float(response.headers["X-Probe-Seconds"]) > 0.5


class TestSTTProbe:
    def test_empty_upload_is_rejected_with_a_remedy(self, client: TestClient) -> None:
        response = client.post("/api/diag/stt", content=b"")
        assert response.status_code == 400
        assert response.json()["remedy"]

    def test_garbage_upload_fails_without_raising(self, client: TestClient) -> None:
        response = client.post("/api/diag/stt", content=b"this is not audio")
        assert response.status_code == 200
        assert response.json()["status"] == "fail"

    def test_silence_is_reported_honestly(self, client: TestClient) -> None:
        """Silence must not come back as a confident empty transcript."""
        silence = encode_wav(np.zeros(DEFAULT_SAMPLE_RATE, dtype=np.float32))
        payload = client.post("/api/diag/stt", content=silence).json()
        if payload["status"] == "fail" and "onnx" in payload["detail"].lower():
            pytest.skip("STT model not installed")
        assert payload["status"] in {"warn", "ok"}
        if payload["status"] == "warn":
            assert "no words" in payload["detail"]

    @pytest.mark.requires_stt
    @pytest.mark.requires_tts
    def test_round_trip_through_synthesized_speech(self, client: TestClient) -> None:
        """Kokoro speaks, Parakeet listens -- no microphone needed.

        Proves the server half of the microphone diagnostic in CI. The operator's
        manual checklist proves the other half, with real hardware.
        """
        tts = client.post("/api/diag/tts")
        if tts.status_code != 200:
            pytest.skip("TTS unavailable")
        payload = client.post("/api/diag/stt", content=tts.content).json()
        if payload["status"] == "fail":
            pytest.skip(f"STT unavailable: {payload['detail']}")
        assert payload["status"] == "ok"

        # Assert on mid-sentence content. The first word is routinely clipped at
        # utterance onset -- observed here as "Spee" for "Speech" -- and the test
        # is about the audio round-tripping, not about every phoneme surviving.
        transcript = payload["data"]["transcript"].lower()
        assert "karumi agent" in transcript, transcript
        assert "local machine" in transcript, transcript


class TestExport:
    """Amendment 5: one shareable object, no secrets."""

    def test_contains_environment_hardware_checks_and_config(self, client: TestClient) -> None:
        payload = client.get("/api/diag/export").json()
        for key in ("generated_at", "environment", "hardware", "config", "checks", "probes"):
            assert key in payload, f"export is missing {key}"
        assert payload["environment"]["python"]
        assert payload["environment"]["packages"]["pipecat-ai"]

    def test_records_probe_results_with_elapsed_times(self, client: TestClient) -> None:
        client.post("/api/diag/llm", json={"simulate_unreachable": True})
        # The simulated failure is deliberately not recorded as a real result.
        payload = client.get("/api/diag/export").json()
        assert "llm" not in payload["probes"]

    def test_carries_no_credential_shaped_values(self, client: TestClient) -> None:
        body = client.get("/api/diag/export").text.lower()
        for banned in ("password", "api_key", "secret=", "bearer "):
            assert banned not in body, f"export leaked something matching {banned!r}"

    def test_downloads_as_a_file(self, client: TestClient) -> None:
        response = client.get("/api/diag/export")
        assert "attachment" in response.headers.get("content-disposition", "")


class TestConsolePage:
    def test_is_served(self, client: TestClient) -> None:
        response = client.get("/")
        assert response.status_code == 200
        assert "milestone 0" in response.text

    def test_offers_every_required_test_button(self, client: TestClient) -> None:
        page = client.get("/").text
        for label in (
            "Test browser",
            "Test LLM",
            "Test TTS",
            "Test microphone",
            "Test STT",
            "Download diagnostics JSON",
        ):
            assert label in page, f"console is missing the {label!r} control"

    def test_releases_the_microphone_in_a_finally_block(self, client: TestClient) -> None:
        """Amendment 4, asserted structurally.

        Browser-side cleanup cannot be exercised headlessly here, so this pins
        the shape: the capture helper must stop tracks and close the
        AudioContext in a `finally`, so cancellation and errors release the
        hardware too.
        """
        page = client.get("/").text
        helper = page[page.index("async function withMicrophone") :]
        helper = helper[: helper.index("\nfunction testMic")]
        assert "finally" in helper
        assert "getTracks().forEach(t => t.stop())" in helper
        assert "ctx.close()" in helper
        assert "microphone released" in helper
