# Project State

Updated: 2026-07-29

## Current State

- The local MVP connects the React product mock directly to the Pipecat voice worker through SmallWebRTC and RTVI.
- Shared UI contract v4 is implemented. Automated checks and local service probes pass; current final Chrome validation is unavailable and pending.

## Architecture

- React/Vite publishes session-bound UI state and executes semantic commands. Pipecat runs Whisper STT, deterministic UI and guided-demo control, Qwen through Ollama for conversation, and Kokoro TTS.
- UI actions are serialized, use unique command IDs, time out after five seconds each, and require coherent React confirmation. Session generations and UI session nonces isolate disconnects and reconnects.

## Models

- STT: Whisper `base`, CPU, `int8`, English.
- LLM: `qwen2.5:1.5b` through Ollama, temperature `0.3`, maximum `150` tokens.
- TTS: Kokoro `af_heart`, English.
- Turn and action timing: 1.0-second speech timeout and 5.0-second UI action timeout.

## Implemented Capabilities

- Deterministic navigation, target focus, Team member search, and Team search clear; Qwen remains responsible for normal conversation and cannot invent search results.
- Autonomous Dashboard -> Analytics -> Team tour with proactive offer, silent advancement, deterministic controls, interruption-aware state, external-action synchronization, and session cleanup.
- Contract v4 clear handling has an explicit Team/cleared confirmation matcher and synchronizes the tour to Team. Automated tests cover the fix.
- React command execution is idempotent by command ID; the backend rejects stale, late, mismatched, and old-session confirmations.
- Optional correlated local JSONL profiling covers voice stages, resolution paths, first audio, and confirmed UI action timing.

## Supported Views And Targets

- Dashboard: Total Revenue, Active Users, Conversion Rate, Growth Rate, and Performance Overview.
- Analytics: Page Views, Click-through Rate, Average Session Duration, Conversion Rate, Traffic Overview, and Traffic Sources.
- Team: Team member list, member-name search, and search clear.
- The contract contains twelve targets in total. Navigation is limited to Dashboard, Analytics, and Team.

## Automated Validation

- On 2026-07-29, `uv sync` passed with Python `3.12.13` and uv `0.11.32`.
- `bot.py` and `ui_control/*.py` byte-compilation passed, importing `bot` passed, and all 76 standard-library `unittest` tests passed.
- Linux nvm resolved Node `24.18.0` and npm `11.16.0` from Linux paths. `npm ci` passed with 0 vulnerabilities.
- Vite `8.1.5` production build passed. Its dated build output measured the JavaScript bundle at 678.94 kB; this is a build observation, not a permanent size baseline.
- Ollama `0.32.4` exposed `qwen2.5:1.5b` at 986 MB, and the model check returned exactly `OK`.
- Local endpoint probes returned HTTP 200 for the frontend, backend status (`ready` with WebRTC), the diagnostic client after its redirect, and Ollama.

## Manual Validation

- Current Chrome manual validation is unavailable and pending; contract v4 does not yet have current final browser acceptance evidence.
- Historical evidence from 2026-07-28 includes normal voice exchanges, the initial Dashboard, Analytics, and Team flows, and one silent full Dashboard -> Analytics -> Team tour. This is historical evidence, not current final validation.
- Reconnect behavior, all twelve targets, Team search, Team clear, and interruption behavior must not be treated as manually validated from that historical run.

## Known Warnings

- Vite emits its advisory because the generated JavaScript chunk exceeds 500 kB after minification. The 2026-07-29 output was 678.94 kB.

## Open Bugs

- No blocking bug is currently known from automated validation after the clear-confirmation fix. Current Chrome testing may still identify browser, audio, timing, or reconnect defects.

## Current Limitations

- Product data is static and in memory; there is no persistence or product-data backend.
- Goals, analytics tabs, arbitrary scrolling, coordinates, model-generated selectors, and generic screen control are outside the semantic command contract.
- Whisper produces completed utterances rather than partial streaming transcripts and can misrecognize product vocabulary or split speech around pauses.
- The compact local Qwen model is not trusted to dispatch explicit UI actions; deterministic handling intentionally limits supported phrasing and actions.
- End-to-end voice and UI acceptance depends on Chrome media access and the local model/runtime services.

## Profiling Status

- Opt-in profiling and its automated tests are implemented. No current dated performance corpus has been accepted for this state; use `docs/profiling-corpus.md` before drawing latency conclusions.

## Recommended Next Step

- Complete the current Chrome matrix in `docs/FINAL_CHECKLIST.md`, with particular attention to coherent clear confirmation, tour synchronization, reconnect isolation, the twelve-target matrix, search, and interruptions before expanding contract v4.

Development chronology belongs in [`PROJECT_JOURNEY.md`](PROJECT_JOURNEY.md), not in this snapshot.
