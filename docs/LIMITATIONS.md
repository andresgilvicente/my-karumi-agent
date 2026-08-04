# Limitations and next steps

What is actually proven, what is assumed, and what is known not to work. Kept honest on
purpose: a demo agent that overstates its own reliability is the exact failure mode this
project is built to avoid.

Last updated at the end of **Phase 2**.

---

## 0. The second big one: the agent has never spoken to a real LLM

**Status: unverified.**

Phase 2 is built and its plumbing is tested, but this development environment has no
Anthropic credentials — no `ANTHROPIC_API_KEY`, no `ant` profile, no SDK. Nothing here has
ever run a model.

What **is** proven, by `tests/integration/test_browser_worker.py` against a real bus and a
real browser:

- snapshots reach the bus in the exact shape `UIWorker.render_ui_state` reads
- page changes keep producing snapshots without anyone asking
- the `observe` and `act` jobs round-trip
- a verified action comes back verified, with its observed changes and timings
- a policy refusal comes back as a *result* the agent can explain, not an exception
- a stale snapshot id is refused over the bus
- a malformed tool call is reported, not raised — a bad call must not end the demo
- acting republishes, so the model's next turn sees the effect of its own action

What is **not** proven: that Claude Haiku 4.5 reasons well over a real `<ui_state>` — picks
the right ref, resists narrating the whole page, and repeats an unverified result honestly
instead of claiming success. That was the explicit question Phase 2 was meant to answer, and
it remains open.

**To close this gap**, with a key in `.env`:

```bash
uv run karumi text --url http://127.0.0.1:8899 --ask "What is this page about?"
uv run karumi text --url http://127.0.0.1:8899 --ask "Show me the analytics section"
uv run karumi text --url http://127.0.0.1:8899 --ask "Highlight the most important revenue metric"
```

Watch for three specific failures: the model inventing a ref that is not in the snapshot;
the model reporting success after a result whose `verified` is false; and the model reading
out the whole dashboard when asked about one number. Each has a different fix — tool schema,
prompt, and prompt respectively — so it is worth knowing which one you have.

---

## 1. The big one: not yet run against Metric Master Suite

**Status: unverified.**

`https://metric-master-suite505.apps.rebolt.ai/` is blocked by this development environment's
egress policy, along with general web browsing. Every measurement in this repository was taken
against the local fixture at `tests/fixtures/site/`.

The fixture was built to be *hostile in the ways real apps are hostile* — client-side view
switching with no page load, a DOM mutation loop, content below the fold, a canvas chart with
no text of its own, a modal, a same-origin iframe, a disabled control, and a
destructive-looking button. It is deliberately not a replica of Metric Master Suite, and
nothing in the agent's source refers to either site.

**To close this gap**, on a machine that can reach the target:

```bash
uv run karumi spike --url https://metric-master-suite505.apps.rebolt.ai/
```

All nine probes should pass. The ones most likely to surface something new are *semantic
representation* (does the app expose usable roles and names?) and *click navigation control*
(does it declare a navigation landmark?). Record the output in this file.

---

## 2. Measured numbers

From the fixture, headless, Chromium 141, 85 nodes across 2 frames.

| Metric | Value | Target | |
|---|---|---|---|
| Steady-state observation, p50 | 14 ms | — | |
| Steady-state observation, p95 | 33 ms | < 200 ms | ✅ comfortably |
| Cold start (launch + navigate + first walk) | ~900 ms | once per session | ✅ off the speech path |
| Action execution (click) | 40–130 ms | — | |
| Verification | ~370 ms | — | ⚠️ see below |
| Ref stability under mutation | 5/5 over 20 snapshots | 20+ | ✅ |

**Verification cost is dominated by a fixed 350 ms settle delay**, not by measurement. That
delay is a conservative guess at "long enough for an SPA to re-render", and it is currently
the largest avoidable number in the browser path. Phase 3 should replace it with an adaptive
wait — observe until the ref set stops changing, with the fixed delay as a ceiling. Until
then, an action takes roughly half a second to confirm, which is why the agent is designed to
speak *before* confirmation rather than after.

No end-to-end voice latency has been measured. The budget in
[`ARCHITECTURE.md` §6](ARCHITECTURE.md#6-latency-budget) is derived from published provider
figures, not from this system. **Treat it as a hypothesis until Phase 3 measures it.**

---

## 3. Known gaps

### Browser layer

- **Cross-origin iframes are untested.** Same-origin frames work and their refs are correctly
  namespaced (`f0.*` vs `f1.*`). `addInitScript` applies to all frames, so cross-origin
  frames should work identically, but nothing here proves it. A third-party embed that blocks
  scripting will simply be absent from the snapshot rather than break the run.
- **Shadow DOM is unverified.** The walker's traversal of shadow roots has not been exercised
  by the fixture. Components built on closed shadow roots may be invisible to observation.
- **Frame-subtree placement is best-effort.** A child frame's tree is spliced under its host
  `<iframe>` when that element can be located in the parent, and appended at the root
  otherwise. The fallback loses position, not content.
- **Popups are followed but not managed.** A new tab becomes the active page; there is no
  facility yet for going back to the previous one.
- **`canvas` content is only as readable as its ARIA labelling.** The fixture's chart is
  legible because it carries a descriptive `aria-label`. A chart with no accessible
  description is currently invisible to the agent — the on-demand vision path that would
  address this does not exist yet.
- **No adaptive waiting.** See §2.

### Policy

- **Sensitive-action detection is heuristic.** It matches accessible names against an
  English/Spanish pattern plus structural signals (`type="submit"`, committing roles). A
  destructive button labelled only with an icon, or labelled in another language, will not be
  caught by the name rule — though `type="submit"` and the disabled check still apply, and
  `click` can be withheld entirely via capabilities. Do not treat the name list as the
  security boundary; the capability allowlist is.
- **Confirmation is not yet wired to a human.** `NEEDS_CONFIRMATION` is returned and the
  action is refused. The interactive approval loop arrives with the voice layer in Phase 3.

### Worker layer (Phase 2)

- **The `text` prompt loop is untested end to end** for the reason in §0. Every layer beneath
  it is covered; the loop itself has only been run as far as the credential check.
- **Sequential actions only.** The `act` job is serialised on purpose — two clicks racing on
  one page make the before/after diff meaningless. A demo that wanted to fan out reads would
  need a separate, read-only job.
- **No confirmation round-trip yet.** A sensitive action returns `NEEDS_CONFIRMATION` and
  stops. Asking the human and resuming needs a channel to ask on, which arrives with voice.
- **Conversation history is not persisted.** `keep_history` is left at Pipecat's default, so
  the model sees the current turn and the injected snapshot. Cross-turn memory ("the button
  you showed me earlier") is a Phase 4 concern.

### Not yet built

Phases 3–5 are unimplemented: no voice, no guided demo flow, and no Stage 2 URL learning.
The `voice` extra installs the pipeline dependencies and the worker layer imports cleanly,
but nothing constructs a transport yet.

---

## 4. Dependencies and coupling

- **`_UI_SNAPSHOT_BUS_EVENT_NAME` is private Pipecat API.** The design publishes snapshots
  onto the worker bus under this name so `UIWorker` picks them up without an RTVI client.
  `pipecat-ai` is pinned to `>=1.7.0,<1.8`. A minor-version bump could rename it. Phase 2 will
  confine the dependency to one adapter module with a test that fails loudly.
- **The injected bundle ships the whole of `@pipecat-ai/client-js`** (90 kB minified) because
  its Parcel build cannot be tree-shaken. The dead code is verified inert both at build time
  and at runtime — see [`ARCHITECTURE.md` §7.1](ARCHITECTURE.md#71-the-injected-bundle-carries-the-whole-client-sdk-accepted).
- **Chromium build mismatch.** This environment ships Chromium 141 (build 1194) while
  Playwright 1.62 expects build 1234. The adapter auto-detects the preprovisioned binary. On a
  normal machine `uv run playwright install chromium` makes this moot.
- **NLTK 3.10 vs. an in-project virtualenv.** NLTK's CWE-427 import guard blocks
  NLTK-initiated imports of anything resolving inside the cwd, which is every package when
  `.venv` sits in the project — the uv default. Pipecat imports NLTK, so every Pipecat import
  from the project root fails without the shim in `workers/_pipecat.py`. Pipecat pins
  `nltk>=3.10`, so there is no version to retreat to. Covered by
  `tests/unit/test_pipecat_compat.py`.

---

## 4a. Two things Phase 2 corrected in Phase 1's work

Recorded because both slightly weakened claims made earlier.

- **Streamed snapshots never reached subscribers.** The page pushed snapshots on every
  mutation and the adapter stored them, but only `observe(force=True)` ever notified
  listeners — so nothing downstream saw a change until something asked. Phase 1 never
  noticed because the spike always forced. Fixed by publishing on the push path; the
  regression is pinned by `test_page_changes_produce_new_snapshots`.
- **The fixture's "mutation pressure" was attribute-only.** Its heartbeat wrote a `dataset`
  value, which accessibility walkers do not observe — so the tree was quiet and the Phase 1
  claim that refs survive "while the page mutated" was softer than it sounded. The heartbeat
  now removes and re-adds a real element. Re-running the spike against the harsher fixture
  still gives **5/5 refs held across 25 snapshots**, so the claim now stands on its own.

---

## 5. Deliberate non-goals

- **No guarantee of working on every website.** The design degrades in a controlled way: a
  page with poor accessibility semantics yields a thin snapshot and the agent will have less
  to say, rather than inventing content.
- **No reconstruction of the target frontend**, no scripted tour, no hardcoded element
  catalogue. If the site changes, the agent adapts or reports that it cannot find something.
- **No autonomous purchasing, publishing, deleting or account modification** — blocked or
  gated by design, not by prompt instruction.

---

## 6. Next steps, in order

1. **Run `karumi text` with a real key** and record what the model actually does (§0). This
   is the open question Phase 2 was built to answer.
2. **Run the spike against Metric Master Suite** and record the result here (§1).
3. **Replace the fixed settle delay** with adaptive waiting (§2) — the largest avoidable
   number in the browser path.
4. **Phase 3** — voice, and the first true end-to-end latency measurement. Publish the real
   P50/P90/P95 here whether or not they meet the sub-second target.
5. **Add the local/free provider configuration** as a benchmark counterpart to the cloud
   stack, comparing latency, tool-calling reliability, quality and cost.
