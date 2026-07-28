# Project State

Updated: 2026-07-28

## Current State

- The local MVP now has one connected product-demo flow: the React Metric Master Suite interface connects directly to the Pipecat voice worker over SmallWebRTC and RTVI.
- Voice-turn stabilization remains on Whisper `base` with a 1.0-second speech timeout.
- The first UI-control phase is complete. Supported voice requests can navigate to Dashboard, Analytics, or Team and then confirm the destination's standard highlight or scroll action.
- Semantic UI control is now centralized in a versioned catalog shared by React and Python; the backend validates advertised capabilities per action and ignores stale UI state.
- The backend now owns a declarative autonomous tour through Dashboard, Analytics, and Team, with a proactive offer, deterministic controls, interruption-safe continuation, and silence-based advancement.
- Repository setup, architecture, command-contract, and validation guidance reflect the connected implementation.

## Implemented Components

- Voice: WebRTC input/output, Whisper transcription, VAD start detection, speech-timeout turn completion, Ollama responses, Kokoro speech, automatic introduction, conversation context, and native per-stage/end-to-end latency observers.
- Frontend: Home, Dashboard Overview/Goals, Analytics, and Team views with hash navigation, responsive CSS, keyboard-operated dashboard tabs, in-memory team filtering, voice connection/microphone controls, and voice activity status.
- UI bridge: catalog-derived route and capability publication through `ui_state`; idempotent `navigate_to`, `highlight`, and `scroll_to` handlers; and `ui_action_result` acknowledgements correlated by `commandId`.
- Backend orchestration: dynamic React state in the LLM instruction, catalog-derived tool schemas, five-second action timeouts, confirmation-aware responses, and deterministic dispatch for explicit allowlisted requests.
- Context safety: deterministic turns commit the user request and confirmed assistant response before audio synthesis, so an interruption does not erase the confirmed action from later LLM context.
- Kokoro applies Pipecat's sentence-level Markdown filter before synthesis so emphasis markers are not pronounced without changing Qwen's response generation.
- Optional local profiling correlates VAD, logical turn completion, STT/LLM/TTS metrics, first audio, resolution path, and confirmed UI action timing in JSONL without changing normal runtime behavior.
- Navigation compounds the expected focus action: Dashboard highlights Total Revenue, Analytics highlights Traffic Overview, and Team scrolls to the member list.
- Guided-demo actions reuse the same serialized executor and acknowledgement contract; Qwen answers questions but never controls the tour state.

## Current Constraints

- Frontend data is representative and in memory; there is no persistence or backend data API.
- UI control is intentionally limited to Dashboard, Analytics, and Team navigation plus two highlights and one scroll target. Goals, search, analytics tabs, and generic screen control remain out of scope.
- Explicit allowlisted requests bypass Qwen's tool selection because `qwen2.5:1.5b` can narrate an action even when a tool is required. Ollama still handles normal product conversation, and function tools remain a fallback.
- Whisper emits completed utterances rather than streaming partial transcripts. The semantic resolver retains a short previous transcript so a split phrase such as `Can you take me?` followed by `to Analytics` can still navigate.
- Whisper `base` still misses product vocabulary such as `Pipecat` and `Metric Master Suite`; custom vocabulary support remains deferred.
- Kokoro response generation remains the main source of variable conversational latency, especially for longer answers.
- End-to-end voice and UI behavior depends on local models, browser media, and system services and remains a manual verification path.
- Guided-demo timing is currently fixed at five seconds after the offer, four seconds between steps, and a five-second missing-transcript recovery window; these values are expected to be tuned after manual use.
- The catalog intentionally describes only the supported semantic surface, not all React routes or DOM structure.

## Last Verified

- On 2026-07-28, `bot.py` passed byte-compilation, import, focused semantic-intent assertions, and `git diff --check`.
- On 2026-07-28, the Vite production build passed under Linux nvm Node 24. The generated JavaScript chunk is 670.01 kB and still produces Vite's size warning.
- On 2026-07-28, the Pipecat runner reported ready and the React app connected through SmallWebRTC, published its initial `ui_state`, and completed normal voice exchanges.
- On 2026-07-28, manual browser tests passed for Team navigation plus member-list scroll, Dashboard navigation plus Total Revenue highlight, and Analytics navigation plus Traffic Overview highlight.
- Backend logs confirmed a matching `status: ok` result for every navigation and focus `commandId` before the agent reported success.
- On 2026-07-28, the catalog refactor passed ten standard-library UI-control tests, Python compilation/import, and a Linux Node 24 production build. Manual regression checks passed normal conversation, all three navigation/focus flows, a split navigation request, and sidebar navigation before a voice command.
- On 2026-07-28, Pipecat's sentence-level Markdown filter passed focused automated cases for bold, italic, underscore, inline-code, and plain text; a manual greeting check confirmed that section names are spoken naturally without formatting symbols.
- On 2026-07-28, the opt-in JSONL profiler passed standard-library tests for disabled behavior, correlated turn/action records, interruption summaries, Python compilation, and import.
- On 2026-07-28, 39 standard-library tests passed for the guided-demo state machine, closed controls, proactive and silent advancement, interruption races, serialized actions, consumed-turn finalization, failure handling, and disconnect cleanup. Python compilation/import and the Vite build also passed.
- On 2026-07-28, a manual Chrome run passed the complete silent Dashboard -> Analytics -> Team sequence and completion message. Interruption and explicit control variants remain separate manual checks.

## Relevant Files

- `bot.py`: voice pipeline, semantic intent resolution, function schemas, UI state, RTVI commands, acknowledgements, and failure handling.
- `client/src/ui-demo-catalog.json`: canonical semantic UI contract used by both runtime surfaces.
- `ui_control/`: catalog validation, resolver, action execution, function fallback, and deterministic context handling.
- `ui_control/guided_demo.json`: ordered tour actions, spoken copy, control phrases, and timing.
- `ui_control/guided_demo.py` and `ui_control/guided_demo_processor.py`: session state machine, timers, interruption handling, and Pipecat adaptation.
- `ui_control/turns.py`: safe closure of deterministic voice turns consumed before Ollama.
- `docs/guided-demo.md`: behavior and manual validation protocol.
- `client/src/voiceClient.js`: Pipecat client, SmallWebRTC transport, and configurable runner endpoint.
- `client/src/VoiceControls.jsx`: connect/disconnect, mute/unmute, and activity/error state.
- `client/src/UICommandBridge.jsx`: React state publication, command allowlists, idempotency, and results.
- `client/src/App.jsx`: routes, product views, semantic element IDs, and voice integration points.
- `client/src/App.css`: responsive product UI, voice controls, and highlight animation.
- `README.md`: setup, connected runtime flow, command scope, and verification.
- `AGENTS.md`: stable architecture, commands, validation, and working rules.

## Next Sequence

1. Treat the current three-route vertical demo as the baseline and avoid expanding the command surface without a concrete demo need.
2. If broader phrasing becomes necessary, replace or augment the compact semantic resolver without weakening the command allowlists or confirmation contract.
3. Collect the fixed local latency corpus before changing model or timeout settings; profile conversational latency separately from deterministic UI actions.
4. Manually validate and tune the guided-demo cadence without widening the frontend command contract.
5. Keep the standard-library semantic UI and guided-demo tests current whenever either declarative catalog changes; introduce broader test infrastructure only when the surface grows beyond this small contract.
