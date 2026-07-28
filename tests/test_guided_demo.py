import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from pipecat.frames.frames import (
    TranscriptionFrame,
    UninterruptibleFrame,
    VADUserStartedSpeakingFrame,
)
from ui_control.catalog import load_catalog
from ui_control.guided_demo import (
    GuidedDemoController,
    GuidedDemoState,
    load_guided_demo,
    resolve_demo_intent,
)
from ui_control.turns import ConsumedUserTurnFrame, ConsumedUserTurnStopStrategy
from pipecat.turns.types import ProcessFrameResult


class ControlledSleep:
    def __init__(self):
        self.waits = []

    async def __call__(self, delay):
        future = asyncio.get_running_loop().create_future()
        self.waits.append((delay, future))
        await future

    async def release_next(self):
        for _, future in self.waits:
            if not future.done():
                future.set_result(None)
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                return
        raise AssertionError("no pending timer")


class GuidedDemoTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.catalog = load_catalog()
        self.definition = load_guided_demo(self.catalog)
        self.actions = []
        self.speech = []
        self.action_result = {"status": "confirmed"}
        self.sleep = ControlledSleep()

        async def execute(command, payload):
            self.actions.append((command, payload))
            return self.action_result

        async def speak(text, user_text):
            self.speech.append((text, user_text))

        self.controller = GuidedDemoController(
            self.definition,
            execute,
            speak,
            sleep=self.sleep,
        )

    async def asyncTearDown(self):
        await self.controller.disconnect()

    async def connect_and_offer(self):
        await self.controller.connect("session-1")
        await self.controller.set_ui_ready(True)
        self.assertEqual(self.controller.state, GuidedDemoState.OFFERED)
        self.assertEqual(self.speech[-1][0], self.definition.introduction)

    async def finish_current_speech(self):
        await self.controller.on_bot_started_speaking()
        await self.controller.on_bot_stopped_speaking()
        await asyncio.sleep(0)

    async def start_explicit_demo(self):
        await self.connect_and_offer()
        await self.finish_current_speech()
        self.controller.on_user_started_speaking()
        handled = await self.controller.handle_control("start_demo", "Start the demo.")
        self.assertTrue(handled)
        self.assertEqual(self.speech[-1][0], "Let's begin with the Dashboard.")

    def test_definition_and_closed_intents(self):
        self.assertEqual(self.definition.offer_delay_secs, 5)
        self.assertEqual(self.definition.user_intervention_window_secs, 4)
        self.assertEqual(self.definition.transcript_grace_secs, 5)
        self.assertEqual([step.id for step in self.definition.steps], ["dashboard", "analytics", "team"])
        cases = {
            "Start the demo.": "start_demo",
            "Let's start the demo.": "start_demo",
            "Go on.": "continue_demo",
            "Hold on.": "pause_demo",
            "Continue the tour.": "resume_demo",
            "Cancel the demo.": "stop_demo",
            "Start the tour again.": "restart_demo",
            "Restart demo.": "restart_demo",
            "Tell me about the product.": None,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(resolve_demo_intent(text, self.definition), expected)

    def test_definition_rejects_an_unsupported_step(self):
        source = Path(__file__).resolve().parent.parent / "ui_control" / "guided_demo.json"
        data = json.loads(source.read_text())
        data["steps"][0]["action"]["payload"]["view"] = "goals"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.json"
            path.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, "supported views"):
                load_guided_demo(self.catalog, path)

    async def test_proactive_start_waits_for_intro_and_warning(self):
        await self.connect_and_offer()
        self.assertFalse(self.controller.pending_auto_advance)

        await self.finish_current_speech()
        self.assertTrue(self.controller.pending_auto_advance)
        self.assertEqual(self.sleep.waits[-1][0], 5)
        await self.sleep.release_next()

        self.assertEqual(self.actions, [])
        self.assertEqual(self.speech[-1][0], "I'll start with the main Dashboard.")
        await self.finish_current_speech()
        await self.controller.wait_for_action()

        self.assertEqual(
            self.actions,
            [("navigate_to", {"view": "dashboard"})],
        )
        self.assertEqual(self.controller.last_confirmed_step, "dashboard")
        self.assertEqual(self.speech[-1][0], self.definition.steps[0].explanation)

    async def test_action_is_confirmed_before_the_step_is_explained(self):
        await self.start_explicit_demo()
        self.assertEqual(self.actions, [])

        await self.finish_current_speech()
        await self.controller.wait_for_action()

        self.assertEqual(self.actions[0], ("navigate_to", {"view": "dashboard"}))
        self.assertEqual(self.controller.last_confirmed_step, "dashboard")
        self.assertEqual(self.controller.next_step_index, 1)
        self.assertEqual(self.speech[-1][0], self.definition.steps[0].explanation)

        for step_index in (1, 2):
            await self.finish_current_speech()
            await self.sleep.release_next()
            self.assertEqual(
                self.speech[-1][0],
                self.definition.steps[step_index].transition_message,
            )
            await self.finish_current_speech()
            await self.controller.wait_for_action()
            self.assertEqual(
                self.actions[-1],
                ("navigate_to", {"view": self.definition.steps[step_index].id}),
            )
            self.assertEqual(
                self.controller.last_confirmed_step,
                self.definition.steps[step_index].id,
            )

    async def test_failed_action_never_speaks_the_explanation_or_advances(self):
        self.action_result = {"status": "timeout"}
        await self.start_explicit_demo()
        await self.finish_current_speech()
        await self.controller.wait_for_action()

        self.assertEqual(self.controller.state, GuidedDemoState.FAILED)
        self.assertIsNone(self.controller.last_confirmed_step)
        self.assertEqual(self.speech[-1][0], self.definition.failure_message)
        self.assertNotIn(self.definition.steps[0].explanation, [text for text, _ in self.speech])
        self.assertFalse(self.controller.pending_auto_advance)

    async def test_silence_after_explanation_advances_to_analytics(self):
        await self.start_explicit_demo()
        await self.finish_current_speech()
        await self.controller.wait_for_action()
        await self.finish_current_speech()

        self.assertEqual(self.controller.state, GuidedDemoState.WAITING_FOR_USER)
        self.assertEqual(self.sleep.waits[-1][0], 4)
        await self.sleep.release_next()

        self.assertEqual(self.controller.current_step_index, 1)
        self.assertEqual(self.speech[-1][0], self.definition.steps[1].transition_message)

    async def test_full_silent_tour_completes_each_step_once(self):
        await self.start_explicit_demo()
        for step_index, step in enumerate(self.definition.steps):
            await self.finish_current_speech()
            await self.controller.wait_for_action()
            self.assertEqual(self.controller.last_confirmed_step, step.id)
            self.assertEqual(self.speech[-1][0], step.explanation)
            await self.finish_current_speech()
            await self.sleep.release_next()
            if step_index + 1 < len(self.definition.steps):
                self.assertEqual(
                    self.speech[-1][0],
                    self.definition.steps[step_index + 1].transition_message,
                )

        self.assertEqual(self.speech[-1][0], self.definition.completion_message)
        await self.finish_current_speech()
        self.assertEqual(self.controller.state, GuidedDemoState.COMPLETED)
        self.assertEqual(
            self.actions,
            [
                ("navigate_to", {"view": "dashboard"}),
                ("navigate_to", {"view": "analytics"}),
                ("navigate_to", {"view": "team"}),
            ],
        )

    async def test_question_during_explanation_keeps_analytics_as_next_step(self):
        await self.start_explicit_demo()
        await self.finish_current_speech()
        await self.controller.wait_for_action()

        self.controller.on_user_started_speaking()
        self.controller.on_normal_transcription()
        self.assertEqual(self.controller.next_step_index, 1)
        await self.controller.on_bot_started_speaking()
        await self.controller.on_bot_stopped_speaking()
        await asyncio.sleep(0)

        self.assertEqual(self.controller.state, GuidedDemoState.WAITING_FOR_USER)
        await self.sleep.release_next()
        self.assertEqual(self.controller.current_step_index, 1)
        self.assertEqual(self.controller.last_confirmed_step, "dashboard")

    async def test_answer_finishing_before_an_action_still_schedules_the_next_step(self):
        action_started = asyncio.Event()
        release_action = asyncio.Event()

        async def blocking_action(command, payload):
            self.actions.append((command, payload))
            action_started.set()
            await release_action.wait()
            return {"status": "confirmed"}

        self.controller._execute_action = blocking_action
        await self.start_explicit_demo()
        await self.finish_current_speech()
        await action_started.wait()

        self.controller.on_user_started_speaking()
        self.controller.on_normal_transcription()
        await self.controller.on_bot_started_speaking()
        await self.controller.on_bot_stopped_speaking()
        self.assertFalse(self.controller.pending_auto_advance)

        release_action.set()
        await self.controller.wait_for_action()
        await asyncio.sleep(0)
        self.assertEqual(self.controller.state, GuidedDemoState.WAITING_FOR_USER)
        self.assertTrue(self.controller.pending_auto_advance)
        self.assertEqual(self.controller.next_step_index, 1)

    async def test_pause_resume_and_stop_cancel_pending_advances(self):
        await self.start_explicit_demo()
        await self.finish_current_speech()
        await self.controller.wait_for_action()
        await self.finish_current_speech()
        self.assertTrue(self.controller.pending_auto_advance)

        self.controller.on_user_started_speaking()
        self.assertTrue(
            await self.controller.handle_control("pause_demo", "Pause the demo.")
        )
        self.assertEqual(self.controller.state, GuidedDemoState.PAUSED)
        self.assertFalse(self.controller.pending_auto_advance)
        await self.finish_current_speech()

        self.controller.on_user_started_speaking()
        self.assertTrue(
            await self.controller.handle_control("resume_demo", "Resume the demo.")
        )
        self.assertEqual(self.controller.current_step_index, 1)
        self.assertEqual(self.speech[-1][0], self.definition.steps[1].transition_message)

        self.controller.on_user_started_speaking()
        self.assertTrue(await self.controller.handle_control("stop_demo", "Stop the demo."))
        self.assertEqual(self.controller.state, GuidedDemoState.CANCELLED)
        self.assertFalse(self.controller.pending_auto_advance)

    async def test_restart_from_team_resets_and_executes_dashboard(self):
        await self.connect_and_offer()
        self.controller.state = GuidedDemoState.WAITING_FOR_USER
        self.controller.current_step_index = 2
        self.controller.last_confirmed_step = "team"
        self.controller.next_step_index = 3

        self.controller.on_user_started_speaking()
        self.assertTrue(
            await self.controller.handle_control("restart_demo", "Restart demo.")
        )
        self.assertEqual(self.controller.current_step_index, 0)
        self.assertIsNone(self.controller.last_confirmed_step)
        self.assertEqual(self.controller.next_step_index, 0)
        self.assertEqual(self.speech[-1][0], self.definition.steps[0].transition_message)

        await self.finish_current_speech()
        await self.controller.wait_for_action()

        self.assertEqual(self.actions[-1], ("navigate_to", {"view": "dashboard"}))
        self.assertEqual(self.controller.last_confirmed_step, "dashboard")
        self.assertEqual(self.controller.next_step_index, 1)
        self.assertEqual(self.speech[-1][0], self.definition.steps[0].explanation)

    async def test_external_navigation_synchronizes_the_next_step(self):
        await self.start_explicit_demo()
        await self.controller.on_external_action(
            "navigate_to",
            {"view": "analytics"},
            {"status": "confirmed"},
        )
        self.assertEqual(self.controller.last_confirmed_step, "analytics")
        self.assertEqual(self.controller.next_step_index, 2)

        self.controller.on_user_started_speaking()
        await self.controller.handle_control("continue_demo", "Continue.")
        self.assertEqual(self.controller.current_step_index, 2)
        self.assertEqual(self.speech[-1][0], self.definition.steps[2].transition_message)

    async def test_external_focus_and_search_synchronize_the_tour_view(self):
        await self.start_explicit_demo()
        await self.controller.on_external_action(
            "focus_target",
            {"target": "traffic-sources"},
            {"status": "confirmed", "view": "analytics"},
        )
        self.assertEqual(self.controller.last_confirmed_step, "analytics")
        self.assertEqual(self.controller.next_step_index, 2)

        await self.controller.on_external_action(
            "search_team_member",
            {"query": "Olivia"},
            {"status": "confirmed", "view": "team"},
        )
        self.assertEqual(self.controller.last_confirmed_step, "team")
        self.assertEqual(self.controller.next_step_index, 3)

    async def test_partial_external_navigation_retries_the_visible_step(self):
        await self.start_explicit_demo()
        await self.controller.on_external_action(
            "navigate_to",
            {"view": "analytics"},
            {
                "status": "error",
                "view": "analytics",
                "screenConfirmed": True,
            },
        )
        self.assertEqual(self.controller.current_step_index, 1)
        self.assertEqual(self.controller.next_step_index, 1)
        self.assertIsNone(self.controller.last_confirmed_step)

    async def test_external_navigation_does_not_resume_an_explicit_pause(self):
        await self.start_explicit_demo()
        self.controller.on_user_started_speaking()
        await self.controller.handle_control("pause_demo", "Pause the demo.")
        await self.controller.on_external_action(
            "navigate_to",
            {"view": "analytics"},
            {"status": "confirmed"},
        )
        self.assertEqual(self.controller.state, GuidedDemoState.PAUSED)
        self.assertEqual(self.controller.next_step_index, 2)

    async def test_external_navigation_during_offer_does_not_start_the_tour(self):
        await self.connect_and_offer()
        await self.controller.on_external_action(
            "navigate_to",
            {"view": "analytics"},
            {"status": "confirmed"},
        )
        self.assertEqual(self.controller.state, GuidedDemoState.CANCELLED)
        self.assertIsNone(self.controller.last_confirmed_step)
        self.assertFalse(self.controller.pending_auto_advance)

    async def test_continue_during_an_action_does_not_launch_a_second_action(self):
        action_started = asyncio.Event()
        release_action = asyncio.Event()

        async def blocking_action(command, payload):
            self.actions.append((command, payload))
            action_started.set()
            await release_action.wait()
            return {"status": "confirmed"}

        self.controller._execute_action = blocking_action
        await self.start_explicit_demo()
        await self.finish_current_speech()
        await action_started.wait()
        self.controller.on_user_started_speaking()
        self.assertTrue(
            await self.controller.handle_control("continue_demo", "Continue.")
        )
        release_action.set()
        await self.controller.wait_for_action()

        self.assertEqual(len(self.actions), 1)
        self.assertEqual(self.controller.last_confirmed_step, "dashboard")
        self.assertEqual(self.speech[-1][0], self.definition.steps[0].explanation)

    async def test_disconnect_prevents_a_pending_timer_from_advancing(self):
        await self.connect_and_offer()
        await self.finish_current_speech()
        self.assertTrue(self.controller.pending_auto_advance)
        await self.controller.disconnect()
        self.assertFalse(self.controller.pending_auto_advance)
        self.assertEqual(self.controller.state, GuidedDemoState.IDLE)

    async def test_missing_transcription_retries_an_interrupted_step(self):
        await self.start_explicit_demo()
        self.controller.on_user_started_speaking()
        await self.controller.on_transcription_timeout()
        self.assertEqual(self.controller.current_step_index, 0)
        self.assertEqual(self.controller.state, GuidedDemoState.RUNNING)
        self.assertEqual(self.speech[-1][0], self.definition.steps[0].transition_message)

    async def test_missing_transcription_replays_an_interrupted_explanation(self):
        await self.start_explicit_demo()
        await self.finish_current_speech()
        await self.controller.wait_for_action()
        self.assertEqual(self.speech[-1][0], self.definition.steps[0].explanation)

    async def test_missing_transcription_during_action_still_explains_confirmed_step(self):
        action_started = asyncio.Event()
        release_action = asyncio.Event()

        async def blocking_action(command, payload):
            action_started.set()
            await release_action.wait()
            return {"status": "confirmed"}

        self.controller._execute_action = blocking_action
        await self.start_explicit_demo()
        await self.finish_current_speech()
        await action_started.wait()
        self.controller.on_user_started_speaking()

        release_action.set()
        await self.controller.wait_for_action()
        self.assertEqual(self.controller.last_confirmed_step, "dashboard")
        await self.controller.on_transcription_timeout()
        self.assertEqual(self.speech[-1][0], self.definition.steps[0].explanation)
        self.assertEqual(self.controller.next_step_index, 1)

        self.controller.on_user_started_speaking()
        await self.controller.on_transcription_timeout()
        self.assertEqual(self.controller.last_confirmed_step, "dashboard")
        self.assertEqual(self.controller.next_step_index, 1)
        self.assertEqual(self.speech[-1][0], self.definition.steps[0].explanation)

    async def test_pause_wins_if_an_action_confirmation_arrives_late(self):
        action_started = asyncio.Event()
        release_action = asyncio.Event()

        async def blocking_action(command, payload):
            action_started.set()
            await release_action.wait()
            return {"status": "confirmed"}

        self.controller._execute_action = blocking_action
        await self.start_explicit_demo()
        await self.finish_current_speech()
        await action_started.wait()

        self.controller.on_user_started_speaking()
        self.controller.on_normal_transcription()
        await self.controller.on_bot_started_speaking()
        await self.controller.on_bot_stopped_speaking()
        self.controller.on_user_started_speaking()
        await self.controller.handle_control("pause_demo", "Pause the demo.")

        release_action.set()
        await self.controller.wait_for_action()
        self.assertEqual(self.controller.state, GuidedDemoState.PAUSED)
        self.assertFalse(self.controller.pending_auto_advance)

    async def test_control_turn_strategy_closes_without_a_transcript(self):
        strategy = ConsumedUserTurnStopStrategy()
        frame = ConsumedUserTurnFrame(generation=0)
        self.assertIsInstance(frame, UninterruptibleFrame)
        triggered = False

        async def trigger():
            nonlocal triggered
            triggered = True

        strategy.trigger_user_turn_stopped = trigger
        result = await strategy.process_frame(frame)
        self.assertEqual(result, ProcessFrameResult.STOP)
        self.assertTrue(triggered)

    async def test_control_turn_strategy_retries_after_an_overtaking_transcript(self):
        strategy = ConsumedUserTurnStopStrategy()
        trigger_count = 0

        async def trigger():
            nonlocal trigger_count
            trigger_count += 1

        strategy.trigger_user_turn_stopped = trigger
        await strategy.process_frame(ConsumedUserTurnFrame(generation=0))
        await strategy.process_frame(
            TranscriptionFrame(
                text="What does this product do?",
                user_id="user",
                timestamp="2026-07-28T00:00:00Z",
            )
        )
        self.assertEqual(trigger_count, 2)

    async def test_control_turn_strategy_rejects_an_overtaken_finalizer(self):
        strategy = ConsumedUserTurnStopStrategy()
        trigger_count = 0

        async def trigger():
            nonlocal trigger_count
            trigger_count += 1

        strategy.trigger_user_turn_stopped = trigger
        await strategy.process_frame(VADUserStartedSpeakingFrame())
        result = await strategy.process_frame(ConsumedUserTurnFrame(generation=0))
        self.assertEqual(result, ProcessFrameResult.STOP)
        self.assertEqual(trigger_count, 0)
