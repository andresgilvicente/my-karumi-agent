# Limitations and next steps

What is actually proven, what is assumed, and what is known not to work. Kept honest on
purpose: a demo agent that overstates its own reliability is the exact failure mode this
project is built to avoid.

Last updated at the end of **Phase 1**.

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

### Not yet built

Phases 2–5 are unimplemented: there is no LLM in the loop, no voice, no editorial config
consumption, and no Stage 2 URL learning. The `voice` extra installs the pipeline
dependencies, but no code imports them yet.

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

1. **Run the spike against Metric Master Suite** and record the result here (§1).
2. **Phase 2** — put a real LLM behind the snapshot and measure whether Haiku 4.5 reasons
   reliably over `<ui_state>` at this size before considering a stronger model for the
   interface path.
3. **Replace the fixed settle delay** with adaptive waiting (§2).
4. **Phase 3** — voice, and the first true end-to-end latency measurement. Publish the real
   P50/P90/P95 here whether or not they meet the sub-second target.
5. **Add the local/free provider configuration** as a benchmark counterpart to the cloud
   stack, comparing latency, tool-calling reliability, quality and cost.
