# Presenter Demo Script

This runbook is for a live Metric Master Suite presentation. It is not an acceptance record. The
current final Chrome rehearsal is **PENDING**. Complete every required item in
[`FINAL_CHECKLIST.md`](FINAL_CHECKLIST.md) and record a final `PASS` before recording.

## Preflight

1. Start Ollama and confirm that the configured `qwen2.5:1.5b` model is available.
2. From the repository root, start the voice worker with `uv run bot.py -t webrtc`.
3. From `client/`, select the repository Node version with `nvm use` and start Vite with
   `npm run dev`.
4. Use the URLs printed by the running services. The defaults are the React app at
   `http://localhost:5173` and the Pipecat runner at `http://localhost:7860`.
5. Open the React app in current Chrome at `#home`, grant microphone access, and leave the browser
   zoom at a readable level. Close or silence applications that may use the microphone or play
   notifications.
6. Keep the backend log visible to the operator. A UI result is successful only after the matching
   command confirmation appears; do not narrate visual success before confirmation.
7. Speak only the quoted user phrases while the microphone is live. Wait for the agent to finish
   speaking before the next phrase unless the script explicitly calls for an interruption.
8. Before recording, complete one warm-up connection. An uncached Whisper or Kokoro download can
   exceed the frontend's 20-second readiness window; let loading finish, disconnect, and begin the
   recorded run with a fresh connection.

## Main Flow: 3-5 Minutes

The timing is approximate and depends on local STT, LLM, and TTS latency.

1. **Connect and allow the proactive offer.** Click `Connect voice`. The agent introduces itself
   after React publishes compatible UI state. When the introduction has completely finished, remain
   silent for five seconds. The agent announces that it will start with Dashboard, opens Dashboard,
   centers and highlights Total Revenue, and then explains the view.
2. **Pause an active tour.** As soon as the Dashboard explanation finishes, and before the
   four-second continuation window expires, say: `Pause the demo`. Confirm that the pause response is
   spoken and no automatic Analytics transition follows.
3. **Use a direct action while paused.** Say: `Show me Traffic Overview`. Analytics should open and
   Traffic Overview should be centered and highlighted. This confirmed external action synchronizes
   the tour to Analytics but does not remove the explicit pause.
4. **Ask a normal product question.** Say: `What does this product do`. The agent should answer
   conversationally without moving the interface. The tour remains paused.
5. **Resume from synchronized state.** Say: `Resume the demo`. Because Analytics was already
   confirmed, the guided sequence should continue with Team rather than repeat Analytics. Team opens,
   the member list is centered, and the Team explanation follows.
6. **Show confirmed Team data.** Before the next four-second continuation window expires, say:
   `Search for Olivia`. The Team list should show and highlight Olivia Chen, and the spoken result
   should use the confirmed match. Then say: `Clear the search`. The full member list should return
   and be centered.
7. **Complete explicitly.** Say: `Continue`. The agent should speak the completion message without
   repeating Team.
8. **Demonstrate restart and stop.** Say: `Restart the demo`. Confirm that the sequence resets to
   Dashboard. After the Dashboard explanation, say: `Stop the demo`. Confirm that no later tour step
   runs; normal product questions remain available.

## Short Flow: About 90 Seconds

Use this cut when the presentation slot is constrained. Actual duration still depends on the local
voice pipeline.

1. Click `Connect voice`, let the introduction finish, and remain silent for five seconds.
2. Let the automatic Dashboard action and explanation complete.
3. Before four seconds elapse, say: `Pause the demo`.
4. Say: `Show me Traffic Overview` and wait for the confirmed Analytics result.
5. Say: `Resume the demo` and let the controller advance to Team.
6. Say: `Search for Olivia`, then say: `Clear the search`.
7. Say: `Continue` to close the tour.

## Exact Supported Controls

Use these exact phrases in a presentation:

| Phrase | Expected effect |
|---|---|
| `Pause the demo` | Pause an offered or active tour and cancel pending advancement. |
| `Resume the demo` | Continue an explicitly paused tour from its synchronized position. |
| `Continue` | Advance immediately from an offered, active, waiting, answering, or paused tour state. |
| `Stop the demo` | Cancel the tour and its pending timer or action. |
| `Restart the demo` | Reset progress and begin again at Dashboard. |

## Exact Resolver-Verified Phrases

These phrases are in the deterministic acceptance surface. Use them exactly when showing direct UI
control.

| Phrase | Expected deterministic behavior |
|---|---|
| `Go to Dashboard` | Open Dashboard, center Total Revenue, and highlight it. |
| `Can you please open Analytics` | Open Analytics, center Traffic Overview, and highlight it. |
| `Take me back to Team` | Open Team and center the member list. |
| `Could you navigate to Dashboard` | Open Dashboard, center Total Revenue, and highlight it. |
| `Show me the Team section` | Open Team and center the member list. |
| `Show me Total Revenue` | Open Dashboard if needed, then center and highlight Total Revenue. |
| `Focus on Active Users` | Open Dashboard if needed, then center and highlight Active Users. |
| `Highlight Performance Overview` | Open Dashboard if needed, then center and highlight Performance Overview. |
| `Show me Traffic Overview` | Open Analytics if needed, then center and highlight Traffic Overview. |
| `Focus on Traffic Sources` | Open Analytics if needed, then center and highlight Traffic Sources. |
| `Take me to the team member list` | Open Team if needed, then center and highlight the member list. |
| `Is there anyone called Juan` | Open Team if needed and report zero confirmed matches. |
| `Search for Olivia` | Open Team if needed, filter to Olivia Chen, center the row, and highlight it. |
| `Find a team member called Alex` | Open Team if needed and report zero confirmed matches. |
| `Does someone named Michael work here` | Open Team if needed and report zero confirmed matches. |
| `Search for Li` | Open Team if needed and report the confirmed name-prefix matches. |
| `Clear the search` | Open Team if needed, clear the filter, and center the full member list. |
| `What does this product do` | Give a normal conversational answer with no UI action. |

## Recovery

| Situation | Presenter recovery |
|---|---|
| The introduction does not start | Confirm the voice status reaches `connected`, verify the runner and Vite URLs, then disconnect, wait for the control to settle, and reconnect. |
| A phrase is transcribed incorrectly | Wait for the current response to finish and repeat the exact phrase once, clearly and without added words. |
| A direct action is issued during the initial offer | That action intentionally cancels proactive advancement. Say `Restart the demo` to begin the tour. |
| The tour advances before the next talking point | Say `Pause the demo` during the next active or waiting window, then continue from the synchronized view. |
| A direct action is used while paused | The tour remains paused. Say `Resume the demo` when ready; it should continue after the confirmed view. |
| The interface does not confirm an action | Let the agent report the failure. Do not claim success. Check the backend for an error or five-second timeout, restore the connection, and say `Restart the demo`. |
| The wrong view is open during the tour | Use one exact direct navigation phrase, wait for confirmation, then say `Resume the demo` or `Continue` as appropriate. |
| The agent is interrupted by unintended room speech | Stop speaking and let the turn settle. If the tour position is unclear, say `Pause the demo`, then use `Restart the demo` for a clean reset. |
| The connection drops | Wait until the control returns to `disconnected`, reconnect, and treat the replacement connection as a new session with a fresh offer. |
