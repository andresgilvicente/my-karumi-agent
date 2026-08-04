# Architecture

## The problem

Give a live voice demo of a website whose source we do not control, cannot
modify, and have never seen. No cloned frontend, no hardcoded selectors, no
scripted click path. Everything local and free.

## Shape

```
operator ──▶ console (WebRTC mic) ──▶ Pipecat: VAD ─ Smart Turn ─ STT
                                                          │
                                                          ▼
   ┌──────────────────────────────────────────────────────────────┐
   │ DemoSession                                                  │
   │                                                              │
   │   Snapshotter ──▶ PageSnapshot ──▶ schema ──▶ Ollama         │
   │        ▲                                        │            │
   │        │                                        ▼            │
   │   BrowserSession ◀── ActionExecutor ◀── ActionRequest        │
   │        │                    │                                │
   │        │                    └──▶ safety gate, verification   │
   │        ▼                                                     │
   │   KnowledgeWorker (background site learning)                 │
   └──────────────────────────┬───────────────────────────────────┘
                              ▼
                    Pipecat: TTS ──▶ console ──▶ speakers
                              │
                    Playwright ──▶ headed Chromium (what the audience watches)
```

`domain/` is pure and does no I/O: the snapshot model, reference resolution,
staleness rules, safety classification and editorial validation are all testable
without a browser or a model, and they are where the rules that must not drift
live.

## Decisions

### Observation: Playwright's ARIA snapshot

`page.aria_snapshot(mode="ai", boxes=True)` returns a distilled accessibility
tree with stable `[ref=eN]` handles, clickability hints and element geometry, and
stitches same-origin iframes automatically.

Two properties we depend on, both verified by test:

* References are identity-based expandos keyed on role and accessible name, so
  they **survive same-document SPA navigation** — which matters, since the demo
  target is React Router.
* `mode="ai"` only assigns references to elements that are visible and receive
  pointer events. A **disabled control therefore has no reference at all**, which
  means the model cannot express clicking it. Safety by construction rather than
  by checking.

**Rejected:** raw Chrome DevTools Protocol (would mean rebuilding the actionability
checks, the reference engine and the tree distiller); a browser extension (MV3
isolated world, CSP, service-worker lifecycle, and still rebuilding the above);
an iframe (**the same-origin policy makes the child DOM unreadable**, regardless
of framing headers); attaching to the operator's own Chrome (Playwright's docs
call it "significantly lower fidelity", and it would put their real session one
action away from an agent on a shared screen).

### Reasoning: one inference, grammar-constrained

The model replies in a JSON schema built fresh for the current page and request:

```jsonc
{
  "say":    "string",                       // first -> streams to TTS immediately
  "action": {"enum": ["answer", ...]},      // "answer" first; narrowed by the
                                            //   verbs in the operator's speech
  "ref":    {"enum": ["e4", "e5", ..., "none"]}  // exactly what is on screen now
}
```

Three measurements drove this:

1. Unconstrained tool calling on a small model produced accessible names in the
   reference slot (`"Analytics"` instead of `e5`). The enum makes that
   inexpressible: 6/6 valid afterwards. Fully generic — the enum comes from the
   live page, which is the opposite of hardcoding.
2. The action field **collapsed to `click` for every input**, including plain
   questions. So the enum is ordered with `answer` first (small models bias to
   the first value) and narrowed by a prior taken from the operator's own verbs.
   A question with no imperative verb cannot produce a click.
3. Property order drives decoding order. `say` first means speech synthesis
   starts on the opening words: ~1 s to first speech instead of ~3 s.

Only **one** inference per turn. The conventional voice-model-delegates-to-UI-model
arrangement costs two, which at ~13 tokens/s is four to five seconds.

### Latency: prefix caching dominates

Measured, identical 1029-token prompt: cold prefill **6.69 s (154 tok/s)**, warm
**0.10 s (10,737 tok/s)**. A 70x gap, larger than every other factor combined.
Grammar compilation and enum size were both negligible by comparison.

So the design treats the prompt prefix as a resource:

* Prompt sections run **least volatile first** (editorial guidance, page state,
  per-turn notes, the operator's words). Putting the "already covered" list ahead
  of the page state cost ~7 s on the turn after every navigation, because
  appending one item invalidated everything behind it.
* The snapshot id is **not** in the rendered page-state header. It used to be,
  and because it changes on every observation it forced a cold prefill every
  single turn — 7.7 s versus 1 s in the benchmark that caught it. The model never
  needed it; the planner attaches the id from the store.
* After a navigation the new page state is **prefilled in the background**, so
  the operator's next question resumes a warm cache instead of paying for it.
* The model is warmed **with the real system prefix** at startup, not a throwaway
  prompt.

### Safety: structural, not instructed

A 1.5B model was measured **complying with a prompt-injection framing** during
development. The prompt does tell it that page content is untrusted data, but
nothing rests on it obeying:

| Layer | What it prevents |
|---|---|
| Closed action vocabulary, no JavaScript path | The model cannot express running code |
| Reference enum from the current snapshot | It cannot act on an element that is not there |
| Snapshot ids on every action | It cannot act on a stale observation |
| Sensitivity classifier over accessible names | Destructive-looking controls need confirmation |
| Intent classifier over the operator's words | Catches the case where the model picks an unrelated target while narrating a destructive one — an observed failure |
| Credential fields blocked outright | Passwords are never typed, never read into a prompt |
| Origin allowlist + redirect re-check | The agent cannot wander off the site |
| Dialog / download / popup interception | A page cannot trap or divert the demo |

The classifier reads the **accessible name the model already reasoned over**, so
it needs no selectors, works in any language it has patterns for, and stays
correct when a page relabels a control.

### Verification: evidence, not optimism

Every action returns a typed result carrying an id, the source snapshot, status,
evidence, observed changes, duration, and `verified: true|false`. `verified` is
only true when a fresh observation confirms the intended effect — a URL change, a
new element set, a value that matches, an element now in the viewport.

Settling is request-driven with hard caps, **not** `networkidle`: the demo target
holds an open WebSocket, so an idle-based wait would never resolve.

When verification fails the agent says so out loud. An agent that narrates
success it did not confirm is worse than one that admits uncertainty.

### Overlay: a glass pane over a page we do not own

One init script installs `<karumi-glass popover="manual">` with a **closed** shadow
root, styled through constructable stylesheets. That combination is CSP-immune
(CSSOM is not an element, so `style-src` does not apply), renders above every
z-index, and `pointer-events: none` means it never steals a click — and, neatly,
means Playwright never assigns it a reference, so the overlay is invisible to the
agent's own snapshot.

It is presentation only. Nothing the agent decides depends on it, and the
authoritative record is the JSONL run log.

## The local stack

| Layer | Choice | Why |
|---|---|---|
| Browser | Playwright + bundled Chromium, headed | Public ARIA snapshot API, actionability checks, interception |
| VAD | Silero (bundled, 2.3 MB) | No download, no torch |
| Turn detection | Smart Turn v3 (bundled, 8.7 MB) | ~12 ms on CPU; removes the fixed silence tax |
| Speech recognition | Parakeet via `onnx-asr`, Whisper fallback | Best accuracy per CPU cycle, and avoids CTranslate2 — which is stranded on CUDA 12 and *silently* falls back to CPU |
| Reasoning | Ollama, `qwen3:1.7b` | Only local server exposing JSON-schema-constrained decoding |
| Synthesis | Kokoro | Apache-2.0, chunked streaming output |

Deliberately avoided: Piper (GPL-3.0-or-later is a real redistribution
constraint), vLLM (worse single-user time-to-first-token, will not co-reside),
any vision model (no local VLM fits this latency budget).
