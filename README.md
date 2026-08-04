# karumi-agent

A voice agent that gives **live demos of web products whose source code it does not have**.

It drives a real browser showing the real site, reads that page through its accessibility
tree, explains what is on screen, answers follow-up questions, and performs safe interactions
— verifying every action against the page before claiming it happened.

There are no hardcoded selectors, no scripted tours, no clone of the target frontend, and no
modifications to the site being demoed.

> **Status: Phase 2 (text-driven UI worker).** The browser layer is built and validated;
> an LLM now reads the page and acts on it through typed tools, driven by text. Voice
> arrives in Phase 3. See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the design and
> [`docs/LIMITATIONS.md`](docs/LIMITATIONS.md) for what is and is not proven today —
> notably, the agent has not yet been run against a real LLM or the real target site.

---

## How it works

```
Chromium (headed, real site)  ──snapshot──▶  BrowserWorker  ──▶  UIWorker (LLM)
        ▲                                         │                    │
        └──────────── verified action ────────────┘              VoiceWorker
```

The page is observed through Pipecat's own accessibility walker, injected into the target
site via Playwright's `addInitScript`. That yields the exact `A11ySnapshot` payload Pipecat's
`UIWorker` expects — with element refs that stay stable across snapshots — so the official UI
Worker protocol is reused end to end, with the browser standing in for the usual React client.

Actions are executed by Playwright as real, trusted input, then **verified** by diffing the
page state before and after. The agent does not say "done" until the page agrees.

---

## Requirements

- Python **3.11+** and [`uv`](https://docs.astral.sh/uv/)
- Node **20+** (only to build the injected page bundle)
- Chromium via Playwright

## Setup

```bash
uv sync                                   # Python deps (browser layer only)
uv run playwright install chromium        # skip if PLAYWRIGHT_BROWSERS_PATH is preprovisioned

npm --prefix src/karumi_agent/browser/injected install
npm --prefix src/karumi_agent/browser/injected run build   # -> dist/karumi-page.js

cp .env.example .env                      # fill in only what the phase you're running needs
```

The voice stack is a separate extra so the browser layer stays installable on its own:

```bash
uv sync --extra voice
```

## Running

```bash
# Phase 1 — browser spike, no LLM, no voice. Opens a visible Chromium and drives it.
uv run karumi spike --url https://metric-master-suite505.apps.rebolt.ai/

# Against the bundled local fixture (no network needed):
uv run karumi serve-fixture &            # http://127.0.0.1:8899
uv run karumi spike --url http://127.0.0.1:8899
```

```bash
# Phase 2 — an LLM reads the page and acts on it. Needs ANTHROPIC_API_KEY.
uv run karumi text \
  --url https://metric-master-suite505.apps.rebolt.ai/ \
  --product metric_master_suite

# One-shot instead of a prompt loop:
uv run karumi text --url http://127.0.0.1:8899 --ask "What is this page about?"
```

`--product` names an editorial briefing under `config/products/` — demo goals, value
messages and claims to avoid. It contains no selectors and no fixed route; the agent works
out how to honour it from whatever is on screen. Omit it to run against an unknown URL.

Add `--headless` for CI. Timings for every observation, action and verification are written
as JSONL to `logs/`.

## Configuration

All configuration is environment-driven; see [`.env.example`](.env.example) for the full list
with defaults. Nothing in the repo contains secrets.

## Tests

```bash
uv run pytest tests/unit           # pure logic: schemas, diffing, policy, token budget
uv run pytest tests/integration    # real Chromium against the local fixture
uv run ruff check . && uv run mypy
```

## Documentation

| Document | Contents |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Problem, research findings, alternatives, chosen design, trade-offs, latency budget |
| [`docs/LIMITATIONS.md`](docs/LIMITATIONS.md) | Known limits, unproven assumptions, measured numbers that miss their target |
