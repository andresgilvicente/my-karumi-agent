# Recovery milestones

The first delivery built everything at once. Its tests passed and it did not
work. Four defects surfaced only when a human ran it: no VAD stage, so nothing
was ever transcribed; FastAPI 422s on every POST, so the microphone button
silently did nothing; speech models loading inside the WebRTC handler; and a
keyword-only argument passed positionally.

The common thread is that breadth was proven and the vertical was not. This
document is the correction.

## Rules

- Milestones are **strictly sequential**.
- Each ends with automated tests, a manual checklist, and a **stop**.
- **Only the operator closes a milestone.** Passing tests are a precondition,
  never the evidence.
- Nothing hardware-dependent is called validated without the operator confirming
  it on their machine.
- No retries or fallbacks that hide a failure. Failures appear in the UI *and*
  the terminal.
- One focused commit per validated milestone. Validated commits are never
  amended. Nothing is pushed. No new branches.

## Status

Current: **Milestone 0, awaiting operator validation.**
The ledger lives in `docs/PROJECT_STATE.md`.

| M | Scope | Proves |
|---|---|---|
| **0** | Diagnostic console, no pipeline | Every subsystem is individually reachable and its failure visible |
| 1 | Browser only: URL → headed Chromium → observation → manual scroll/highlight/click | Observation and verified actions on the real site |
| 2 | Text agent: typed instruction → LLM → action → verification → written reply | One inference drives a verified action |
| 3 | TTS only: a fixed audible sentence | Audible output on the operator's speakers |
| 4 | Microphone + STT only: listening state, physical transcript | The operator's microphone reaches a transcript |
| 5 | Voice conversation: mic → STT → LLM → TTS, interruptible | The conversational loop, no browser |
| 6 | First full vertical slice: "Highlight the Analytics section" | The whole product in one path |
| 7 | Expand browser capabilities, one at a time | Breadth, on a proven base |
| 8 | Autonomous tour | Only once manual interaction is stable |
| 9 | Generic URL learning | Only once Metric Master Suite is stable |

## Milestone 0 — diagnostic console

**Delivered, pending validation.**

One command opens a console that starts instantly and does nothing until asked.
It reports every subsystem and gives each an independent Test button, so a
failure is attributable to one thing instead of presenting as silence.

### Design decisions

**Cheap and expensive work are strictly separated.** `GET /api/diag` performs
only filesystem reads, `PATH` lookups and one small HTTP GET to Ollama's
`/api/tags`. It loads no model, launches no browser and generates nothing — a
test asserts that `onnx_asr`, `kokoro_onnx` and `faster_whisper` are never
imported by it. Everything expensive sits behind a Test button.

**Audio avoids ffmpeg entirely.** `ffmpeg` is not installed and `MediaRecorder`
emits webm/opus that the server could not decode without it. The console
captures raw PCM through the Web Audio API and encodes WAV in JavaScript;
synthesis returns a WAV the browser plays directly. No new system dependency,
and **Test STT uses the real microphone**, which is the point of that button.

**The LLM failure path is deterministic.** `ollama stop` only unloads a model
and the next request reloads it, so it does not simulate an outage. A
diagnostic-only `simulate_unreachable` flag points the probe at a dead port
(`127.0.0.1:1`) instead. To take the real service down:
`sudo systemctl stop ollama` (or kill the `ollama serve` process), then restart
with `sudo systemctl start ollama` / `ollama serve`.

**The microphone is always released.** Capture runs inside a helper whose
`finally` stops every `MediaStreamTrack` and closes the `AudioContext` — on
success, on cancellation and on error — and the row then states so explicitly.

**WebRTC is capability reporting only.** Browser support and secure context are
shown; no handshake, no transport, no pipeline. That is Milestone 4/5 work.

### What Milestone 0 does *not* prove

The probes exercise components one-shot over HTTP. They do **not** prove the
streaming product paths: Pipecat's `SmallWebRTCTransport`, its STT/TTS service
wrappers, barge-in, or the pipeline. Milestones 3-5 do that, and an M0 pass is
not evidence for them.
