# Project State

Updated: 2026-07-27

## Current State

- The local MVP has two working surfaces: a standalone Pipecat voice conversation and a static Metric Master Suite React mock.
- The voice runtime and React remain disconnected. There is no current HTTP application server, command contract, or mechanism for the agent to navigate the mock.
- Repository context documentation has been reconciled with the current code and Git history; stable development guidance now lives in `AGENTS.md`.

## Implemented Components

- Voice: WebRTC input/output, Whisper transcription, VAD start detection, speech-timeout turn completion, Ollama responses, Kokoro speech, automatic introduction, conversation context, and native per-stage/end-to-end latency observers.
- Frontend: Home, Dashboard Overview/Goals, Analytics, and Team views with hash navigation, responsive CSS, keyboard-operated dashboard tabs, and in-memory team filtering.
- The agent is intentionally brief and English-speaking and explicitly disclaims screen control.

## Current Constraints

- Frontend data is representative and in memory; there is no persistence or backend data API.
- Whisper emits completed utterances rather than streaming partial transcripts.
- End-to-end voice behavior depends on local models and system services and remains a manual verification path.

## Last Verified

- On 2026-07-27, `bot.py` passed Python byte-compilation without importing or starting the model stack.
- On 2026-07-27, the Vite production build passed under Linux nvm Node 24.
- The model-backed voice exchange and the README frontend interaction checklist were not run during this documentation phase.

## Relevant Files

- `bot.py`: current voice behavior and the restriction against claiming UI control.
- `client/src/App.jsx`: route names and interactions that a future command contract would target.
- `client/src/App.css`: responsive behavior that integration work must preserve.
- `AGENTS.md`: stable architecture, commands, validation, and working rules.

## Next Sequence

1. Run both manual checklists to establish a known voice and frontend baseline.
2. Define a minimal command schema using the existing route names and decide how commands will travel between Pipecat and React.
3. Implement one end-to-end navigation command before expanding the tool surface; preserve disconnected and invalid-command behavior.
4. Update the system instruction only after UI control is real, then add focused coverage for command mapping and failure cases.
