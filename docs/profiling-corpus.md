# Voice Demo Profiling Corpus

Use this fixed corpus to compare the current voice pipeline before and after a change. Profile files
are local-only and ignored by Git.

## Run

Start a fresh backend process with profiling enabled:

```bash
KARUMI_PROFILE=1 uv run bot.py -t webrtc
```

The default output is `profiles/karumi-profile-<utc>-<pid>.jsonl`. Set
`KARUMI_PROFILE_PATH=/tmp/karumi.jsonl` to choose a file or `KARUMI_PROFILE_CASE=C01` when running
one isolated case. Profiling records local transcriptions so recognition errors and split phrases can
be reviewed; no profile data is sent over the network.

## Corpus

| ID | Kind | Phrase | Expected behavior |
|---|---|---|---|
| C01 | Conversation, short | What can you show me? | Short spoken answer, no UI action |
| C02 | Conversation, medium | What does this product do? | Product explanation, no UI action |
| C03 | Conversation, medium | What is Metric Master Suite? | Product explanation, no UI action |
| C04 | Conversation, longer | How could Metric Master Suite help a manager understand business performance? | Longer concise answer, no UI action |
| V01 | Vocabulary | Say the names Karumi, Pipecat, and Metric Master Suite. | All three product terms remain recognizable |
| A01 | Deterministic | Take me to Dashboard. | Dashboard plus Total Revenue confirmation |
| A02 | Deterministic | Open Analytics. | Analytics plus Traffic Overview confirmation |
| A03 | Deterministic | Show me the Team section. | Team plus member-list confirmation |
| A04 | Deterministic | Highlight Revenue. | Revenue highlight only when Dashboard is visible |
| A05 | Deterministic | Highlight Traffic Overview. | Traffic highlight only when Analytics is visible |
| A06 | Deterministic | Scroll to the Team members. | Member-list scroll only when Team is visible |
| S01a | Split command | Can you take me? | No action yet |
| S01b | Split command | to the analytics section. | Analytics plus Traffic Overview confirmation |
| N01 | Control | What does the Analytics section show? | Conversational answer, no navigation |
| N02 | Control | Maybe show me something useful later. | No UI action |
| N03 | Unsupported | Open the Goals tab. | No UI action and no claimed success |

Run A04 after A01, A05 after A02, and A06 after A03 so each direct target is visible.

## Protocol

1. Keep the computer, Chrome version, microphone, room, speaking distance, models, and model settings
   unchanged.
2. Start a new backend process and browser connection for one cold pass in the table order.
3. Repeat each user case five times in a warmed process. Keep the same order for every comparison.
4. Use the React application for UI cases because Pipecat's standalone client has no UI handlers.
5. For each case, mark transcription correctness, unexpected turn splitting, expected UI result, and
   action success. Use the ordered `turn_index` and recorded transcript to match cases.
6. Compare startup, first-user-turn, and later-user-turn separately. Treat these as operational labels,
   not proof of model cache state.
7. Report the median and highest observed usable value for speech-end-to-first-audio, STT, LLM, TTS
   TTFA, TTS total, and UI acknowledgement. Report failures separately rather than hiding them in a
   latency average.
8. Compare conversational turns against deterministic UI turns. Change only one runtime variable in
   any later experiment.

The first baseline establishes evidence only. Do not change Whisper, the speech timeout, Qwen, or
Kokoro while collecting it.
