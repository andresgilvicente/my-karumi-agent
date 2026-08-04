# Karumi Agent

A voice agent that gives **live demos of websites it does not control**, running
entirely on your own machine.

You point it at a URL. It opens a real Chromium window, reads the page through
its accessibility tree, and talks about it — answering questions, navigating,
scrolling, highlighting what you ask about, and running a self-guided tour. You
can interrupt it mid-sentence.

**No API keys. No accounts. No cloud services. No paid anything.** Speech
recognition, reasoning, speech synthesis and browser control all run locally
after a one-time open-source model download.

---

## What it actually does

* **Observes the real site.** No clone, no mock, no scripted click path. The page
  is read as a distilled accessibility tree with stable element references, so
  the agent works on a site whose source it has never seen and keeps working when
  that site is redesigned.
* **Never sees the DOM, never runs JavaScript.** The model gets roles, accessible
  names and states — a fraction of the tokens, and nothing executable.
* **Verifies what it does.** Every action re-observes the page and checks the
  intended effect actually happened. If it cannot confirm it, it says so out loud
  rather than claiming success.
* **Refuses what it should.** Destructive-looking controls need a spoken
  confirmation. Password fields are never typed into. Navigation is restricted to
  the site being demonstrated.
* **Learns the site while greeting you.** Site exploration starts before the
  agent has finished saying hello, so it knows something by the time you ask.

---

## Requirements

| | |
|---|---|
| OS | Linux, or Windows with WSL2 (developed and tested on WSL2 Ubuntu 24.04) |
| Python | 3.12+ (`uv` will fetch one) |
| RAM | 8 GB usable minimum; 16 GB comfortable |
| Disk | ~4 GB for models and the browser |
| GPU | **Not required.** Used if present, but everything runs on CPU |

Developed and measured on an **AMD Ryzen 7 5700U (8C/16T), 18 GB RAM, no GPU**.
All numbers in this repo come from that machine, on CPU.

---

## Install

```bash
# 1. System packages (the only step that needs sudo)
sudo apt-get update
sudo apt-get install -y espeak-ng libnss3 libnspr4

# 2. Python dependencies
uv sync --extra parakeet

# 3. The browser
uv run playwright install chromium

# 4. Ollama, for the local language model
curl -fsSL https://ollama.com/install.sh | sh
ollama serve &          # if it is not already running
ollama pull qwen3:1.7b  # 1.4 GB

# 5. Check everything
uv run karumi doctor
```

`karumi doctor` reports what is present, what is missing, and the exact command
to fix each gap. Get it to green before going further.

<details>
<summary>Why those apt packages?</summary>

* `espeak-ng` — Kokoro (text-to-speech) uses it to turn text into phonemes. It is
  a system package, not a Python one.
* `libnss3`, `libnspr4` — Playwright's Chromium links against them and they are
  absent from a clean Ubuntu 24.04 WSL image. Without them the browser fails to
  start with `libnspr4.so: cannot open shared object file`. `uv run playwright
  install-deps chromium` does the same job if you prefer.
</details>

### Downloads, and what they cost

Nothing large is fetched without you asking for it.

| What | When | Size |
|---|---|---|
| Chromium | `playwright install chromium` | ~170 MB download |
| `qwen3:1.7b` | `ollama pull` | 1.4 GB |
| Parakeet speech recognition | first voice run | ~670 MB |
| Kokoro speech synthesis | first voice run | ~337 MB |
| Silero VAD + Smart Turn v3 | already inside the Python wheel | 11 MB |

**Total ≈ 2.6 GB.**

---

## Run

### Voice demo

```bash
uv run karumi demo
```

Two windows open:

* **Chromium** — the real site. *This* is the window to screen-share.
* **Operator console** at `http://127.0.0.1:7860` — click **Start talking** and
  allow the microphone. It also shows captions and live latency, and takes typed
  input if you have no microphone.

Then just talk: *"What does this page show?"*, *"Show me the conversion rate"*,
*"Take me to the analytics section"*, *"Give me a tour"*.

The header pill tells you where you are: **microphone off** → **connecting…** →
**listening — just talk**. If anything fails it says so in the conversation
panel rather than sitting there quietly.

**Easiest first run** — no sign-in, no network, everything local:

```bash
uv run karumi demo --url "$(uv run karumi fixture --port 8123 &>/dev/null & sleep 1; echo http://127.0.0.1:8123)"
```

or, in two terminals:

```bash
uv run karumi fixture --port 8123     # terminal 1
uv run karumi demo --url http://127.0.0.1:8123   # terminal 2
```

```bash
uv run karumi demo --url https://www.karumi.ai   # any URL
uv run karumi demo --autonomous                  # start the tour immediately
```

### The Metric Master Suite demo

```bash
uv run karumi demo --url https://metric-master-suite505.apps.rebolt.ai/
```

That site **requires signing in** — that is the site's own login, not something
this project adds. The agent notices it generically, tells you, and **carries on
regardless**: you can still ask about what is on screen, run the tour, or point
it somewhere else. Sign in *in the Chromium window* whenever you like and it
picks up from there. It never types a credential itself, and the browser profile
is persistent, so you only do this once.

If you would rather not deal with a login at all, point the demo anywhere else:

```bash
uv run karumi demo --url https://www.karumi.ai
```

### Without audio

```bash
uv run karumi chat          # type instead of speaking; same loop
uv run karumi fixture       # serve the offline hostile test site
```

### Tests, benchmark, diagnostics

```bash
uv run karumi doctor                                  # what is installed
uv run karumi bench                                   # latency, P50/P90/P95
uv run pytest                                         # everything
uv run pytest -m "not requires_model"                 # skip model-dependent tests
uv run ruff check . && uv run mypy --strict src       # lint and types
```

Tests needing hardware or models are marked and **skip with a stated reason**
rather than passing quietly.

---

## Configuration

Everything has a working default. `.env.example` documents the knobs and
**contains no keys**, because none exist.

```bash
KARUMI_LLM__MODEL=qwen3:1.7b      # any Ollama model with tool/JSON support
KARUMI_STT__ENGINE=auto           # auto | parakeet | whisper
KARUMI_VOICE__VAD_START_SECS=0.12 # lower interrupts faster
KARUMI_BROWSER__HEADLESS=false    # the audience should see the browser
```

### Editorial configuration

`configs/metric-master-suite.yaml` can steer *what a demo is about* — goals,
themes, approved messaging, facts, claims to avoid.

It **cannot** contain selectors, element ids, coordinates, route paths or click
scripts, and this is enforced by the loader, not just documented: a config
containing `#export-button` is rejected at startup, with a test asserting it.
The agent finds everything by observing the live page.

---

## How it works

```
  microphone ──▶ operator console (WebRTC) ──▶ Pipecat pipeline
                                                    │
                          speech recognition ───────┤
                                                    ▼
                                            ┌───────────────┐
                                            │ DemoSession   │
                                            │  observe      │──▶ Playwright ──▶ Chromium
                                            │  plan (1 LLM) │      (real site the
                                            │  act          │       audience sees)
                                            │  verify       │
                                            └───────┬───────┘
                                                    ▼
                          speech synthesis ◀────────┘
```

Three decisions carry most of the design, each made from a measurement rather
than a preference:

**One model inference per turn.** The conventional arrangement — a voice model
that delegates to a separate UI model — costs two inferences. At the ~13 tokens/s
this CPU manages, that is four to five seconds per turn. The planner sees the
transcript and the page together, decides once, and speaks its own answer.

**The element reference enum is built from the live page.** The model replies in
a JSON schema whose `ref` field enumerates exactly the elements currently on
screen. Measured: with ordinary tool calling a small model put accessible names
in the reference slot about half the time; with the enum, invalid references
became impossible to express — 6 out of 6 valid.

**The spoken sentence is generated first.** Schema property order drives decoding
order, so putting `say` first means speech synthesis starts on the opening words
instead of waiting for the whole decision. Time to first speech: ~1 second rather
than ~3.

Fuller detail in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

## Performance, honestly

The aspiration was under a second from end of your turn to first audio. **On a
CPU-only machine that is not reachable**, and this project does not pretend
otherwise.

Measured on the machine above, against the local fixture, `qwen3:1.7b`:

| | P50 | P90 | P95 |
|---|---|---|---|
| First token from the model | 0.89 s | 3.21 s | 8.03 s |
| First speech out | 1.28 s | 3.56 s | 8.39 s |
| Whole turn | 3.53 s | 10.95 s | 11.55 s |

The spread is real and worth understanding: a turn whose prompt prefix is already
cached costs about a second, and one following a page change costs seven, because
cold prefill on this CPU runs at ~154 tokens/s against ~10,700 warm. That 70x gap
is the single biggest lever on this hardware, and the design leans on it hard
(stable prompt prefixes, background prefill after navigation, short replies).

Reproduce with `uv run karumi bench`. Full numbers in
[`docs/BENCHMARKS.md`](docs/BENCHMARKS.md).

---

## Limitations

Read [`docs/LIMITATIONS.md`](docs/LIMITATIONS.md) — it is written plainly and
covers what does not work as well as what does. The short version:

* Sub-second response is not achievable on CPU.
* The CUDA path is implemented but **has never been run on a real GPU**.
* Charts drawn to canvas without accessible labels cannot be described, and the
  agent will say so rather than invent numbers.
* A model small enough to be interactive on CPU is not a strong reasoner; the
  guardrails exist because the model cannot be trusted, not because it can.
* The safety measures are layered defence, not a security boundary.

---

## If something does not work

**"Start talking" does nothing / the pill stays on "microphone off".**
The console now reports the reason in the conversation panel — read that first.
Common causes:

* *You opened the console on a LAN IP.* `getUserMedia` only exists in a secure
  context. `http://127.0.0.1:7860` counts; `http://192.168.x.x:7860` does not.
  Open it on localhost.
* *Speech models did not load.* The pill will read "voice unavailable — type
  below". Run `uv run karumi doctor`. Typing keeps working meanwhile.
* *The browser blocked audio playback.* Click once anywhere on the console page.

**Chromium will not start** — `libnspr4.so: cannot open shared object file`.
Install the system libraries: `sudo apt-get install -y libnss3 libnspr4`.

**The first question is slow (~7 s), later ones are fast (~1 s).**
Expected on CPU. Cold prompt prefill runs at ~154 tokens/s against ~10,700 warm.
The agent prefills a new page in the background after navigating, so this mostly
shows up on the very first question.

**Everything is slow.** Check nothing else is holding the model in memory:
`ollama ps`, then `ollama stop <model>` for anything unused.

**Reporting a problem.** Send `~/.cache/karumi-agent/logs/karumi.jsonl` and the
output of `uv run karumi doctor --json`.

## Documentation

| | |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | How it is put together and why |
| [`docs/BENCHMARKS.md`](docs/BENCHMARKS.md) | Measurements and the machine behind them |
| [`docs/LIMITATIONS.md`](docs/LIMITATIONS.md) | What does not work |
| [`docs/PROJECT_STATE.md`](docs/PROJECT_STATE.md) | Verified facts and phase status |
