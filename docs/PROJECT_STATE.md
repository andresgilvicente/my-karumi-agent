# Project state

**Development strategy changed after the first delivery.** The whole agent was
built in one pass; its automated tests passed and the product still did not work
on real hardware. The project is now being recovered through small vertical
milestones, each validated by the operator on their own machine before the next
begins. See `docs/MILESTONES.md`.

Machine for all measurements unless stated:
**AMD Ryzen 7 5700U, 8C/16T, no GPU, 18 GB RAM, WSL2 Ubuntu 24.04, Python 3.12.**

## Milestone ledger

Only the operator closes a milestone. Automated tests are a precondition, never
the evidence.

| Milestone | Automated | Operator validated | Committed |
|---|---|---|---|
| 0. Diagnostic console | yes (40 tests) | **pending** | no |
| 1. Browser only | — | — | — |
| 2. Text agent | — | — | — |
| 3. TTS only | — | — | — |
| 4. Microphone + STT only | — | — | — |
| 5. Voice conversation | — | — | — |
| 6. First vertical slice | — | — | — |
| 7. Expanded browser capabilities | — | — | — |
| 8. Autonomous tour | — | — | — |
| 9. Generic URL learning | — | — | — |

## Unvalidated modules

Built during the first pass, never confirmed on hardware by a human. They remain
in the tree, **unwired from the product flow**, and are re-enabled one milestone
at a time. Reachable for development via `karumi dev`.

| Module | What is genuinely unproven | Milestone |
|---|---|---|
| `agent/voice.py` | Only ever run with synthesized audio and a synthetic aiortc track. No physical microphone, no audible output, no barge-in with real speech. | 4-5 |
| `agent/session.py` `start()` | Does seven things in one call before serving. The direct cause of "nothing happens". Being decomposed. | 1-2 |
| `agent/knowledge.py` | Parsing has unit tests; the crawl has never run in a real demo. | 9 |
| `planner.next_tour_beat` | Never run end to end with a human present. | 8 |
| `browser/overlay.js` | Tested for presence in the DOM. Its visual states have never been seen. | 1 |
| `bench.py` | Works; a distraction during recovery. | 7 |
| `server/app.py` | The pre-recovery console. Superseded by `server/console.py`; kept so its regression tests still run. | 5 |

## Phase status (first delivery, superseded by the milestone ledger)

| Phase | State | Notes |
|---|---|---|
| 0. Scaffold + Pipecat API verification | done | 20/21 API claims verified against the installed 1.7.0 |
| 1. Browser adapter + hostile fixture | done | Snapshot, executor, verifier, guards, overlay |
| 2. Planner over text | done | Constrained decoding; validated against the fixture with a real model |
| 3. Local STT + LLM + TTS | done | Parakeet/Whisper, Ollama, Kokoro, all behind factories |
| 4. Voice via SmallWebRTC + console | done | Verified end to end: synthesized speech -> transcription, and a real aiortc peer connection reaching `connected` |
| 5. Metric Master Suite | done | Login-wall path implemented; signed-in views need an operator |
| 6. Configurable URL + learning + tour | done | Learning starts before the greeting ends |
| 7. Latency, docs | done | P50 first-token 0.89 s after the prefix-cache fix |
| 8. Clean-install validation + commit | done | See "Clean install" below |

## Verified facts (checked against source or measured here, not assumed)

### Pipecat 1.7.0
* `UIWorker.render_ui_state(self) -> str` is **public and synchronous**, and
  `send_command(self, name, payload)` is public. Both confirmed on the installed
  package. This is what lets us drive a third-party site with our own Playwright
  adapter instead of the `@pipecat-ai/client-js` snapshot walker, which would
  require the target site to embed a Pipecat SDK.
* Silero VAD (2.3 MB) and `smart-turn-v3.2-cpu.onnx` (8.68 MB) ship **inside the
  wheel**. No download, no HuggingFace fetch, no torch.
* `OLLamaLLMService` defaults to `http://localhost:11434/v1`.
* Smart Turn v3 is the **default** stop strategy in 1.7.0.

### Playwright 1.62
* `page.aria_snapshot(mode="ai", boxes=True)` is a public Python API.
* `page.accessibility.snapshot()` was **removed** in 1.57.
* `mode="ai"` refs only elements that are visible and receive pointer events.
  Consequence we rely on: a **disabled control gets no ref at all**, so the model
  cannot express clicking it. Pinned by a test.

### The demo target
* `https://metric-master-suite505.apps.rebolt.ai/` is a Vite + React Router v7
  SPA with 7 routes, shadcn/Radix components, and a live Convex WebSocket backend.
* **Its landing route is a sign-in wall.** Captured live: heading "Welcome back",
  an email textbox, a password textbox, a disabled "Sign In" button. One visible
  `input[type=password]`. The real snapshot is checked in as a test fixture in
  `tests/conftest.py`, so the parser is tested against genuine Playwright output.
* Because of the Convex WebSocket, `wait_for_load_state("networkidle")` never
  resolves. We use request-settling instead.
* It bundles **no chart library** (no recharts/d3/chart.js). Visualisations are
  hand-rolled and largely invisible to the accessibility tree. See LIMITATIONS.

### Measured locally
* `qwen2.5:1.5b`, 575-token prompt: ~15 tok/s generation. Cold prefill 98 tok/s
  (11.4 s first response) vs warm 3721 tok/s (0.40 s TTFT). **Warmup matters ~38x.**
* `qwen3.5:9b` on CPU: exceeded 2 minutes for a single turn. Not viable here.
* Unconstrained tool calling on 1.5B produced invalid refs (`ref: "Analytics"`
  instead of `e5`). With a JSON schema whose `ref` enum comes from the live
  snapshot: **6/6 valid refs**. Invalid refs became inexpressible.
* **Prefix caching dominates turn latency.** Identical 1029-token prompt,
  `qwen3:1.7b`: first call prefill **6.69 s at 154 tok/s**, second call prefill
  **0.10 s at 10,737 tok/s**. A ~70x difference, and the single largest lever on
  this hardware.
  * JSON-schema grammar compilation: negligible (no measurable delta).
  * `ref` enum size (41 values vs 1): negligible (0.35 s vs 0.11 s prefill).
  * Consequences, both implemented: prompt sections are ordered least-volatile
    first so a per-turn change cannot invalidate the page state behind it, and
    the new page state is prefilled in the background after every navigation
    (`Planner.prewarm_page`).
* End-to-end against the hostile fixture with `qwen3:1.7b`, warm:
  **time-to-first-speech ~0.9-1.1 s**, full turn ~2.6-3.4 s, ~12-13 tok/s decode.
  Turns whose prompt prefix is cold cost ~7-8 s instead.

## Bugs found by running the app, not by tests

Four defects that unit tests could not have caught, all found by driving the real
console over HTTP. Each now has a regression test.

1. **No VAD stage in the voice pipeline.** `SegmentedSTTService` reacts only to
   `VADUserStartedSpeakingFrame` / `VADUserStoppedSpeakingFrame`, and in Pipecat
   1.7.0 `TransportParams` has no `vad_analyzer` field — VAD is a pipeline stage
   (`VADProcessor`) or lives on an LLM context aggregator this design does not
   have. Without it the microphone connected, audio flowed, and nothing was ever
   transcribed. Covered by `tests/integration/test_voice_pipeline.py`, which
   feeds real synthesized speech through the stages.
2. **FastAPI could not resolve handler annotations.** `from __future__ import
   annotations` makes every annotation a string, and FastAPI resolves them
   against *module* globals — but `Request` was imported inside `build_app()`.
   Every POST returned `422 Field required: query.request`. The console then fed
   that error object into `setRemoteDescription`, so the microphone silently
   stayed off. Covered by `tests/integration/test_console.py`.
3. **Speech models were built inside `/api/offer`.** That blocked the request
   ~15 s warm and minutes on a first run. They now load at session start, with a
   test asserting the offer is answered in under 5 s.
4. **`sample_rate` passed positionally to `onnx-asr`.** It is keyword-only, so
   every utterance became an error frame. Found by the audio pipeline test.

Two smaller ones from the same session: the login wall blocked startup for up to
ten minutes before the console began serving (now non-blocking, with a
background watcher), and session teardown left the voice pipeline running so
Ctrl+C appeared to hang.

## Environment notes

* **System libraries.** Playwright's Chromium needs `libnss3` and `libnspr4`,
  which are not present on a clean Ubuntu 24.04 WSL image. Without them the
  browser fails to launch with `libnspr4.so: cannot open shared object file`.
  Fix: `sudo playwright install-deps chromium`, or
  `sudo apt-get install -y libnss3 libnspr4`. `karumi doctor` checks for this.
* **nltk 3.10 CWD guard.** Pipecat imports nltk, and nltk >= 3.10 installs a
  meta-path finder that blocks imports resolving inside the current working
  directory. uv places the environment at `<project>/.venv`, i.e. inside the CWD,
  so every legitimate import is blocked and `import pipecat.workers.ui` fails.
  Handled in `karumi_agent/_bootstrap.py`: we keep `PYTHONSAFEPATH=1` (the real
  protection) and disable only the containment heuristic that misfires.

## Clean install

Verified from this checkout on the machine above:

```bash
sudo apt-get install -y espeak-ng libnss3 libnspr4
uv sync --extra parakeet
uv run playwright install chromium
ollama pull qwen3:1.7b
uv run karumi doctor      # all required checks pass
```

No API key is requested at any point, and `.env.example` contains none.
`.env` is gitignored and absent from the repository.

## Test inventory

| Suite | Count | Needs |
|---|---|---|
| `tests/unit` | 105 | nothing |
| `tests/integration` (browser) | 19 | Chromium |
| `tests/integration` (planner) | 7 | Chromium + Ollama |
| `tests/integration` (voice pipeline) | 3 | Parakeet + Kokoro weights |
| `tests/integration` (console) | 5 | Chromium + Ollama + speech models |

**166 total, all passing.**

Run everything: `uv run pytest`. Skip hardware-dependent suites:
`uv run pytest -m "not requires_browser and not requires_model"`.

## Milestone 0 — measured

Console startup, this machine:

```
build_app          7.5 ms
first byte       239.0 ms
GET /api/diag    306.1 ms   (11 cheap checks)
heavy modules loaded at startup: NONE
```

`GET /api/diag` performs filesystem and environment reads plus one small HTTP
GET to Ollama's `/api/tags`. It loads no model, launches no browser and
generates nothing; a test asserts that `onnx_asr`, `kokoro_onnx` and
`faster_whisper` are never imported by it.
