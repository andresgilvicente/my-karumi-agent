# Repository Guide

## Project Boundaries

- The repository has two connected surfaces: the Pipecat voice application in `bot.py` and a React/Vite product mock in `client/`. React connects directly to Pipecat's SmallWebRTC runner and exchanges RTVI UI state, semantic commands, and command results with the voice worker.
- `bot.py` is the only current application-owned Python entrypoint. Pipecat's runner supplies the WebRTC server and test client; the former FastAPI `server.py` was removed and port 8000 is no longer part of the application.
- The frontend has no router package, API client, data layer, or persistence. `client/src/App.jsx` owns the data, components, and hash routes: `#home`, `#dashboard`, `#dashboard-goals`, `#analytics`, and `#team`. Unknown hashes are replaced with `#home`.
- `session-ses_05d4.md` is historical session output, not a source of truth. Prefer current code, manifests, and lockfiles over historical prose.

## Relevant Layout

- `bot.py`: runtime configuration, system instruction, turn handling, metrics observers, Pipecat pipeline, connection handlers, and runner entrypoint.
- `client/src/ui-demo-catalog.json`: shared, versioned contract for the allowlisted semantic UI surface.
- `ui_control/catalog.py`, `ui_control/intents.py`, and `ui_control/processor.py`: catalog loading/validation, deterministic intent resolution, serialized command execution, function schemas, and context-safe deterministic responses.
- `ui_control/guided_demo.json`, `ui_control/guided_demo.py`, and `ui_control/guided_demo_processor.py`: declarative tour content, deterministic demo controls, interruptible session state, timers, and Pipecat turn/playback integration.
- `ui_control/turns.py`: generation-correlated, uninterruptible turn finalization for deterministic transcripts consumed before the LLM aggregator.
- `client/src/App.jsx`: all frontend views, state, interactions, and route definitions.
- `client/src/UICommandBridge.jsx`: RTVI UI state publication, semantic command handlers, idempotent command results, highlights, and scrolling.
- `client/src/VoiceControls.jsx` and `client/src/voiceClient.js`: WebRTC connection, microphone controls, voice activity status, and the configurable runner endpoint.
- `client/src/App.css`: the complete responsive visual layer; `client/src/main.jsx` mounts the Pipecat provider, app, and client audio output.
- `pyproject.toml`/`uv.lock` and `client/package.json`/`client/package-lock.json`: independent backend and frontend dependency boundaries.

## Runtime Configuration

- The pipeline order is `transport.input()` -> Whisper STT -> guided-demo control processor -> UI intent/tool-choice processor -> user context aggregator -> Ollama LLM -> Kokoro TTS -> `transport.output()` -> assistant context aggregator. Kokoro uses Pipecat's `MarkdownTextFilter` after sentence aggregation so formatting markers are not pronounced. Latency, metrics, and guided-demo playback observers observe the worker without being pipeline processors.
- Current settings are Whisper `base` on CPU with `int8`, Ollama `qwen2.5:1.5b`, Kokoro voice `af_heart`, English for STT, TTS, and responses, and a 1.0-second speech timeout for ending a user turn. Change these through the constants and `SYSTEM_INSTRUCTION` near the top of `bot.py`.
- The default architecture is fully local and requires no `.env` or API keys. A model-backed run still needs the local runtime prerequisites documented in `README.md`, including Ollama, the configured model, and `espeak-ng`.
- Local profiling is opt-in with `KARUMI_PROFILE=1`; it reuses Pipecat metrics and writes correlated JSONL events under ignored `profiles/` without changing normal logs. Use the fixed corpus and protocol in `docs/profiling-corpus.md`, and never commit machine-specific profile output.
- React publishes `ui_state` with a per-connection `uiSessionId`, `uiContractVersion`, supported commands/views/targets, and a monotonic revision. The backend owns authorization through the shared catalog, ignores stale revisions, allows extra frontend capabilities, and requires every action's capabilities before dispatch.
- Backend commands use `RTVIUICommandFrame` with the current `uiSessionId` and a unique `commandId`, wait up to five seconds for `ui_action_result`, and only treat a matching session, `status: ok`, command, view, and target as confirmation. Keep this acknowledgement requirement for every new UI action.
- UI execution is allowlisted: navigation to Dashboard/Analytics/Team, focus actions for twelve catalog targets, and Team member search. Dashboard exposes its four KPI cards and Performance Overview; Analytics exposes its four KPI cards, Traffic Overview, and Traffic Sources; Team exposes its member list. Duplicate labels such as Conversion Rate resolve against the current view unless the user names a view explicitly. Navigation performs the catalog sequence for its destination; Analytics centers and highlights Traffic Overview. Goals, analytics tabs, coordinates, directional scrolling, and generic screen control remain unsupported.
- Explicit supported requests are dispatched deterministically before Ollama because `qwen2.5:1.5b` can ignore required tool calls. Function tools remain a limited fallback, and Ollama continues to handle normal conversation. Logs identify `deterministic`, `function_call`, and `conversation` resolution paths.
- The backend owns an autonomous three-step guided demo: Dashboard -> Analytics -> Team. It offers the tour after the compatible React state arrives, starts after five seconds of silence with a spoken warning, and advances four seconds after each completed explanation. All timing and copy are declarative in `ui_control/guided_demo.json`.
- Guided-demo controls (`start`, `continue`, `pause`, `resume`, `stop`, and `restart`) are resolved deterministically. User speech cancels pending advancement, normal questions still reach Ollama, and advancement resumes only after playback ends. Actions remain serialized and confirmation-gated.
- Voice reconnection reuses one Pipecat client, serializes connection operations, and holds reconnect for a short transport-settling window after every disconnect. Session generations and UI session nonces prevent old actions or acknowledgements from crossing into the replacement session.

## Development And Validation

- Python is constrained to 3.12 by `.python-version`, `pyproject.toml` (`>=3.12,<3.13`), and `uv.lock` (`==3.12.*`). Use `uv sync` from the repository root and run the backend with `uv run bot.py -t webrtc`.
- `.nvmrc` selects Node 24 while `client/package.json` accepts Node >=22.12. Use npm inside `client/`: `npm ci` for installation, `npm run dev` for development, `npm run build` for a production build, and `npm run preview` to serve that build.
- Under WSL, load Linux nvm with `source "$HOME/.nvm/nvm.sh"`, run `nvm use`, and confirm `command -v node` and `command -v npm` resolve under `/home/...`, not `/mnt/c/...`. Windows npm fails against this UNC workspace path.
- Pipecat normally exposes its test client at `http://localhost:7860/client` and status at `/status`; Vite normally uses `http://localhost:5173`. Treat the URLs printed by the running tools as authoritative rather than assuming those ports are free.
- The semantic UI suite uses Python's standard-library `unittest`: run `uv run python -m unittest discover -s tests`. No linter, formatter, typechecker, pre-commit hook, task runner, frontend test runner, or CI workflow is configured. Do not invent commands such as `pytest`, `npm test`, or `npm run lint`.
- Profiling tests use temporary output paths. A runtime profile can be smoke-tested with `KARUMI_PROFILE=1 KARUMI_PROFILE_PATH=/tmp/karumi-profile.jsonl uv run bot.py -t webrtc`; profile files include local transcriptions only when explicitly enabled.
- For backend-only edits, first run the import-independent syntax check `uv run python -m py_compile bot.py`. Run the full model-backed voice flow only when the change warrants its heavier local prerequisites.
- For frontend edits, run `npm run build` under Linux Node inside `client/`, then manually exercise affected routes and interactions using the `Validate Frontend` checklist in `README.md`.
- Voice and UI end-to-end verification is manual in Chrome through the React app. Click `Connect voice`, verify the proactive offer, the silent Dashboard -> Analytics -> Team flow, interruption with a question, pause/resume, stop/restart, and direct navigation during a tour. Every successful action must log its matching UI confirmations before the explanation. Pipecat's test client remains useful for voice-only isolation but cannot acknowledge UI actions.

## Working Rules

- Inspect the relevant implementation and executable configuration before editing; do not rely on this file or `README.md` when they conflict with code.
- Prefer the smallest change that solves the task and do not add dependencies unless the existing stack cannot reasonably support it.
- Keep the AI stack local by default. Do not introduce hosted services, telemetry, secrets, or API-key requirements unless explicitly requested.
- Do not claim UI success before React confirms the matching `commandId`; report errors and timeouts honestly.
- After each change, run the cheapest relevant validation above; clearly distinguish automated checks from manual or unexecuted verification.
- Update `README.md` when user setup or runtime behavior changes, this file when stable architecture or workflow changes, and `PROJECT_STATE.md` when a meaningful development phase completes.
- Do not edit `uv.lock`, `client/package-lock.json`, or generated `client/dist/` assets manually. Use uv, npm, or the Vite build command that owns them.
