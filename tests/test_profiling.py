import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ui_control.profiling import LocalVoiceProfile, create_local_profile


class LocalVoiceProfileTestCase(unittest.TestCase):
    def test_profile_is_disabled_without_explicit_opt_in(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "disabled.jsonl"
            self.assertIsNone(
                create_local_profile({"KARUMI_PROFILE_PATH": str(path)})
            )
            self.assertFalse(path.exists())

    def test_explicit_empty_environment_does_not_use_process_variables(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "unexpected.jsonl"
            with patch.dict(
                "os.environ",
                {
                    "KARUMI_PROFILE": "1",
                    "KARUMI_PROFILE_PATH": str(path),
                },
                clear=True,
            ):
                profile = create_local_profile({})
                if profile:
                    profile.close()
                self.assertIsNone(profile)
            self.assertFalse(path.exists())

    def test_profile_writes_correlated_turn_and_action_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.jsonl"
            profile = create_local_profile(
                {
                    "KARUMI_PROFILE": "1",
                    "KARUMI_PROFILE_PATH": str(path),
                    "KARUMI_PROFILE_CASE": "A01",
                }
            )
            self.assertIsNotNone(profile)
            profile.start_run({"stt_model": "base"})
            profile.start_session()
            profile.start_turn()
            turn_id = profile.current_turn_id
            profile.speech_ended(silence_confirmation_ms=1000)
            profile.logical_turn_closed(1012.4)
            profile.transcription_received("Take me to Dashboard.", 1320.5)
            profile.resolution(
                "deterministic", {"command": "navigate_to", "view": "dashboard"}
            )
            profile.service_metric("stt", "processing", 725.2)
            profile.ui_action_started(
                "action-1", "command-1", "navigate_to", {"view": "dashboard"}
            )
            profile.ui_action_finished(
                "action-1",
                "command-1",
                "navigate_to",
                {"view": "dashboard", "ignored": "value"},
                "confirmed",
                84.2,
            )
            profile.first_bot_audio(2310.4, startup=False)
            profile.bot_stopped_speaking(410.3)
            profile.end_session("disconnected")
            profile.close()

            records = [json.loads(line) for line in path.read_text().splitlines()]
            summary = next(record for record in records if record["event"] == "turn_summary")
            action = next(
                record for record in records if record["event"] == "ui_action_finished"
            )

            self.assertEqual(summary["turn_id"], turn_id)
            self.assertEqual(summary["resolution"], "deterministic")
            self.assertEqual(summary["phase"], "first_user_turn")
            self.assertEqual(summary["metrics_ms"]["tts_total"], 410.3)
            self.assertEqual(summary["ui_outcomes"][0]["outcome"], "confirmed")
            self.assertEqual(action["case_id"], "A01")
            self.assertEqual(action["view"], "dashboard")
            self.assertNotIn("ignored", action)
            self.assertTrue(all(record["schema_version"] == 1 for record in records))

    def test_new_turn_finishes_the_previous_turn_as_interrupted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.jsonl"
            profile = LocalVoiceProfile(path)
            profile.start_session()
            profile.start_turn()
            first_turn_id = profile.current_turn_id
            profile.start_turn()
            profile.close()

            records = [json.loads(line) for line in path.read_text().splitlines()]
            summary = next(
                record
                for record in records
                if record["event"] == "turn_summary" and record["turn_id"] == first_turn_id
            )
            self.assertEqual(summary["outcome"], "interrupted")
