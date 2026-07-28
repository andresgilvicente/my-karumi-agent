# Autonomous Guided Demo

Karumi Agent owns a deterministic, session-local tour of Dashboard, Analytics, and Team. Qwen still
answers normal questions, but it does not control the tour state or decide whether a UI action was
successful.

## Start And Timing

After React publishes a compatible `ui_state`, the agent introduces itself and offers a tour. The
timer starts only after the introduction finishes playing.

- No user response for 5 seconds: the agent announces that it will start with Dashboard.
- No user response for 4 seconds after a confirmed step explanation: the agent announces and opens
  the next step.
- No transcription for 5 seconds after VAD stops: the consumed voice turn is closed and the pending
  demo position is recovered.

These values and every spoken transition live in `ui_control/guided_demo.json`.

## Sequence

1. Dashboard navigation and Total Revenue highlight are both confirmed before the explanation.
2. Analytics navigation and Traffic Overview highlight are both confirmed before the explanation.
3. Team navigation and member-list scroll are both confirmed before the explanation.
4. The agent closes the tour and invites questions.

The sequence uses the existing `UIActionExecutor`; it cannot bypass the shared UI catalog,
`commandId` matching, five-second action timeout, or React acknowledgement.

## Controls

The closed deterministic resolver accepts the exact phrase variants declared in
`ui_control/guided_demo.json`:

- Start: `Start the demo`, `Give me a tour`, `Show me the product`, `Begin the walkthrough`.
- Continue: `Continue`, `Go on`, `Next`, `Show me the next section`.
- Pause: `Pause the demo`, `Stop for a moment`, `Hold on`.
- Resume: `Resume the demo`, `Continue the tour`.
- Stop: `Stop the demo`, `End the tour`, `Cancel the demo`, `No thanks`, `Not now`.
- Restart: `Restart the demo`, `Start the tour again`.

Normal questions pass to Qwen. Speaking cancels the pending timer immediately. Once the answer has
finished playing, the controller opens another four-second intervention window and then continues
from the next unvisited step. A confirmed step is never repeated solely because its explanation was
interrupted.

An explicit standalone navigation during the initial offer opts out of proactive tour advancement.
During an active tour, confirmed user navigation synchronizes the next step. Semantic actions are
serialized so a user navigation cannot race an in-flight guided navigation.

## Validation

Run the automated checks:

```bash
uv run python -m unittest discover -s tests
uv run python -m py_compile bot.py
```

Then connect the React app in Chrome and verify:

1. Remain silent after the introduction and confirm the announced automatic Dashboard start.
2. Remain silent through Dashboard, Analytics, Team, and the completion message.
3. Restart and ask a question while Dashboard is being explained; confirm Analytics remains next.
4. Exercise `Pause`, `Resume`, `Continue`, `Stop`, and `Restart`.
5. Navigate directly to Analytics during an active tour and confirm Team becomes next.
6. Disconnect while waiting and confirm no later action or speech occurs.

Every successful step must show matching navigation and default-focus confirmations in the backend
log before its explanation is spoken.
