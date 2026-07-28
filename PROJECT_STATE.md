# Project State

Updated: 2026-07-28

## Current State

- The local MVP has two working surfaces: a standalone Pipecat voice conversation and a static Metric Master Suite React mock.
- The voice runtime and React remain disconnected. There is no current HTTP application server, command contract, or mechanism for the agent to navigate the mock.
- Voice-turn stabilization is complete for the current phase. Whisper `base` with a 1.0-second speech timeout is the retained baseline while UI integration proceeds.
- Repository context documentation has been reconciled with the current code and Git history; stable development guidance now lives in `AGENTS.md`.

## Implemented Components

- Voice: WebRTC input/output, Whisper transcription, VAD start detection, speech-timeout turn completion, Ollama responses, Kokoro speech, automatic introduction, conversation context, and native per-stage/end-to-end latency observers.
- Frontend: Home, Dashboard Overview/Goals, Analytics, and Team views with hash navigation, responsive CSS, keyboard-operated dashboard tabs, and in-memory team filtering.
- The agent is intentionally brief and English-speaking and explicitly disclaims screen control.

## Current Constraints

- Frontend data is representative and in memory; there is no persistence or backend data API.
- Whisper emits completed utterances rather than streaming partial transcripts.
- Whisper `base` still misses important product vocabulary: the latest run transcribed "Pipecat" as "pipe cut" and "Metric Master Suite" as "metric master shoot." Pipecat 1.6.0 does not expose faster-whisper's `initial_prompt` or `hotwords` options through `WhisperSTTService`; a custom integration is deferred.
- Kokoro response generation remains the main source of variable end-to-end latency, especially for longer answers.
- End-to-end voice behavior depends on local models and system services and remains a manual verification path.

## Last Verified

- On 2026-07-27, `bot.py` passed Python byte-compilation without importing or starting the model stack.
- On 2026-07-27, the Vite production build passed under Linux nvm Node 24.
- On 2026-07-28, a Chrome WebRTC run verified repeated end-to-end exchanges with Whisper `base` and a 1.0-second speech timeout. Clean warm turns reached final transcription in approximately 1.4-2.3 seconds after speech ended; one dashboard turn reached first bot audio in 4.284 seconds.
- The same run confirmed that longer responses can raise end-of-speech-to-first-audio latency to 8-18 seconds, with Kokoro TTFB accounting for most of the delay, and that the critical product vocabulary errors remain.
- The README frontend interaction checklist was not rerun during this voice-stabilization phase.

## Relevant Files

- `bot.py`: current voice behavior and the restriction against claiming UI control.
- `client/src/App.jsx`: route names and interactions that a future command contract would target.
- `client/src/App.css`: responsive behavior that integration work must preserve.
- `AGENTS.md`: stable architecture, commands, validation, and working rules.

## Next Sequence

1. Use the current `base`/1.0-second voice configuration as the known baseline; defer custom STT vocabulary support and TTS optimization unless they block UI integration.
2. Run the frontend interaction checklist, then define a minimal command schema using the existing route names and decide how commands will travel between Pipecat and React.
3. Implement one end-to-end navigation command before expanding the tool surface; preserve disconnected and invalid-command behavior.
4. Update the system instruction only after UI control is real, then add focused coverage for command mapping and failure cases.
