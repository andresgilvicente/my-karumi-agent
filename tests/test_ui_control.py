import asyncio
import unittest
from pathlib import Path

from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.frames.frames import TTSSpeakFrame, TranscriptionFrame
from pipecat.processors.frame_processor import FrameDirection
from pipecat.utils.text.markdown_text_filter import MarkdownTextFilter

from bot import pending_ui_action_for_session, reject_pending_ui_actions
from ui_control.catalog import load_catalog, validate_catalog
from ui_control.intents import fallback_tool_name, resolve_ui_intent
from ui_control.processor import (
    UIActionExecutor,
    UIActionToolChoiceProcessor,
    record_deterministic_turn,
)
from ui_control.turns import ConsumedUserTurnFrame


class UIControlTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.catalog = load_catalog()

    def compatible_state(self, current_page="home", visible_elements=None):
        return {
            "uiContractVersion": self.catalog.contract_version,
            "contractCompatible": True,
            "uiSessionId": "test-ui-session",
            "currentPage": current_page,
            "visibleElements": visible_elements or [],
            "supportedCommands": list(self.catalog.client_commands),
            "supportedViews": list(self.catalog.views),
            "supportedTargets": list(self.catalog.targets),
        }

    def test_catalog_preserves_the_current_contract(self):
        self.assertEqual(self.catalog.contract_version, 4)
        self.assertEqual(self.catalog.view_ids, ("dashboard", "analytics", "team"))
        self.assertEqual(
            set(self.catalog.target_ids),
            {
                "total-revenue-card",
                "active-users-card",
                "conversion-rate-card",
                "growth-rate-card",
                "performance-overview",
                "page-views-card",
                "click-through-rate-card",
                "session-duration-card",
                "analytics-conversion-card",
                "traffic-overview",
                "traffic-sources",
                "team-member-list",
            },
        )
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
        self.assertIn("clearSearchPhrases", self.catalog.data)
        self.assertGreater(len(self.catalog.data["clearSearchPhrases"]), 0)

    def test_catalog_rejects_missing_required_capabilities(self):
        state = self.compatible_state()
        state["supportedCommands"] = ["navigate_to"]
        self.assertFalse(self.catalog.is_contract_compatible(state))

    def test_catalog_rejects_non_object_command_definitions(self):
        data = {
            **self.catalog.data,
            "commands": {
                **self.catalog.commands,
                "clear_team_search": "invalid",
            },
        }
        with self.assertRaisesRegex(
            ValueError, "command clear_team_search must define an object"
        ):
            validate_catalog(data)

        data = {
            **self.catalog.data,
            "commands": {
                **self.catalog.commands,
                "clear_team_search": {
                    **self.catalog.commands["clear_team_search"],
                    "view": "unsupported",
                },
            },
        }
        with self.assertRaisesRegex(
            ValueError, "command clear_team_search must define a supported view"
        ):
            validate_catalog(data)

    def test_catalog_rejects_non_string_capabilities(self):
        state = self.compatible_state()
        state["supportedCommands"].append({"invalid": True})
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

    def test_natural_navigation_and_contextual_tim(self):
        cases = {
            "Can you please go to Dashboard?": "dashboard",
            "Can you go back to Analytics?": "analytics",
            "Go back to Team.": "team",
            "Could you navigate to Dashboard?": "dashboard",
            "Can you take me back to Analytics?": "analytics",
            "Please show me the Team section.": "team",
            "Would you open Dashboard?": "dashboard",
            "I want to return to Analytics.": "analytics",
            "Let's go back to Team.": "team",
            "Go to Tim.": "team",
            "Open the Tim section.": "team",
            "Show me the Tim members.": "team",
        }
        for text, view in cases.items():
            with self.subTest(text=text):
                self.assertEqual(
                    resolve_ui_intent(text, self.catalog),
                    ("navigate_to", {"view": view}),
                )

    def test_informational_view_and_tim_questions_do_not_navigate(self):
        cases = (
            "What is Analytics?",
            "What can I see in Dashboard?",
            "What can Dashboard show me?",
            "Tell me about the Team.",
            "Is Analytics useful?",
            "Who is Tim?",
            "Tell me about Tim.",
            "Tim is a person.",
            "I spoke with Tim.",
            "Go to Tim's profile.",
        )
        for text in cases:
            with self.subTest(text=text):
                self.assertIsNone(resolve_ui_intent(text, self.catalog))
                self.assertIsNone(fallback_tool_name(text, self.catalog))
        self.assertEqual(
            fallback_tool_name("Dashboard, please open it.", self.catalog),
            "navigate_to",
        )

    def test_ambiguous_and_generic_requests_do_not_force_a_tool(self):
        cases = (
            "Show me revenue and performance overview.",
            "Where is the company based?",
            "Can you show me?",
            "Show me Dashboard and Analytics.",
            "Search for revenue.",
            "Search for quarterly revenue.",
            "Search for monthly income.",
            "Search for an engineer.",
            "Search for the admin.",
        )
        for text in cases:
            with self.subTest(text=text):
                self.assertIsNone(resolve_ui_intent(text, self.catalog))
                self.assertIsNone(fallback_tool_name(text, self.catalog))

    def test_target_intents(self):
        cases = (
            ("Focus on total revenue", None, "total-revenue-card"),
            ("Could you show me Active Users?", None, "active-users-card"),
            ("Focus on Growth Rate", None, "growth-rate-card"),
            ("Focus on Performance Overview", None, "performance-overview"),
            ("Focus on Page Views", None, "page-views-card"),
            ("Focus on paid views", None, "page-views-card"),
            ("Highlight Click Through Rate", None, "click-through-rate-card"),
            ("Focus on Average Session Duration", None, "session-duration-card"),
            ("Emphasize traffic", None, "traffic-overview"),
            ("Can you focus on Traffic Sources?", None, "traffic-sources"),
            ("Take me to Total Revenue.", None, "total-revenue-card"),
            ("Where is the team member list?", None, "team-member-list"),
            ("Focus on Dashboard Conversion Rate", None, "conversion-rate-card"),
            ("Focus on Analytics Conversion Rate", None, "analytics-conversion-card"),
        )
        for text, current_view, target in cases:
            with self.subTest(text=text):
                self.assertEqual(
                    resolve_ui_intent(
                        text,
                        self.catalog,
                        current_view=current_view,
                    ),
                    ("focus_target", {"target": target}),
                )
        self.assertEqual(
            resolve_ui_intent("Scroll to the member list", self.catalog),
            ("scroll_to", {"target": "team-member-list"}),
        )

    def test_conversion_rate_uses_the_current_view(self):
        self.assertEqual(
            resolve_ui_intent(
                "Focus on Conversion Rate",
                self.catalog,
                current_view="dashboard",
            ),
            ("focus_target", {"target": "conversion-rate-card"}),
        )
        self.assertEqual(
            resolve_ui_intent(
                "Highlight Conversion Rate",
                self.catalog,
                current_view="analytics",
            ),
            ("focus_target", {"target": "analytics-conversion-card"}),
        )
        self.assertIsNone(resolve_ui_intent("Focus on Conversion Rate", self.catalog))
        self.assertEqual(
            resolve_ui_intent(
                "Focus on Conversion Rate in Analytics",
                self.catalog,
                current_view="dashboard",
            ),
            ("focus_target", {"target": "analytics-conversion-card"}),
        )
        self.assertEqual(
            resolve_ui_intent(
                "Focus on Conversion Rate on the Dashboard",
                self.catalog,
                current_view="analytics",
            ),
            ("focus_target", {"target": "conversion-rate-card"}),
        )

    def test_current_view_does_not_hide_a_multi_target_request(self):
        cases = (
            "Show me Total Revenue and Traffic Sources",
            "Show me Conversion Rate in Dashboard and Analytics",
            "Focus on Total Revenue in Analytics",
            "Focus on Traffic Sources in Dashboard",
            "Show me Total Revenue in Analytics",
        )
        for text in cases:
            with self.subTest(text=text):
                self.assertIsNone(
                    resolve_ui_intent(text, self.catalog, current_view="dashboard")
                )
                self.assertIsNone(fallback_tool_name(text, self.catalog, "dashboard"))

    def test_every_catalog_target_has_a_stable_frontend_id(self):
        app_source = (
            Path(__file__).resolve().parent.parent / "client" / "src" / "App.jsx"
        ).read_text()
        for target in self.catalog.target_ids:
            with self.subTest(target=target):
                self.assertIn(f'id="{target}"', app_source)

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
                ("scroll_to", {"target": "traffic-overview"}),
                ("highlight", {"target": "traffic-overview"}),
            ],
        )

    async def test_analytics_stops_when_centering_fails(self):
        calls = []

        async def send_command(command, payload):
            calls.append((command, payload))
            if command == "navigate_to":
                return {"status": "confirmed", "currentPage": payload["view"]}
            return {"status": "timeout", "action": command}

        executor = UIActionExecutor(self.catalog, send_command, lambda: self.compatible_state())
        result = await executor.execute("navigate_to", {"view": "analytics"})
        self.assertEqual(result["code"], "presentation_action_failed")
        self.assertEqual(
            calls,
            [
                ("navigate_to", {"view": "analytics"}),
                ("scroll_to", {"target": "traffic-overview"}),
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
            {"status": "error", "code": "target_not_visible"},
        )

    async def test_semantic_actions_are_serialized(self):
        calls = []
        first_command_started = asyncio.Event()
        release_first_command = asyncio.Event()

        async def send_command(command, payload):
            calls.append((command, payload))
            if len(calls) == 1:
                first_command_started.set()
                await release_first_command.wait()
            if command == "navigate_to":
                return {"status": "confirmed", "currentPage": payload["view"]}
            return {"status": "confirmed", "target": payload["target"]}

        executor = UIActionExecutor(self.catalog, send_command, lambda: self.compatible_state())
        dashboard = asyncio.create_task(
            executor.execute("navigate_to", {"view": "dashboard"})
        )
        await first_command_started.wait()
        analytics = asyncio.create_task(
            executor.execute("navigate_to", {"view": "analytics"})
        )
        await asyncio.sleep(0)
        self.assertEqual(calls, [("navigate_to", {"view": "dashboard"})])

        release_first_command.set()
        await asyncio.gather(dashboard, analytics)
        self.assertEqual(
            calls,
            [
                ("navigate_to", {"view": "dashboard"}),
                ("scroll_to", {"target": "total-revenue-card"}),
                ("highlight", {"target": "total-revenue-card"}),
                ("navigate_to", {"view": "analytics"}),
                ("scroll_to", {"target": "traffic-overview"}),
                ("highlight", {"target": "traffic-overview"}),
            ],
        )

    async def test_action_sequence_stops_when_session_generation_changes(self):
        calls = []
        generation = 1

        async def send_command(command, payload):
            nonlocal generation
            calls.append((command, payload))
            generation += 1
            return {"status": "confirmed", "currentPage": payload.get("view")}

        executor = UIActionExecutor(
            self.catalog,
            send_command,
            lambda: self.compatible_state(),
            lambda: generation,
        )
        result = await executor.execute("navigate_to", {"view": "analytics"})
        self.assertEqual(result, {"status": "error", "code": "session_changed"})
        self.assertEqual(calls, [("navigate_to", {"view": "analytics"})])

    async def test_focuses_every_declared_target_with_confirmed_scroll_and_highlight(self):
        for target, definition in self.catalog.targets.items():
            calls = []

            async def send_command(command, payload):
                calls.append((command, payload))
                if command == "navigate_to":
                    return {"status": "confirmed", "currentPage": payload["view"]}
                return {"status": "confirmed", "target": payload["target"]}

            executor = UIActionExecutor(
                self.catalog,
                send_command,
                lambda: self.compatible_state("home"),
            )
            result = await executor.execute("focus_target", {"target": target})
            with self.subTest(target=target):
                self.assertEqual(result["status"], "confirmed")
                self.assertEqual(result["label"], definition["label"])
                self.assertEqual(
                    calls,
                    [
                        ("navigate_to", {"view": definition["view"]}),
                        ("scroll_to", {"target": target}),
                        ("highlight", {"target": target}),
                    ],
                )

    async def test_focus_failure_stops_before_highlight(self):
        calls = []

        async def send_command(command, payload):
            calls.append((command, payload))
            if command == "navigate_to":
                return {"status": "confirmed", "currentPage": payload["view"]}
            return {"status": "timeout", "action": command}

        executor = UIActionExecutor(self.catalog, send_command, lambda: self.compatible_state())
        result = await executor.execute(
            "focus_target", {"target": "performance-overview"}
        )
        self.assertEqual(result["code"], "scroll_failed")
        self.assertTrue(result["screenConfirmed"])
        self.assertEqual(len(calls), 2)

    async def test_same_screen_focus_failure_does_not_claim_navigation(self):
        async def send_command(command, payload):
            return {"status": "timeout", "action": command}

        state = self.compatible_state("dashboard", ["active-users-card"])
        executor = UIActionExecutor(self.catalog, send_command, lambda: state)
        result = await executor.execute(
            "focus_target", {"target": "active-users-card"}
        )
        self.assertEqual(result["code"], "scroll_failed")
        self.assertFalse(result["screenConfirmed"])

    async def test_focus_navigates_from_dashboard_goals_to_overview(self):
        calls = []
        state = self.compatible_state("dashboard", [])
        state["selectedTab"] = "goals"

        async def send_command(command, payload):
            calls.append((command, payload))
            return {"status": "confirmed", "action": command}

        executor = UIActionExecutor(self.catalog, send_command, lambda: state)
        result = await executor.execute(
            "focus_target", {"target": "active-users-card"}
        )
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(calls[0], ("navigate_to", {"view": "dashboard"}))

    async def test_focus_highlight_failure_is_reported(self):
        calls = []

        async def send_command(command, payload):
            calls.append((command, payload))
            if command in {"navigate_to", "scroll_to"}:
                return {"status": "confirmed", "action": command}
            return {"status": "timeout", "action": command}

        executor = UIActionExecutor(self.catalog, send_command, lambda: self.compatible_state())
        result = await executor.execute("focus_target", {"target": "active-users-card"})
        self.assertEqual(result["code"], "highlight_failed")
        self.assertEqual(
            [command for command, _ in calls],
            ["navigate_to", "scroll_to", "highlight"],
        )

    async def test_team_search_uses_only_confirmed_results(self):
        cases = (
            ("Nobody", []),
            ("Olivia", ["Olivia Chen"]),
            ("a", ["Olivia Chen", "Sophia Patel", "Liam Brown"]),
        )
        for query, names in cases:
            calls = []

            async def send_command(command, payload):
                calls.append((command, payload))
                if command == "navigate_to":
                    return {"status": "confirmed", "currentPage": "team"}
                return {
                    "status": "confirmed",
                    "query": payload["query"],
                    "matchCount": len(names),
                    "matchedNames": names,
                }

            executor = UIActionExecutor(self.catalog, send_command, lambda: self.compatible_state())
            result = await executor.execute("search_team_member", {"query": query})
            with self.subTest(query=query):
                self.assertEqual(result["matchCount"], len(names))
                self.assertEqual(result["matchedNames"], names)
                self.assertEqual(
                    calls,
                    [
                        ("navigate_to", {"view": "team"}),
                        ("search_team_member", {"query": query}),
                    ],
                )

    async def test_team_search_rejects_empty_query(self):
        calls = []

        async def send_command(command, payload):
            calls.append((command, payload))
            return {"status": "confirmed"}

        executor = UIActionExecutor(self.catalog, send_command, lambda: self.compatible_state())
        self.assertEqual(
            await executor.execute("search_team_member", {"query": "  "}),
            {"status": "error", "code": "empty_query"},
        )
        self.assertEqual(calls, [])

    def test_team_search_phrases_extract_only_the_name(self):
        cases = {
            "Search for Juan.": "Juan",
            "Search for Olivia.": "Olivia",
            "Is there anyone called Juan?": "Juan",
            "Does someone named Sarah work here?": "Sarah",
            "Does someone named Michael work here?": "Michael",
            "Find a team member called Michael.": "Michael",
            "Find a team member called Alex.": "Alex",
            "Do we have a Juan on the team?": "Juan",
            "Is anyone named Alex?": "Alex",
            "Could you search for Olivia?": "Olivia",
            "Search for Juan on the team.": "Juan",
            "Search for Li.": "Li",
        }
        for text, query in cases.items():
            with self.subTest(text=text):
                self.assertEqual(
                    resolve_ui_intent(text, self.catalog),
                    ("search_team_member", {"query": query}),
                )

    def test_team_clear_search_phrases(self):
        for text in self.catalog.data["clearSearchPhrases"]:
            with self.subTest(text=text):
                self.assertEqual(
                    resolve_ui_intent(text, self.catalog),
                    ("clear_team_search", {}),
                )

    def test_team_clear_search_with_prefixes(self):
        cases = (
            "can you clear the search",
            "could you clear search",
            "please clear filter",
            "could you clear the filter",
            "can you reset search",
            "please show all members",
        )
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(
                    resolve_ui_intent(text, self.catalog),
                    ("clear_team_search", {}),
                )

    def test_documented_ui_phrases_match_the_resolver(self):
        cases = {
            "Go to Dashboard.": ("navigate_to", {"view": "dashboard"}),
            "Go to Analytics.": ("navigate_to", {"view": "analytics"}),
            "Can you please open Analytics?": ("navigate_to", {"view": "analytics"}),
            "Take me back to Team.": ("navigate_to", {"view": "team"}),
            "Could you navigate to Dashboard?": ("navigate_to", {"view": "dashboard"}),
            "Show me the Team section.": ("navigate_to", {"view": "team"}),
            "Take me to Analytics.": ("navigate_to", {"view": "analytics"}),
            "Show me Total Revenue.": ("focus_target", {"target": "total-revenue-card"}),
            "Show me Active Users.": ("focus_target", {"target": "active-users-card"}),
            "Focus on Active Users.": ("focus_target", {"target": "active-users-card"}),
            "Focus on Dashboard Conversion Rate.": (
                "focus_target",
                {"target": "conversion-rate-card"},
            ),
            "Focus on Growth Rate.": ("focus_target", {"target": "growth-rate-card"}),
            "Highlight Performance Overview.": (
                "focus_target",
                {"target": "performance-overview"},
            ),
            "Focus on Page Views.": ("focus_target", {"target": "page-views-card"}),
            "Highlight Click-through Rate.": (
                "focus_target",
                {"target": "click-through-rate-card"},
            ),
            "Focus on Average Session Duration.": (
                "focus_target",
                {"target": "session-duration-card"},
            ),
            "Focus on Analytics Conversion Rate.": (
                "focus_target",
                {"target": "analytics-conversion-card"},
            ),
            "Show me Traffic Overview.": ("focus_target", {"target": "traffic-overview"}),
            "Focus on Traffic Sources.": ("focus_target", {"target": "traffic-sources"}),
            "Take me to the team member list.": (
                "focus_target",
                {"target": "team-member-list"},
            ),
            "Is there anyone called Juan?": ("search_team_member", {"query": "Juan"}),
            "Search for Olivia.": ("search_team_member", {"query": "Olivia"}),
            "Find a team member called Alex.": (
                "search_team_member",
                {"query": "Alex"},
            ),
            "Does someone named Michael work here?": (
                "search_team_member",
                {"query": "Michael"},
            ),
            "Search for Li.": (
                "search_team_member",
                {"query": "Li"},
            ),
            "Clear the search.": ("clear_team_search", {}),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(resolve_ui_intent(text, self.catalog), expected)

        for text in (
            "What does this product do?",
            "Open the Goals tab.",
            "What does the Analytics section show?",
            "Show me Dashboard and Analytics.",
            "Show me Total Revenue and Traffic Sources.",
            "Focus on Traffic Sources in Dashboard.",
            "Search for revenue.",
            "Search for an engineer.",
            "Scroll down.",
            "Click the second analytics tab.",
            "Go to Tim's profile.",
        ):
            with self.subTest(text=text):
                self.assertIsNone(resolve_ui_intent(text, self.catalog))
                self.assertIsNone(fallback_tool_name(text, self.catalog))

    async def test_team_clear_search_execution(self):
        calls = []

        async def send_command(command, payload):
            calls.append((command, payload))
            return {"status": "confirmed"}

        executor = UIActionExecutor(
            self.catalog, send_command, lambda: self.compatible_state("team", ["team-member-list"])
        )
        result = await executor.execute("clear_team_search", {})
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(result["action"], "clear_team_search")
        self.assertEqual(calls, [("clear_team_search", {})])

    async def test_team_clear_search_navigates_to_team(self):
        calls = []

        async def send_command(command, payload):
            calls.append((command, payload))
            if command == "navigate_to":
                return {"status": "confirmed", "currentPage": "team"}
            return {"status": "confirmed"}

        executor = UIActionExecutor(self.catalog, send_command, lambda: self.compatible_state())
        result = await executor.execute("clear_team_search", {})
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(result["action"], "clear_team_search")
        self.assertEqual(
            calls,
            [
                ("navigate_to", {"view": "team"}),
                ("clear_team_search", {}),
            ],
        )

    async def test_team_clear_search_does_not_confirm_after_session_change(self):
        calls = []
        generation = 1

        async def send_command(command, payload):
            nonlocal generation
            calls.append((command, payload))
            generation += 1
            return {"status": "confirmed", "cleared": True}

        executor = UIActionExecutor(
            self.catalog,
            send_command,
            lambda: self.compatible_state("team", ["team-member-list"]),
            lambda: generation,
        )
        self.assertEqual(
            await executor.execute("clear_team_search", {}),
            {"status": "error", "code": "session_changed", "view": "team", "screenConfirmed": False},
        )
        self.assertEqual(calls, [("clear_team_search", {})])

    async def test_deterministic_ui_intent_closes_the_consumed_user_turn(self):
        context = LLMContext()
        pushed = []

        async def execute(command, payload):
            return {"status": "confirmed", "action": command, **payload}

        processor = UIActionToolChoiceProcessor(
            context,
            self.catalog,
            {},
            execute,
            lambda command, payload, result: "Confirmed.",
        )

        async def capture(frame, direction=FrameDirection.DOWNSTREAM):
            pushed.append((frame, direction))

        processor.push_frame = capture
        await processor.process_frame(
            TranscriptionFrame(
                text="Take me to Dashboard.",
                user_id="user",
                timestamp="2026-07-28T00:00:00Z",
            ),
            FrameDirection.DOWNSTREAM,
        )

        self.assertIsInstance(pushed[0][0], ConsumedUserTurnFrame)
        self.assertEqual(pushed[0][0].generation, 0)
        self.assertIsInstance(pushed[1][0], TTSSpeakFrame)

    async def test_processor_resolves_conversion_rate_from_live_view(self):
        calls = []

        async def execute(command, payload):
            calls.append((command, payload))
            return {"status": "confirmed", "action": command, **payload}

        processor = UIActionToolChoiceProcessor(
            LLMContext(),
            self.catalog,
            {},
            execute,
            lambda command, payload, result: "Confirmed.",
            get_current_view=lambda: "analytics",
        )
        processor.push_frame = lambda *args, **kwargs: asyncio.sleep(0)

        await processor.process_frame(
            TranscriptionFrame(
                text="Focus on Conversion Rate.",
                user_id="user",
                timestamp="2026-07-28T00:00:00Z",
            ),
            FrameDirection.DOWNSTREAM,
        )

        self.assertEqual(
            calls,
            [("focus_target", {"target": "analytics-conversion-card"})],
        )

    async def test_session_change_after_turn_consumption_prevents_old_action(self):
        context = LLMContext()
        generation = 1
        executed = False

        async def execute(command, payload):
            nonlocal executed
            executed = True
            return {"status": "confirmed", "action": command, **payload}

        processor = UIActionToolChoiceProcessor(
            context,
            self.catalog,
            {},
            execute,
            lambda command, payload, result: "Confirmed.",
            get_session_generation=lambda: generation,
        )

        async def capture(frame, direction=FrameDirection.DOWNSTREAM):
            nonlocal generation
            if isinstance(frame, ConsumedUserTurnFrame):
                generation += 1

        processor.push_frame = capture
        await processor.process_frame(
            TranscriptionFrame(
                text="Open Dashboard.",
                user_id="user",
                timestamp="2026-07-28T00:00:00Z",
            ),
            FrameDirection.DOWNSTREAM,
        )
        self.assertFalse(executed)

    def test_ui_intent_session_reset_clears_transcript_context(self):
        processor = UIActionToolChoiceProcessor(
            LLMContext(),
            self.catalog,
            {},
            lambda command, payload: None,
            lambda command, payload, result: "Confirmed.",
        )
        processor._previous_transcript = "Can you take me?"
        processor._previous_transcript_at = 1.0
        processor.reset_session()
        self.assertEqual(processor._previous_transcript, "")
        self.assertEqual(processor._previous_transcript_at, 0.0)

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
        self.assertFalse(
            self.catalog.confirmation_matches(
                "navigate_to",
                {"view": "dashboard", "uiSessionId": "new-session"},
                {
                    "status": "ok",
                    "command": "navigate_to",
                    "currentPage": "dashboard",
                    "uiSessionId": "old-session",
                },
            )
        )
        self.assertFalse(
            self.catalog.confirmation_matches(
                "search_team_member",
                {"query": "Olivia"},
                {
                    "status": "ok",
                    "command": "search_team_member",
                    "currentPage": "team",
                    "query": "Olivia",
                    "matchCount": 2,
                    "matchedNames": ["Olivia Chen"],
                },
            )
        )

    def test_clear_search_confirmation_requires_team_and_cleared_state(self):
        expected = {"uiSessionId": "current-session"}
        self.assertTrue(
            self.catalog.confirmation_matches(
                "clear_team_search",
                expected,
                {
                    "status": "ok",
                    "command": "clear_team_search",
                    "currentPage": "team",
                    "cleared": True,
                    "uiSessionId": "current-session",
                },
            )
        )
        for result in (
            {
                "status": "ok",
                "command": "clear_team_search",
                "currentPage": "analytics",
                "cleared": True,
                "uiSessionId": "current-session",
            },
            {
                "status": "ok",
                "command": "clear_team_search",
                "currentPage": "team",
                "cleared": False,
                "uiSessionId": "current-session",
            },
            {
                "status": "ok",
                "command": "clear_team_search",
                "currentPage": "team",
                "cleared": True,
                "uiSessionId": "old-session",
            },
            {
                "command": "clear_team_search",
                "currentPage": "team",
                "cleared": True,
                "uiSessionId": "current-session",
            },
            {
                "status": "confirmed",
                "command": "clear_team_search",
                "currentPage": "team",
                "cleared": True,
                "uiSessionId": "current-session",
            },
            {
                "status": [],
                "command": "clear_team_search",
                "currentPage": "team",
                "cleared": True,
                "uiSessionId": "current-session",
            },
        ):
            with self.subTest(result=result):
                self.assertFalse(
                    self.catalog.confirmation_matches(
                        "clear_team_search",
                        expected,
                        result,
                    )
                )

    async def test_session_cleanup_rejects_pending_actions(self):
        loop = asyncio.get_running_loop()
        first = loop.create_future()
        second = loop.create_future()
        pending = {
            "one": (first, "navigate_to", {"view": "dashboard"}, 1),
            "two": (second, "highlight", {"target": "total-revenue-card"}, 1),
        }
        self.assertEqual(reject_pending_ui_actions(pending, "client_disconnected"), 2)
        self.assertEqual(first.result()["code"], "client_disconnected")
        self.assertEqual(second.result()["command"], "highlight")
        self.assertEqual(pending, {})

    async def test_stale_session_confirmation_cannot_resolve_a_new_session(self):
        future = asyncio.get_running_loop().create_future()
        pending = {
            "old-command": (
                future,
                "navigate_to",
                {"view": "dashboard"},
                3,
            )
        }
        self.assertIsNone(
            pending_ui_action_for_session(pending, "old-command", session_generation=4)
        )
        self.assertIs(
            pending_ui_action_for_session(pending, "old-command", session_generation=3),
            pending["old-command"],
        )

    async def test_stale_clear_confirmation_cannot_resolve_a_new_session(self):
        future = asyncio.get_running_loop().create_future()
        expected_payload = {"uiSessionId": "old-ui-session"}
        coherent_result = {
            "status": "ok",
            "command": "clear_team_search",
            "currentPage": "team",
            "cleared": True,
            "uiSessionId": "old-ui-session",
        }
        pending = {
            "old-clear": (
                future,
                "clear_team_search",
                expected_payload,
                7,
            )
        }
        self.assertTrue(
            self.catalog.confirmation_matches(
                "clear_team_search", expected_payload, coherent_result
            )
        )
        self.assertIsNone(
            pending_ui_action_for_session(pending, "old-clear", session_generation=8)
        )
        self.assertFalse(future.done())

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
