import unittest

from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.utils.text.markdown_text_filter import MarkdownTextFilter

from ui_control.catalog import load_catalog
from ui_control.intents import resolve_ui_intent
from ui_control.processor import UIActionExecutor, record_deterministic_turn


class UIControlTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.catalog = load_catalog()

    def compatible_state(self, current_page="home", visible_elements=None):
        return {
            "uiContractVersion": self.catalog.contract_version,
            "contractCompatible": True,
            "currentPage": current_page,
            "visibleElements": visible_elements or [],
            "supportedCommands": list(self.catalog.commands),
            "supportedViews": list(self.catalog.views),
            "supportedTargets": list(self.catalog.targets),
        }

    def test_catalog_preserves_the_current_contract(self):
        self.assertEqual(self.catalog.contract_version, 1)
        self.assertEqual(self.catalog.view_ids, ("dashboard", "analytics", "team"))
        self.assertEqual(
            self.catalog.views["team"]["defaultAction"],
            {"command": "scroll_to", "target": "team-member-list"},
        )
        self.assertTrue(
            self.catalog.is_contract_compatible(
                {
                    **self.compatible_state(),
                    "supportedViews": [*self.catalog.view_ids, "goals"],
                }
            )
        )

    def test_catalog_rejects_missing_required_capabilities(self):
        state = self.compatible_state()
        state["supportedCommands"] = ["navigate_to"]
        self.assertFalse(self.catalog.is_contract_compatible(state))

    def test_navigation_intents(self):
        cases = {
            "Take me to Dashboard": ("navigate_to", {"view": "dashboard"}),
            "Open analytics": ("navigate_to", {"view": "analytics"}),
            "Show me Team": ("navigate_to", {"view": "team"}),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(resolve_ui_intent(text, self.catalog), expected)

    def test_target_intents(self):
        self.assertEqual(
            resolve_ui_intent("Focus on total revenue", self.catalog),
            ("highlight", {"target": "total-revenue-card"}),
        )
        self.assertEqual(
            resolve_ui_intent("Emphasize traffic", self.catalog),
            ("highlight", {"target": "traffic-overview"}),
        )
        self.assertEqual(
            resolve_ui_intent("Scroll to the member list", self.catalog),
            ("scroll_to", {"target": "team-member-list"}),
        )

    def test_split_navigation_and_non_actions(self):
        self.assertEqual(
            resolve_ui_intent("to the analytics section", self.catalog, "Can you take me?"),
            ("navigate_to", {"view": "analytics"}),
        )
        self.assertIsNone(resolve_ui_intent("Tell me what analytics shows", self.catalog))
        self.assertIsNone(resolve_ui_intent("Open Dashboard and Analytics", self.catalog))
        self.assertIsNone(resolve_ui_intent("Can you take me?", self.catalog))

    async def test_navigation_runs_the_catalog_default_action(self):
        calls = []

        async def send_command(command, payload):
            calls.append((command, payload))
            if command == "navigate_to":
                return {"status": "confirmed", "currentPage": payload["view"]}
            return {"status": "confirmed", "target": payload["target"]}

        executor = UIActionExecutor(self.catalog, send_command, lambda: self.compatible_state())
        result = await executor.execute("navigate_to", {"view": "analytics"})
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(
            calls,
            [
                ("navigate_to", {"view": "analytics"}),
                ("highlight", {"target": "traffic-overview"}),
            ],
        )

    async def test_target_rejects_wrong_page_and_timeout_response(self):
        async def send_command(command, payload):
            return {"status": "timeout", "action": command}

        executor = UIActionExecutor(self.catalog, send_command, lambda: self.compatible_state())
        result = await executor.execute("highlight", {"target": "total-revenue-card"})
        self.assertEqual(result, {"status": "error", "code": "target_not_visible"})
        self.assertEqual(
            executor.response_for("navigate_to", {"view": "team"}, {"status": "timeout"}),
            "I couldn't update the interface. Please try again.",
        )

    async def test_target_timeout_and_unsupported_target(self):
        async def send_command(command, payload):
            return {"status": "timeout", "action": command}

        state = self.compatible_state("dashboard", ["total-revenue-card"])
        executor = UIActionExecutor(self.catalog, send_command, lambda: state)
        self.assertEqual(
            await executor.execute("highlight", {"target": "total-revenue-card"}),
            {"status": "timeout", "action": "highlight"},
        )
        self.assertEqual(
            await executor.execute("highlight", {"target": "team-member-list"}),
            {"status": "error", "code": "unsupported_target"},
        )

    def test_confirmation_requires_the_expected_command_and_target(self):
        self.assertTrue(
            self.catalog.confirmation_matches(
                "highlight",
                {"target": "traffic-overview"},
                {
                    "status": "ok",
                    "command": "highlight",
                    "currentPage": "analytics",
                    "target": "traffic-overview",
                },
            )
        )
        self.assertFalse(
            self.catalog.confirmation_matches(
                "highlight",
                {"target": "traffic-overview"},
                {
                    "status": "ok",
                    "command": "scroll_to",
                    "currentPage": "analytics",
                    "target": "traffic-overview",
                },
            )
        )

    def test_deterministic_turn_is_recorded_before_tts_without_duplicates(self):
        context = LLMContext()
        frame = record_deterministic_turn(
            context,
            "Take me to Team.",
            "The Team view is open, and I've scrolled to the member list.",
        )
        self.assertFalse(frame.append_to_context)
        self.assertEqual(
            context.messages,
            [
                {"role": "user", "content": "Take me to Team."},
                {
                    "role": "assistant",
                    "content": "The Team view is open, and I've scrolled to the member list.",
                },
            ],
        )

    async def test_spoken_text_removes_markdown_formatting(self):
        text_filter = MarkdownTextFilter()
        cases = {
            "Visit **Dashboard** next.": "Visit Dashboard next.",
            "Open *Analytics*.": "Open Analytics.",
            "Go to __Team__.": "Go to Team.",
            "Focus on `Traffic Overview`.": "Focus on Traffic Overview.",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(await text_filter.filter(source), expected)

    async def test_spoken_text_leaves_plain_responses_unchanged(self):
        text_filter = MarkdownTextFilter()
        response = "The Dashboard is open, with Total Revenue highlighted."
        self.assertEqual(await text_filter.filter(response), response)
