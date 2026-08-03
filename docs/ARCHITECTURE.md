# Architecture

> Phase 0 deliverable. Written before the implementation and updated as phases land.
> Measured numbers in this document come from real runs; anything unproven is marked as
> such here and tracked in [`LIMITATIONS.md`](LIMITATIONS.md).

---

## 1. The problem

Build a voice agent that gives **live demos of web products whose source code it does not
have**. Stage 1 targets Metric Master Suite; Stage 2 accepts an arbitrary URL and learns the
product while the agent is still saying hello.

The user must be able to talk naturally, ask what is on screen, ask about a feature or a
number, ask to navigate or scroll, ask to point at something, ask for a safe interaction,
interrupt mid-sentence, and refer back with "that", "this chart", "the button on the right".

The agent must observe the real page, understand it, resolve those references, act, **verify
what actually happened**, remember where it has been, avoid inventing anything, and keep
working when the DOM shifts underneath it.

### Constraints that shaped everything

| Constraint | Consequence |
|---|---|
| We do not control the target site's code | Observation must be injected from the browser side, not cooperatively embedded |
| No hardcoded selectors, routes, or element catalogues | The page must be addressed through roles and accessible names |
| No clone of the target frontend | The demo surface must be the real site |
| Never claim an action worked without checking | Verification is a first-class step, not a log line |
| Never hand the model the whole DOM | The representation must be semantic and budgeted |
| Sub-second from end-of-speech to first audio | Browser work must sit off the speech path |

---

## 2. Research findings

Sources were the primary ones: the `pipecat-ai/pipecat` source at v1.7.0 (published
2026-08-01) and the published types of `@pipecat-ai/client-js` 1.13.0. `docs.pipecat.ai`,
`playwright.dev` and GitHub's HTML interface are blocked by this environment's egress policy,
so nothing below rests on secondary documentation.

### 2.1 Pipecat's UI Worker is exactly the abstraction this problem needs

`pipecat.workers.ui.UIWorker` is an LLM worker that holds an accessibility snapshot, renders
it into the model's context as `<ui_state>`, and drives the page with typed commands.

The rendered format (from `UIWorker.render_ui_state` and `_render_node`) is Playwright-MCP
style:

```
- role "name" = "value" [level=N] [cols=N] [rows=N] [state] [ref=eN]:
```

The wire model (`pipecat.processors.frameworks.rtvi.models`):

```
A11ySnapshot  { root: A11yNode, captured_at: int, selection: A11ySelection? }
A11yNode      { ref, role, name?, value?, state[], level?, colcount?, rowcount?, children[] }
A11ySelection { ref, text, start_offset?, end_offset? }
```

Built-in commands: `scroll_to`, `highlight`, `select_text`, `click`, `set_input_value`, plus
`send_command(name, payload)` for app-defined ones. `UI_STATE_PROMPT_GUIDE` ships the system
prompt text that teaches a model the format, including `[offscreen]` semantics and the
promise that refs are stable across snapshots.

`captured_at` is the freshness token the staleness rules key off.

### 2.2 The integration point is the worker bus, not RTVI

`pipecat/bus/ui/messages.py` states plainly that the bus carriers are *not* the client wire
format, and that `PipelineWorker` translates between the two. Critically:

- `UIWorker.on_bus_message` stores a snapshot whenever a `BusUIEventMessage` arrives whose
  `event_name` is `__ui_snapshot` — **regardless of where it came from**.
- `UIWorker.send_command` publishes a `BusUICommandMessage` that any peer on the bus can
  consume.
- `BaseWorker.send_bus_message` is public API.

**So the React client is not required.** A plain `BaseWorker` can join the bus, publish
snapshots produced by a browser we drive, and execute the commands the UI worker emits. We
reuse the entire official protocol with a real browser standing in for the usual client.

### 2.3 Pipecat's accessibility walker is injectable

`@pipecat-ai/client-js` exports `snapshotDocument()`, `findElementByRef()`,
`findRefForElement()`, `serializeSelection()` and `A11ySnapshotStreamer` as standalone
functions with no React and no transport dependency — the streamer's own docstring says
"Usage (vanilla JS / any framework)". It emits on DOM mutation, focus change, scroll-end,
resize and visibility change.

Its refs are **stable across snapshots while an element remains mounted** — stronger than
Playwright MCP's refs, which are only guaranteed within a single snapshot. That single
property is what makes "the button I mentioned earlier" resolvable across turns.

We inject that bundle into the target page with `addInitScript`. The site is not modified;
the script lives in the browser realm we control.

### 2.4 The rest of the stack

- `UserBotLatencyObserver` provides `on_latency_measured`, `on_latency_breakdown` (per-service
  TTFB, user-turn duration, function-call latency) and `on_first_bot_speech_latency`.
- `LocalSmartTurnAnalyzerV3` runs a bundled `smart-turn-v3.2-cpu.onnx` with only
  numpy/onnxruntime/soxr — local turn detection, no network, no `transformers`.
- Deepgram Flux is integrated (`DeepgramFluxSTTSettings`) with end-of-turn detection inside
  the model, removing a separate VAD stage.
- `SmallWebRTCTransport` and the development runner are present; PyPI extras exist for every
  provider in the reference stack.

---

## 3. Alternatives evaluated

### 3.1 Browser control

| Option | Semantic snapshot | Stable refs | Overlay | Latency cost | Verdict |
|---|---|---|---|---|---|
| **Playwright, headed** | injected walker | yes | trivial | ~0 | **chosen** |
| Raw CDP | manual | manual | manual | ~0 | Playwright already wraps this; reimplementing buys nothing |
| Chrome extension | yes | yes | yes | ~0 | Extra build/install step, MV3 limits, weaker automation surface |
| Iframe embedding | no | no | no | — | Cross-origin blocks everything. Non-starter |
| Browserbase / remote browser | yes | yes | yes | + network | No value for a local demo; adds a dependency and latency |

Playwright wins on the combination, not on familiarity: `addInitScript` injects into any
origin before page script runs; actuation uses real trusted input with actionability checks;
frames, popups and SPA navigation are handled natively; and because the browser is headed,
**the demo surface is the browser** — no encoding, no streaming, no lag.

### 3.2 Where the browser runs

Server-side headless with a CDP screencast into a web page would make the demo shareable in
one tab, but costs 100–300 ms of visual lag plus an encode/transport pipeline. For a live
demo the presenter is in the room or on a screen share, so the lag buys nothing. Rejected in
favour of a headed local Chromium with an injected overlay.

### 3.3 Reimplementing the walker

Vendoring or rewriting the accessibility walker would cut ~90 kB from the injected bundle.
Rejected: refs allocated by the *same* code the official UI Worker client uses, with the same
stability guarantee, is the property the whole design rests on. A divergent fork of the
observation layer is a worse trade than inert bytes. See §7.1.

---

## 4. Chosen architecture

```
┌─ Chromium, headed, operator's machine ───────────────────────────────┐
│  The real target site, unmodified                                    │
│   ├─ addInitScript: Pipecat a11y walker + command/describe surface   │
│   └─ addInitScript: Karumi overlay (status pill, captions, ring)     │
└──────────┬─────────────────────────────────────┬─────────────────────┘
   exposeBinding(snapshot)              Playwright trusted input
           ▼                                     ▲
┌─ BrowserWorker (BaseWorker, no pipeline) ────────────────────────────┐
│  publishes BusUIEventMessage("__ui_snapshot", A11ySnapshot)          │
│  consumes BusUICommandMessage → policy → act → verify → evidence     │
└──────────┬───────────────────────────────────────────────────────────┘
           │  WorkerBus (in-process AsyncQueueBus)
   ┌───────┴────────┬──────────────────────────┐
   ▼                ▼                          ▼
UIWorker        VoiceWorker              KnowledgeWorker
LLM over        PipelineWorker:          Stage 2: bounded crawl,
<ui_state>,     SmallWebRTC + Flux       incremental semantic map
typed tools     + Haiku + Sonic
                     │
              Voice client (local page, SmallWebRTCTransport)
```

### 4.1 Three separated knowledge channels

Karumi asked explicitly for this, and it is enforced by type, not convention:

1. **Observed** — `<ui_state>`. Untrusted, ephemeral, always labelled as page content.
2. **Editorial** — `config/products/*.yaml`: demo goals, value messages, authorised facts,
   claims to avoid. Trusted, version-controlled, human-authored.
3. **Verified action results** — `ActionResult` + `Evidence`. Produced only by observation.

Nothing merges them. A page cannot promote itself into the editorial channel, and the agent
cannot report an action outcome that did not come from evidence.

### 4.2 Observation path

The walker runs in every frame and pushes snapshots through `exposeBinding`. The host:

1. Namespaces refs per frame (`f0.e7`) — the walker allocates refs per JS realm, so two
   frames both hand out `e7`. Doing this host-side, where we know which frame spoke, avoids a
   race against the first snapshot that in-page namespacing would introduce.
2. Merges child-frame trees under their host `<iframe>` node when it can be located, and at
   the root otherwise. Losing position is cosmetic; losing content would not be.
3. Prunes to a node budget — offscreen subtrees first, then a depth cap. Visible content is
   never elided.
4. Assigns a host-side `snapshot_id`, keeps the last 24, and publishes.

### 4.3 Action path

```
Action (typed, carries snapshot_id)
  → freshness check      unknown / expired / navigated-since → STALE_SNAPSHOT
  → resolve ref          gone → REF_NOT_FOUND
  → policy               capability → sensitivity → navigation scope
  → execute              Playwright trusted input
  → settle               KARUMI_BROWSER_SETTLE_MS
  → re-observe + diff    → Evidence
  → verdict              per-action rule → verified: bool
```

`ok` means dispatched. `verified` means the page agreed. Only `verified` licenses the agent to
say something happened; `ActionResult.summary()` spells out the difference in words the model
is expected to repeat.

---

## 5. The twenty decisions

| # | Question | Answer |
|---|---|---|
| 1 | Where does the browser run? | Headed Chromium on the operator's machine, driven by Playwright |
| 2 | Does the user see it directly? | Yes. No representation, no streaming |
| 3 | How is page state obtained? | Pipecat's a11y walker, injected with `addInitScript`, pushed via `exposeBinding` |
| 4 | How is it converted for UI Worker? | No conversion — the walker already emits `A11ySnapshot` |
| 5 | Stable refs after DOM change? | Walker WeakMap keyed on live elements; **measured: 5/5 refs held over 20 mutating snapshots** |
| 6 | How does the browser get commands? | `BusUICommandMessage` → BrowserWorker → resolve ref → Playwright |
| 7 | How is success confirmed? | Before/after snapshot diff + element state → `Evidence`, per-action verdict |
| 8 | How are stale snapshots handled? | Every action carries `snapshot_id`; unknown, >60 s old, or navigated-since is refused |
| 9 | Tabs, modals, iframes, SPAs? | `context.on("page")` adopts popups; walker runs per frame with namespaced refs; modals are ordinary tree nodes; SPA changes are detected by ref-set delta, not by navigation events |
| 10 | A11y + DOM + text + vision? | Accessibility first, always. DOM only for verification. Vision only on demand, for charts with no accessible data |
| 11 | What does the LLM see? | Pruned `<ui_state>`, editorial config, action summaries. Never HTML, CSS, internal attributes, or credentials |
| 12 | Avoiding full HTML? | The walker collapses to nodes with accessible roles; a hard node/depth budget is enforced on top |
| 13 | Incremental semantic map? | Stage 2 `SemanticSiteModel`: pages + link graph + evidence, session-scoped cache, bounded by domain/depth/count |
| 14 | Conversation vs interface split? | `VoiceWorker` owns speech and interruption; delegates screen-grounded turns to `UIWorker` via the `respond` job |
| 15 | What runs in parallel with speech? | Snapshotting, action execution, verification, Stage 2 crawling, connection warm-up |
| 16 | Prompt injection from the page? | Page text arrives only inside `<ui_state>` as a labelled developer message; capabilities live in tool schemas, so no page text can widen them |
| 17 | What needs human confirmation? | Form submits, purchases, deletions, publishing, downloads, account changes, off-origin navigation |
| 18 | How is the agent bounded? | Capability allowlist (`read`, `scroll`, `highlight`, `navigate`, `click`, `fill`). Anything else has no schema to call |
| 19 | Auth, cookies, secrets? | Optional pre-authenticated persistent profile; password/file/hidden inputs are never written and password values are never read back; secrets only via env |
| 20 | How is latency measured? | `UserBotLatencyObserver` + per-stage timers on every action, to structured JSONL, with a percentile report |

---

## 6. Latency budget

Target: **under one second** from end of user speech to first agent audio.

| Stage | Target P50 |
|---|---|
| End of speech → end-of-turn (Deepgram Flux, model-integrated) | ~250 ms |
| Final transcript → first LLM token (Haiku 4.5) | ~150 ms |
| Routing / tool decision | ~20 ms |
| First token → first audio (Cartesia Sonic TTFB) | ~90 ms |
| WebRTC network + jitter | ~80 ms |
| **Total** | **~590 ms** |

Browser work is **not** on this path. Measured on the fixture, headless:

| Stage | Measured |
|---|---|
| Steady-state observation (full re-walk, 2 frames, 85 nodes) | p50 **14 ms**, p90 24 ms, p95 **33 ms** |
| Action execution (click) | 40–130 ms |
| Verification (settle + re-observe + diff) | ~370 ms, dominated by the 350 ms settle delay |
| Cold start (launch + navigate + first walk) | ~900 ms, paid once |

When an action needs longer than the speech budget, the agent starts speaking honestly
("let me open the analytics section…") while the action runs, and confirms only after
verification. That is a design rule, not a fallback.

---

## 7. Trade-offs and risks

### 7.1 The injected bundle carries the whole client SDK (accepted)

`@pipecat-ai/client-js` is a Parcel build whose exports are registered through
`$parcel$exportWildcard` — a module-scope side effect that defeats tree-shaking in every
bundler. Importing `snapshotDocument` pulls in the entire SDK: 90 kB minified, including a
`PipecatClient` class we never instantiate.

Accepted, with the risk bounded by verification rather than assumption:

- `build.mjs` fails the build if any capability-bearing code (`RTCPeerConnection`,
  `getUserMedia`, `getDisplayMedia`, `new WebSocket`, `sendBeacon`) appears in the output.
- `tests/integration/test_injected_bundle.py` loads it in a real browser and asserts it adds
  no globals beyond its own, opens no connections, keeps its overlay out of the accessibility
  tree, and leaves page content untouched.

### 7.2 We depend on a private Pipecat constant (accepted, pinned)

`_UI_SNAPSHOT_BUS_EVENT_NAME` (`"__ui_snapshot"`) is private API. `pipecat-ai` is pinned to
`>=1.7.0,<1.8` and the dependency will be isolated to a single adapter module in Phase 2, so a
break is one file and one failing test rather than a silent loss of vision.

### 7.3 Freshness is deliberately not "latest snapshot wins"

The walker emits a snapshot every few hundred milliseconds. Rejecting any action whose
`snapshot_id` is not the newest would reject essentially everything. The rule is instead
about whether the ground actually moved: unknown id, older than 60 s, a navigation since, or
a target that has left the DOM. `test_a_current_snapshot_is_accepted` pins the usability side
of that trade so a future tightening cannot silently break ordinary use.

### 7.4 Role vocabulary is mixed

The walker reports a blend of ARIA roles and tag-derived names — `<nav>` surfaces as `nav`,
not `navigation`. Queries match both spellings; matching only the ARIA name silently misses
most real sites, which use semantic HTML. This was found by running the spike, not by reading
the source.

### 7.5 Open risks

| Risk | Mitigation |
|---|---|
| Metric Master Suite unreachable from this environment | Validated against a hostile local fixture; real-site run is an operator step, tracked in LIMITATIONS |
| Charts with no accessible data | Fixture proves ARIA-labelled canvases work; unlabelled ones need the vision path (Phase 3+) |
| Cross-origin iframes | Same-origin proven; cross-origin is untested (see LIMITATIONS) |
| Haiku 4.5 may under-reason over large `<ui_state>` | Phase 2 measures it before any decision to escalate the model |

---

## 8. Implementation plan

| Phase | Scope | Status |
|---|---|---|
| 0 | Architecture document, limitations doc | **done** |
| 1 | Browser spike: observe, act, verify, refuse — no voice | **done, 9/9 probes** |
| 2 | UI Worker over text: Pipecat `UIWorker` + `BrowserWorker` on the bus, typed tools | next |
| 3 | Voice: WebRTC, Flux, Haiku, Sonic, interruption, latency observer | |
| 4 | Guided Metric Master Suite demo with editorial config | |
| 5 | Arbitrary URL: progressive exploration, semantic map, grounded answers | |

---

## 9. Acceptance criteria

**Phase 1 — met.**

| Criterion | Result |
|---|---|
| Semantic representation of an unknown page | 85 nodes, 2 frames, headings/controls/tables/canvas all legible |
| Refs survive DOM mutation | 5/5 held across 20 snapshots under a 500 ms mutation loop |
| Cross-frame refs stay distinct | `f0.*` and `f1.*` namespaces verified |
| Act via semantic ref | scroll, highlight, click all confirmed by evidence |
| Detect resulting state change | title/url/scroll/ref-delta/element-state diffing |
| Reject stale commands | unknown id and post-navigation id both refused |
| Refuse dangerous actions | destructive click → confirmation; password field → blocked |
| Record timings | every action reports observe/execute/verify; p50/p90/p95 reported |
| p95 observation < 200 ms | **33 ms** |

**Phases 2–5** — criteria defined when each phase starts, to avoid writing tests against a
design that measurement has not yet justified.

---

## 10. Project structure

```
src/karumi_agent/
  domain/          models, ui_state rendering + budgeting, structural queries
  browser/         adapter port, Playwright implementation, verification
    injected/      the page-side bundle (npm workspace) + build guard
  tools/           policy (capability allowlist, sensitivity, navigation scope)
  observability/   structured logging
  spike.py         Phase 1 probes
  cli.py           entry point
config/products/   editorial knowledge, per product
tests/
  unit/            pure logic, no browser
  integration/     real Chromium against the fixture
  fixtures/site/   deliberately hostile local SPA
```

Typed throughout (`mypy --strict` clean), env-driven config, `structlog` to JSONL, `uv` with
a lockfile. No database, no queues, no microservices.
