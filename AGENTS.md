# Engineering Guide

## Project Purpose

Karumi Agent is a fully local, voice-guided product demo of Metric Master Suite. A user speaks to a Pipecat worker, receives a spoken answer, and can ask the worker to perform a deliberately narrow set of semantic actions in the connected React interface. The product requirement is not merely to issue UI commands: React must prove the requested postcondition before the agent reports success.

Keep installation and operator setup concise here. Use `README.md` for prerequisites, first-time setup, run instructions, and troubleshooting.

## Architecture

- React 19 and Vite 8 render the product mock. `client/src/App.jsx` owns in-memory product data, views, interactions, and hash routing; there is no router package, persistence layer, or product-data API.
- React connects directly to Pipecat's SmallWebRTC runner. RTVI carries React `ui_state`, backend semantic commands, and React `ui_action_result` confirmations over that connection.
- `bot.py` creates the local voice pipeline: transport input -> Whisper STT -> guided-demo control -> deterministic UI intent/tool-choice control -> user context -> Qwen through Ollama -> Kokoro TTS -> transport output -> assistant context.
- Whisper `base` runs on CPU with `int8` English transcription. Qwen handles normal conversation through Ollama. Kokoro uses the English `af_heart` voice and a Markdown text filter before speech synthesis.
- Explicit allowlisted UI requests are resolved before Qwen. The deterministic resolver owns semantic UI dispatch; catalog-derived function tools are only a limited fallback. Qwen must not choose arbitrary UI elements or own guided-demo state.
- The guided controller owns the autonomous Dashboard -> Analytics -> Team sequence, proactive offer, silence timers, playback-aware advancement, deterministic controls, interruption handling, and external-action synchronization.
- Optional local profiling observes turn, model, audio, resolution-path, and confirmed-action timing and writes correlated JSONL. It is observational and must not alter pipeline behavior.
- Contract v4 supports navigation, twelve focus targets, Team member search, and Team search clear. Clear has an explicit confirmation matcher requiring the Team view and `cleared: true`; a confirmed clear also synchronizes an active tour to Team. Both paths have automated coverage, while current Chrome validation is pending.

## Repository Structure

- `bot.py`: runtime constants, system instruction, Pipecat services and pipeline, RTVI event handling, confirmation correlation, session lifecycle, and runner entrypoint.
- `client/src/ui-demo-catalog.json`: the single shared, versioned semantic UI contract loaded by both React and Python.
- `client/src/App.jsx` and `client/src/App.css`: product views, state, stable semantic element IDs, interactions, and responsive presentation.
- `client/src/UICommandBridge.jsx`: catalog-derived capability publication, command handlers, DOM postcondition checks, idempotency, and `ui_action_result` emission.
- `client/src/VoiceControls.jsx` and `client/src/voiceClient.js`: connection lifecycle, microphone state, SmallWebRTC transport, and runner endpoint configuration.
- `ui_control/catalog.py`: shared catalog loading, validation, capability checks, and coherent confirmation matching.
- `ui_control/intents.py`: narrow deterministic phrase and alias resolution.
- `ui_control/processor.py`: deterministic turn handling, serialized simple and compound actions, function fallback schemas, and confirmation-derived responses.
- `ui_control/guided_demo.json`: declarative tour copy, timing, controls, and ordered steps.
- `ui_control/guided_demo.py` and `ui_control/guided_demo_processor.py`: tour state machine, timers, playback and transcription integration, and external-action synchronization.
- `ui_control/turns.py`: generation-correlated finalization of deterministic turns before the LLM aggregator.
- `ui_control/profiling.py`: opt-in local JSONL profiling.
- `tests/`: standard-library tests for UI control, guided-demo behavior, profiling, session isolation, and failure races.
- `docs/`: focused manual and profiling protocols. Dependency boundaries are `pyproject.toml`/`uv.lock` and `client/package.json`/`client/package-lock.json`.

## Development Commands

Use Python 3.12 and Node 24. See `README.md` for system prerequisites.

From the repository root:

```bash
uv sync
uv run python -m py_compile bot.py ui_control/*.py
uv run python -c "import bot"
uv run python -m unittest discover -s tests
uv run bot.py -t webrtc
```

For an opt-in profiling smoke run:

```bash
KARUMI_PROFILE=1 KARUMI_PROFILE_PATH=/tmp/karumi-profile.jsonl uv run bot.py -t webrtc
```

Load Linux nvm, select Node 24, and verify that Node and npm resolve under `/home/...`, not `/mnt/c/...`:

```bash
source "$HOME/.nvm/nvm.sh"
nvm use
command -v node
command -v npm
```

From `client/`:

```bash
npm ci
npm run dev
npm run build
npm run preview
```

No linter, formatter, typechecker, frontend test runner, or CI command is configured. Do not invent `pytest`, `npm test`, or `npm run lint`. Perform current browser verification from `docs/FINAL_CHECKLIST.md` in Chrome; Pipecat's diagnostic client cannot confirm product UI actions.

## Core Invariants

- Shared contract: `client/src/ui-demo-catalog.json` is the only semantic UI command, view, target, capability, phrase, and alias contract. Python loads that exact file. Guided-demo controls remain owned by `ui_control/guided_demo.json`. Never create a second frontend or backend allowlist.
- Semantic control only: commands carry allowlisted view IDs, target IDs, or Team search text. Never add coordinate commands, directional screen control, or model-generated DOM/CSS selectors.
- Confirmation before success: never claim or synthesize UI success until React returns a coherent result for the pending action. Match the current `uiSessionId`, unique `commandId`, command, `status: ok`, and command-specific view, target, query, count/name, or clear postcondition.
- Command identity: generate a new unique `commandId` for every concrete client command, including each member of a compound action. A command ID must never cross sessions or identify two payloads.
- Timeout: every concrete command has its own five-second confirmation timeout. Remove timed-out pending entries and report failure; never accept a later result as success.
- Serialization: all semantic actions, including tour and compound actions, pass through the shared executor lock. Preserve command order, stop a sequence on its first failure, and recheck session generation between commands.
- Late-result rejection: ignore unknown, completed, timed-out, stale-generation, and old-session confirmations. An incoherent result must resolve as `confirmation_mismatch`, not partial success.
- Session cleanup and reconnect: connection replacement or disconnect must reject pending actions, increment generation, clear UI state and conversational/tool state, cancel tour timers/tasks, and prevent old callbacks from mutating the new session. React must rotate its UI session nonce, clear idempotency state, and serialize reconnect operations through the transport-settling window.
- Search truth: Team results come only from React's confirmed `matchCount` and `matchedNames`. Qwen may phrase confirmed data but cannot invent names, counts, or search success.
- Deterministic UI versus conversation: explicit UI intents and guided-demo controls are consumed before Ollama. Normal questions go to Qwen. Qwen must not advance, pause, complete, or narrate success for controller-owned actions.
- Idempotency: React stores in-flight and completed results by `commandId`. An exact retry returns the same result; reuse with another request returns `command_id_reused`; a stale session emits nothing into the replacement session.
- Capability authorization: dispatch only against a compatible contract and currently advertised command, view, and target capabilities. Ignore stale `ui_state` revisions.
- Source ownership: do not manually edit lockfiles or generated `client/dist/` output. Use uv, npm, and Vite to own generated artifacts.
- Repository history: do not commit, amend, push, or create a pull request automatically. Do so only when the user explicitly asks.

## Extending The UI Contract

Make the smallest coherent change. Any action is incomplete until its real React postcondition can be acknowledged and the backend validates that acknowledgement. Bump `uiContractVersion` when the command, capability, payload, result, view, or target wire contract changes; do not bump it for copy-only edits.

### Add A View

1. Implement the route and semantic destination element in `App.jsx`; preserve hash navigation and unknown-route behavior.
2. Add the view once in `ui-demo-catalog.json`, including hash, destination element ID, aliases, navigation aliases, default action, ordered navigation actions, and success copy.
3. Ensure every presentation target belongs to the new view and React publishes the view and visible targets from the catalog.
4. Add catalog, intent, capability, navigation sequence, failure, and confirmation-mismatch tests.
5. Run the Vite build and verify in Chrome that React confirms the destination and every presentation command before speech reports success.

### Add A Target

1. Give the rendered element a stable semantic `id`; do not derive selectors in the model or backend.
2. Add the target once to the catalog with its view, label, resolver aliases, default command, allowed commands, and success copy.
3. Confirm that `pageState()` advertises it only when visible and that bridge handlers validate its catalog view and allowed command.
4. Test catalog validation, positive intent resolution, wrong-view rejection, ambiguity, compound focus ordering, timeout, and coherent target confirmation.
5. Build React and manually confirm scroll/highlight postconditions and the matching result payload.

### Add Or Change Aliases

1. Change the relevant catalog phrase or alias list, not a duplicated backend list.
2. Check overlap with every view, target, Team search reserved term, clear phrase, and duplicate label.
3. Add positive, negative, split-transcript where relevant, current-view disambiguation, and explicit-view disambiguation tests.
4. Verify representative phrases through Chrome before treating speech recognition behavior as validated.

### Add An Intent

1. Define a narrow, explicit phrase surface in the shared catalog and decide its precedence relative to clear, search, target focus, and navigation.
2. Resolve it deterministically in `intents.py`; unsupported or ambiguous text must fall through to conversation rather than guess an action.
3. Route it through `UIActionToolChoiceProcessor` and build spoken output only from the executor's confirmed result.
4. Add positive, negative, ambiguity, previous-transcript, consumed-turn, session-change, and failure-response tests as applicable.
5. If a function fallback is needed, derive its schema choices from the catalog and keep deterministic dispatch authoritative.

### Add A Client UI Action

1. Add the command and payload metadata to the catalog and bump the contract version.
2. Extend catalog validation, required-capability calculation, and `confirmation_matches()` with an explicit command-specific postcondition.
3. Add a `UICommandBridge` handler using `runCommand`; validate the allowlist, await the actual React/DOM state, and return `status: ok` data only after that state is true.
4. Extend the serialized backend executor and confirmation-derived response. Never accept a generic `ok` as sufficient.
5. Test malformed catalog data, missing capability, mismatched session/command/result fields, timeout, late confirmation, ID reuse, disconnect, and session replacement.
6. Build React and inspect the matching `commandId` result in a current Chrome voice run.

### Add A Compound Action

1. Keep orchestration in `UIActionExecutor` under its existing shared lock; do not create an independent queue.
2. Express ordered presentation commands through catalog definitions where possible.
3. Give every emitted command a fresh ID and five-second timeout, abort on the first unconfirmed result, and preserve enough structured failure state to avoid a false overall success.
4. Synchronize the guided controller only from the final confirmed or explicitly screen-confirmed state intended by its state machine.
5. Test strict ordering, serialization against concurrent actions, intermediate failure, timeout, generation changes at every boundary, and response suppression after session replacement.
6. Verify that React confirms every member command before the compound success response.

### Add A Tour Step

1. Add declarative action, transition copy, and explanation to `ui_control/guided_demo.json`; the action must already be supported by the catalog and executor.
2. Extend validation only if the step schema changes; keep sequencing and ownership in `GuidedDemoController`, not Qwen.
3. Test silent progression, explicit controls, timer invalidation, playback completion, question interruption, direct external actions, action failure, stop/restart, and disconnect cleanup around the new step.
4. Run all Python tests and the Vite build.
5. Complete the Chrome tour matrix in `docs/FINAL_CHECKLIST.md`, checking each React confirmation before its explanation.

## Sensitive Areas

- Pipeline processor order in `bot.py` determines whether a turn is consumed deterministically or reaches Qwen. Reordering processors can duplicate context, tools, or speech.
- `pending_ui_actions`, `session_generation`, `uiSessionId`, connection handlers, and the five-second timeout form one safety boundary. Review them together.
- `UICommandBridge.jsx` combines asynchronous React updates, DOM settlement, idempotency, and disconnect epochs. A visual mutation alone is not confirmation.
- Guided-demo timers, playback callbacks, action tasks, transcript grace, and generation invalidation are race-sensitive. Test cancellation and stale callbacks, not only the happy path.
- `ConsumedUserTurnStopStrategy` and deterministic context writes preserve coherent transcripts when audio is interrupted. Do not bypass them for new deterministic responses.
- Search result payloads cross a trust boundary. Keep backend shape validation and React-derived names/counts strict.
- Profiling output can contain local transcriptions. Keep machine-specific JSONL under ignored paths and out of commits.
- Lockfiles and generated Vite assets must be changed only by their owning commands.

## Checklist Before Finalizing A Diff

- Inspect the executable implementation, shared catalog, tests, manifests, and existing worktree changes before editing.
- Confirm the change uses the shared catalog and introduces no duplicate allowlist, coordinates, generated selectors, or unconfirmed success path.
- Check command uniqueness, five-second timeout behavior, serialization, coherent confirmation matching, late-result rejection, idempotency, and disconnect cleanup for every affected action.
- Run `uv run python -m py_compile bot.py ui_control/*.py`, `uv run python -c "import bot"`, and `uv run python -m unittest discover -s tests` for backend or contract changes.
- Run `npm run build` from `client/` under Linux Node 24 for frontend or contract changes.
- Execute the affected Chrome cases in `docs/FINAL_CHECKLIST.md`; clearly label any unexecuted manual validation.
- Run `git diff --check` and review the complete diff without reverting unrelated worktree changes.
- Update the owning documentation, but do not commit unless explicitly requested.

## Documentation Ownership

- `README.md`: installation, prerequisites, normal run instructions, user-facing scope, and troubleshooting.
- `AGENTS.md`: stable architecture, engineering invariants, extension procedure, sensitive areas, and validation commands.
- `PROJECT_STATE.md`: short, dated snapshot of verified state, warnings, limitations, bugs, and next action.
- `PROJECT_JOURNEY.md`: development chronology and superseded phase history; link to it rather than accumulating history in `PROJECT_STATE.md`.
- `docs/DEMO_SCRIPT.md`: presenter sequence, exact showcase phrases, timing cues, and recovery steps.
- `docs/FINAL_CHECKLIST.md`: current manual Chrome, voice, UI, reconnect, and tour acceptance matrix.
- `docs/profiling-corpus.md`: fixed local performance corpus and collection protocol.
- `docs/guided-demo.md`: detailed guided-demo behavior and focused debugging notes.
