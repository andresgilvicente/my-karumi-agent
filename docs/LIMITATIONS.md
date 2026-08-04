# Limitations

Written honestly and kept current. If something does not work, or works worse
than the brief hoped, it is written down here rather than quietly scoped out.

## Latency: sub-1s to first audio is not reachable on this machine

The brief's aspirational target is under one second from end of user turn to
first audio. On a CPU-only Ryzen 7 5700U that is **not achievable**, and the
project does not pretend otherwise.

Measured components (see `docs/BENCHMARKS.md` for the full distribution):

| Stage | CPU-only, this machine |
|---|---|
| LLM TTFT, warm | ~0.40 s |
| LLM generation | ~15 tok/s for a 1.5B model |
| First spoken sentence (~15 tokens) | ~1.0 s |

Realistic end-to-end budget: **~1.5-2.5 s**. What we do about it is in the
architecture (warmup, prefix-stable prompts, short replies, streamed TTS,
parallel snapshot capture, event-based verification), and the benchmark reports
P50/P90/P95 rather than a single flattering number.

A machine with a CUDA GPU would improve this substantially. We have not measured
that, so we do not claim it.

## The CUDA path is implemented but untested

There is no NVIDIA GPU on the development machine. Hardware detection, the CUDA
profile, and GPU-capable model selection are all wired, and `karumi doctor`
reports what it finds. **None of it has been executed against a real GPU.** Treat
GPU performance as unverified.

## The demo target requires signing in

`https://metric-master-suite505.apps.rebolt.ai/` presents a sign-in wall at its
landing route (verified live: email + password fields, disabled "Sign In"
button, Convex `<Authenticated>` gating). The agent:

* detects a login wall **generically**, from a visible `input[type=password]`;
* says out loud that it is waiting for the operator to sign in;
* never types a credential, and never puts one in a prompt;
* keeps a persistent browser profile, so signing in once survives restarts.

Consequently the **signed-in views of the target have not been exercised by
automated tests here.** Everything behind the wall depends on the operator's own
session.

## Charts on the target are largely undescribable

The target ships no chart library; its visualisations are hand-rolled SVG/CSS
with no accessible names. An accessibility-tree-based agent cannot read them, and
this one will not invent values for them. It describes the *surrounding*
evidence it can actually see: headings, KPI text, tables, legends.

The hostile fixture includes a `<canvas role="img" aria-label="...">` precisely
to show the difference: a labelled visual **is** describable, an unlabelled one
is not. Adding a vision model was considered and rejected — no local VLM fits the
latency budget on this hardware.

## Generality: no promise of working on every site

The agent is built on generic primitives (accessibility tree, roles, accessible
names, ARIA refs) and contains no site-specific selectors, routes or click
scripts. That said, it will do better on some sites than others:

* **Good**: semantic HTML, ARIA roles, labelled controls, real `href`s, standard
  component libraries (the target's shadcn/Radix stack is close to ideal).
* **Poor**: canvas- or WebGL-rendered UIs, `<div>`-only interfaces with no roles,
  unlabelled icon buttons, aggressive virtualisation, heavy anti-automation.
* **Blocked**: sites requiring credentials the operator does not supply, and
  anything that detects and refuses automated browsers.

## Safety is layered, not a boundary

The guardrails are real and tested (closed action vocabulary, no JavaScript
execution path, snapshot-scoped refs, sensitive-action confirmation, credential
fields blocked outright, origin-restricted navigation, dialog/download/popup
interception). They are **defence in depth, not a security boundary.** In
particular:

* Origin restriction covers **navigation**. Subresource filtering exists
  (`strict_subresources`) but is off by default because blanket blocking breaks
  real applications, including the demo target.
* The overlay lives in the page's main world. A determined hostile page could
  find and call it. This is why nothing the agent decides depends on the overlay,
  and why the authoritative record is the JSONL run log.
* The sensitivity classifier reads accessible names, so a control that is
  destructive but labelled innocuously would not be caught by it. The
  confirmation gate on all page-mutating actions is the backstop.

Measured and relevant: a 1.5B model **complied with a prompt-injection framing**
in testing. That is why injection defence is structural rather than a prompt
instruction — the model is not trusted to resist it.

## Local model quality is the binding constraint

A model small enough to be interactive on this CPU is not a strong reasoner.
Observed failure modes on 1.5B: collapsing to a single action type, occasional
empty responses, weak instruction following. Mitigations are in the design
(snapshot-derived `ref` enums, ordered action enums, verb priors from the user's
own words, few-shot prefixes, deterministic validation of every model output).
The eval harness measures what actually happens rather than assuming.

## Not implemented

* Cloud STT/LLM/TTS providers. Interfaces are swappable, but no paid integration
  is shipped; the brief asked for none, and a stub nobody runs is a liability.
* Multi-tab demos. Popups are closed rather than adopted.
* Cross-origin iframes. Same-origin frames are stitched in by Playwright; the
  same-origin policy makes cross-origin frame content unreadable to anyone.
* Persisting a learned site map between runs.
