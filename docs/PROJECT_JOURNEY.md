# Project Journey

## 1. Project overview

This project was created as a local voice agent capable of presenting and controlling the Metric Master Suite interface while having a live conversation with the user.

The goal was not simply to place a chatbot next to a dashboard. I wanted to build an agent that could guide the user through the product, move between sections, focus attention on relevant information, answer questions, and react naturally when interrupted.

The final result combines a React interface with a Pipecat voice pipeline. Speech recognition, language generation, and speech synthesis all run locally, without requiring external AI API keys.

The optional idea of learning about a company from a URL was intentionally left outside the MVP. I prioritised building a smaller but reliable and demonstrable vertical experience.

---

## 2. Scope of the MVP

The implemented MVP includes:

- A responsive React mock of Metric Master Suite.
- Local speech-to-text using Whisper.
- Local conversational responses using Qwen through Ollama.
- Local text-to-speech using Kokoro.
- Browser-to-backend communication through SmallWebRTC and RTVI.
- An autonomous guided tour through Dashboard, Analytics, and Team.
- Natural voice navigation between supported sections.
- Scrolling, centring, and highlighting of important panels.
- Voice-based Team member search.
- Demo controls such as pause, resume, continue, stop, and restart.
- Interruption handling during the guided tour.
- Browser confirmations before the agent reports that an action succeeded.
- Optional local latency profiling.

The project deliberately does not include:

- URL ingestion or automatic company learning.
- Generic browser or screen control.
- Persistence or a product database.
- Authentication or multi-user support.
- Production deployment.
- Full voice control of every visible element.
- UIWorker integration.
- Automated browser end-to-end testing.

This limited scope allowed me to focus on reliability, voice interaction, state management, and a polished demonstration flow.

---

## 3. Architecture

The application has two main parts.

### Frontend

The frontend is built with React and Vite. It renders the Metric Master Suite interface and contains the supported UI elements, including:

- Dashboard metrics.
- Analytics panels.
- Team member data and search.
- Voice connection controls.
- Visual scrolling and highlighting.

### Voice backend

The backend is built with Pipecat and runs the complete local voice pipeline:

```text
Browser microphone
→ SmallWebRTC
→ Whisper speech-to-text
→ deterministic intent resolver
→ guided demo controller or Qwen
→ Kokoro text-to-speech
→ browser audio
````

RTVI is used to exchange semantic state and commands between React and the backend.

The frontend publishes information about the current page and its supported capabilities. The backend sends semantic commands such as navigation, scrolling, highlighting, or Team search. React performs the action and sends a result back.

The agent only reports success after React confirms that the requested action was completed.

---

## 4. Main technical decisions

### Local models

I chose local models to keep the project self-contained and avoid external AI services.

The final model stack is:

| Component      | Model                            |
| -------------- | -------------------------------- |
| Speech-to-text | Whisper base                     |
| Language model | Qwen 2.5 1.5B through Ollama     |
| Text-to-speech | Kokoro with the `af_heart` voice |

This approach improves privacy and makes the demo independent from hosted APIs. The main trade-off is that latency depends heavily on the local machine, especially during the first interaction.

### Deterministic UI control

Initially, it was reasonable to consider allowing the language model to decide when to call UI tools. However, a small local model was not always reliable enough to guarantee that an action would actually be executed.

For this reason, explicit UI commands are handled by a deterministic resolver.

Qwen remains responsible for normal conversation and product questions, while actions such as navigation, focus, search, and tour controls follow predictable rules.

This separation provides a good balance:

* Natural conversation remains flexible.
* Important interface actions remain reliable and testable.

### Semantic targets instead of coordinates

The agent does not move the mouse or use screen coordinates.

It works with semantic identifiers such as:

* `total-revenue-card`
* `active-users-card`
* `traffic-overview`
* `traffic-sources`
* `team-member-list`

This makes the system safer and easier to extend. A new supported panel can be added to the shared catalog without teaching the model how to manipulate the DOM directly.

### Confirmation before speaking

Every UI action contains a unique command identifier.

React executes the action and returns a structured result. The backend verifies that the result matches:

* the active session;
* the original command;
* the expected view;
* the expected target or search query.

The agent does not say that something has been opened, highlighted, or found until that confirmation arrives.

This was one of the most important reliability decisions in the project.

---

## 5. Development challenges

### Voice and frontend were initially separate

The first version had a working local voice conversation and a working React mock, but they were not connected.

The next step was to create a semantic communication contract between them. React began publishing its current state, while the backend started sending confirmed UI commands.

### Unreliable tool usage

The local language model could sometimes describe an action without actually executing it.

The solution was to move explicit UI requests into a deterministic resolver and preserve Qwen for conversational responses.

### Turn completion

A voice agent must determine when the user has finished speaking without cutting them off during a natural pause.

The final approach combines Silero VAD with a speech timeout. A shorter timeout can make the agent respond faster, but it may split phrases. A longer timeout is safer but adds latency.

The selected configuration prioritises stability over the fastest possible response.

### Interruptions and autonomous progression

The guided tour must advance automatically, but it must also stop when the user speaks.

The controller therefore keeps explicit state about:

* the current step;
* the last confirmed screen;
* the next step;
* pending timers;
* whether the user is asking a question;
* whether the demo is paused or stopped.

Timers begin after audible playback finishes, not when speech is merely queued. This keeps the pacing closer to what the user actually experiences.

### Reconnection and stale state

Disconnecting and reconnecting introduced risks such as:

* old timers;
* pending UI commands;
* duplicated introductions;
* stale confirmations;
* previous conversation context.

The final design uses session identifiers and generations to reject results from previous sessions and clean pending work during disconnection.

### Search results

Team search could not rely on Qwen because the model might invent a person.

Instead, React filters the actual in-memory Team data and returns the confirmed names and match count. The spoken response is generated from that structured result.

---

## 6. Final product experience

The final demonstration works as a guided but interactive product tour.

A typical flow is:

1. The user opens the React application.
2. The user selects **Connect voice**.
3. The agent introduces itself and offers the tour.
4. The tour moves through Dashboard, Analytics, and Team.
5. Important panels are centred and highlighted.
6. The user can interrupt with a question.
7. The agent answers and preserves the tour state.
8. The user can request a specific panel.
9. The user can search for a Team member by voice.
10. The user can pause, resume, stop, continue, or restart the tour.

Examples of supported interactions include:

* “Go to Analytics.”
* “Show me Active Users.”
* “Focus on Traffic Sources.”
* “Is there anyone called Olivia?”
* “Pause the demo.”
* “Resume the demo.”
* “Restart the demo.”

The interface control surface remains intentionally restricted to known views and targets. Unsupported requests continue as normal conversation rather than becoming unsafe generic browser actions.

---

## 7. Reliability and performance

The project includes automated tests for:

* catalog validation;
* intent resolution;
* negative phrases that must not trigger actions;
* guided demo state transitions;
* interruptions;
* session cleanup;
* stale confirmations;
* UI action sequencing;
* Team search;
* search clearing;
* Markdown filtering before speech;
* profiling events.

The frontend build, Python compilation, imports, and unit tests are part of the final validation process.

The project also includes opt-in profiling for measuring stages such as:

* speech completion;
* transcription;
* intent resolution;
* LLM generation;
* TTS generation;
* first bot audio;
* UI acknowledgement.

The main performance limitations come from local inference and first-turn model loading. Later turns are generally faster once the models are warm.

For the final presentation, reliability is prioritised over aggressive latency optimisation.

---

## 8. Limitations and future work

The current product is a focused MVP rather than a production platform.

Its main limitations are:

* Static and representative product data.
* No persistence.
* No authentication.
* Local-only execution.
* A limited number of supported views and targets.
* Possible speech-recognition errors for product names.
* Variable latency on CPU.
* No browser end-to-end automation.
* No learning from a supplied URL.
* No production deployment.

With more time, I would explore:

### URL-based learning

The user could provide a company website. The system would extract basic product and company information and use it to adapt the introduction and tour.

### Structured product knowledge

Each interface section could contain structured information such as:

* purpose;
* main elements;
* customer value;
* facts that may be stated;
* claims that must be avoided.

The language model could then create natural narration without inventing facts.

### Personalised demonstrations

The tour could adapt according to:

* company;
* user role;
* interests;
* previous questions;
* available time.

### Improved latency

Potential improvements include:

* model prewarming;
* shorter responses;
* shorter tour pauses;
* better turn-completion tuning;
* cached or pre-generated fixed narration;
* model comparison using a measured baseline.

### Broader UI control

More views and product interactions could be added through the same semantic catalog and confirmation system.

### Production readiness

A production version would require:

* authentication;
* persistent data;
* multi-user session isolation;
* deployment;
* monitoring;
* security controls;
* browser integration tests.

---

## 9. Lessons learned

The main lesson from this project is that a voice agent is not only an LLM connected to a microphone.

It is a stateful system involving:

* audio input;
* turn detection;
* transcription;
* model generation;
* speech synthesis;
* browser state;
* timers;
* interruptions;
* session lifecycle.

Reliability depended on assigning each responsibility to the right component.

The language model works well for flexible conversation. Deterministic logic works better for actions that must be correct. React is the source of truth for what actually happened in the interface.

The most important engineering principles were:

* Reliability before feature breadth.
* Semantic actions instead of coordinates.
* Confirmation before reporting success.
* Explicit interruption and session state.
* Shared contracts instead of duplicated configuration.
* Measurement before optimisation.
* Honest documentation of limitations.

---

## 10. Final reflection

The final result is a compact but meaningful demonstration of a local voice agent capable of presenting and controlling a real interface.

It does not attempt to solve generic browser automation or dynamic product learning. Instead, it demonstrates a reliable vertical experience:

* live voice conversation;
* autonomous presentation;
* deterministic interface control;
* confirmed actions;
* interruption handling;
* semantic search;
* local AI models.

I believe this was the correct balance for the assignment. The project demonstrates creativity and technical depth while remaining small enough to understand, test, and extend.

The foundation could later support personalised tours, URL-based company learning, richer product knowledge, broader interface control, and production deployment without discarding the reliability principles established in this MVP.

