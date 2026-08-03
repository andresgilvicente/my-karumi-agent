# karumi-agent

A voice agent that gives **live demos of web products whose source code it does not have**.

It drives a real browser showing the real site, reads that page through its accessibility
tree, explains what is on screen, answers follow-up questions, and performs safe interactions
— verifying every action against the page before claiming it happened.

There are no hardcoded selectors, no scripted tours, no clone of the target frontend, and no
modifications to the site being demoed.

> **Status: Phase 1 (browser spike).** The observation and actuation layer is being built and
> validated first, because it carries the project's technical risk. Voice arrives in Phase 3.
> See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the design and
> [`docs/LIMITATIONS.md`](docs/LIMITATIONS.md) for what is and is not proven today.

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
# Phase 1 — browser spike, no voice. Opens a visible Chromium and drives it.
uv run karumi spike --url https://metric-master-suite505.apps.rebolt.ai/

# Against the bundled local fixture (no network needed):
uv run karumi serve-fixture &            # http://127.0.0.1:8899
uv run karumi spike --url http://127.0.0.1:8899
```

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
