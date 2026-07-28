# Project State

Updated: 2026-07-28

## Current State

- The local MVP now has one connected product-demo flow: the React Metric Master Suite interface connects directly to the Pipecat voice worker over SmallWebRTC and RTVI.
- Voice-turn stabilization remains on Whisper `base` with a 1.0-second speech timeout.
- The first UI-control phase is complete. Supported voice requests can navigate to Dashboard, Analytics, or Team and then confirm the destination's standard highlight or scroll action.
- Repository setup, architecture, command-contract, and validation guidance reflect the connected implementation.

## Implemented Components

- Voice: WebRTC input/output, Whisper transcription, VAD start detection, speech-timeout turn completion, Ollama responses, Kokoro speech, automatic introduction, conversation context, and native per-stage/end-to-end latency observers.
- Frontend: Home, Dashboard Overview/Goals, Analytics, and Team views with hash navigation, responsive CSS, keyboard-operated dashboard tabs, in-memory team filtering, voice connection/microphone controls, and voice activity status.
- UI bridge: current route and capability publication through `ui_state`; idempotent `navigate_to`, `highlight`, and `scroll_to` handlers; and `ui_action_result` acknowledgements correlated by `commandId`.
- Backend orchestration: dynamic React state in the LLM instruction, closed tool schemas, five-second action timeouts, confirmation-aware responses, and deterministic dispatch for explicit allowlisted requests.
- Navigation compounds the expected focus action: Dashboard highlights Total Revenue, Analytics highlights Traffic Overview, and Team scrolls to the member list.

## Current Constraints

- Frontend data is representative and in memory; there is no persistence or backend data API.
- UI control is intentionally limited to Dashboard, Analytics, and Team navigation plus two highlights and one scroll target. Goals, search, analytics tabs, and generic screen control remain out of scope.
- Explicit allowlisted requests bypass Qwen's tool selection because `qwen2.5:1.5b` can narrate an action even when a tool is required. Ollama still handles normal product conversation, and function tools remain a fallback.
- Whisper emits completed utterances rather than streaming partial transcripts. The semantic resolver retains a short previous transcript so a split phrase such as `Can you take me?` followed by `to Analytics` can still navigate.
- Whisper `base` still misses product vocabulary such as `Pipecat` and `Metric Master Suite`; custom vocabulary support remains deferred.
- Kokoro response generation remains the main source of variable conversational latency, especially for longer answers.
- End-to-end voice and UI behavior depends on local models, browser media, and system services and remains a manual verification path.

## Last Verified

- On 2026-07-28, `bot.py` passed byte-compilation, import, focused semantic-intent assertions, and `git diff --check`.
- On 2026-07-28, the Vite production build passed under Linux nvm Node 24. The generated JavaScript chunk is 668.15 kB and still produces Vite's size warning.
- On 2026-07-28, the Pipecat runner reported ready and the React app connected through SmallWebRTC, published its initial `ui_state`, and completed normal voice exchanges.
- On 2026-07-28, manual browser tests passed for Team navigation plus member-list scroll, Dashboard navigation plus Total Revenue highlight, and Analytics navigation plus Traffic Overview highlight.
- Backend logs confirmed a matching `status: ok` result for every navigation and focus `commandId` before the agent reported success.

## Relevant Files

- `bot.py`: voice pipeline, semantic intent resolution, function schemas, UI state, RTVI commands, acknowledgements, and failure handling.
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
3. Profile conversational latency separately from deterministic UI actions; defer model or TTS changes unless measured behavior blocks the demo.
4. Add focused automated coverage only when a test runner is introduced or the command resolver grows beyond its current small scope.
