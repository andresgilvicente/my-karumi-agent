import asyncio
import time
from collections.abc import Awaitable, Callable

from loguru import logger
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.frames.frames import TTSSpeakFrame, TranscriptionFrame
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.services.llm_service import FunctionCallParams

from ui_control.catalog import UIControlCatalog
from ui_control.intents import fallback_tool_name, resolve_ui_intent
from ui_control.turns import ConsumedUserTurnFrame


def record_deterministic_turn(context: LLMContext, user_text: str, response: str) -> TTSSpeakFrame:
    """Commit the confirmed result before audio so an interruption keeps the turn coherent."""
    context.add_message({"role": "user", "content": user_text.strip()})
    context.add_message({"role": "assistant", "content": response})
    return TTSSpeakFrame(response, append_to_context=False)


class UIActionExecutor:
    """Runs catalog actions through the confirmation-aware RTVI command sender."""

    def __init__(
        self,
        catalog: UIControlCatalog,
        send_command: Callable[[str, dict], Awaitable[dict]],
        get_ui_state: Callable[[], dict | None],
        get_session_generation: Callable[[], int] | None = None,
    ):
        self._catalog = catalog
        self._send_command = send_command
        self._get_ui_state = get_ui_state
        self._get_session_generation = get_session_generation
        self._action_lock = asyncio.Lock()

    async def execute(self, command: str, payload: dict) -> dict:
        expected_generation = (
            self._get_session_generation() if self._get_session_generation else None
        )
        async with self._action_lock:
            if not self._generation_matches(expected_generation):
                return {"status": "error", "code": "session_changed"}
            return await self._execute(command, payload, expected_generation)

    def _generation_matches(self, expected_generation: int | None) -> bool:
        return (
            self._get_session_generation is None
            or expected_generation == self._get_session_generation()
        )

    async def _send(
        self, command: str, payload: dict, expected_generation: int | None
    ) -> dict:
        if not self._generation_matches(expected_generation):
            return {"status": "error", "code": "session_changed"}
        result = await self._send_command(command, payload)
        if not self._generation_matches(expected_generation):
            return {"status": "error", "code": "session_changed"}
        return result

    async def _execute(
        self, command: str, payload: dict, expected_generation: int | None
    ) -> dict:
        if command == "navigate_to":
            return await self._navigate(payload.get("view"), expected_generation)
        if command in {"highlight", "scroll_to"}:
            return await self._target(command, payload.get("target"), expected_generation)
        if command == "focus_target":
            return await self._focus_target(payload.get("target"), expected_generation)
        if command == "search_team_member":
            return await self._search_team_member(payload.get("query"), expected_generation)
        return {"status": "error", "code": "unsupported_action"}

    async def _navigate(self, view: str | None, expected_generation: int | None) -> dict:
        if view not in self._catalog.views:
            return {"status": "error", "code": "unsupported_view"}

        navigation = await self._navigate_only(view, expected_generation)
        if navigation.get("status") != "confirmed":
            return navigation

        presentation_actions = []
        for action in self._catalog.views[view]["navigationActions"]:
            result = await self._send(
                action["command"], {"target": action["target"]}, expected_generation
            )
            presentation_actions.append(result)
            if result.get("status") != "confirmed":
                return {
                    "status": "error",
                    "code": "presentation_action_failed",
                    "view": view,
                    "screenConfirmed": True,
                    "navigation": navigation,
                    "presentationActions": presentation_actions,
                }

        return {
            "status": "confirmed",
            "action": "navigate_to",
            "view": view,
            "navigation": navigation,
            "presentationActions": presentation_actions,
        }

    async def _navigate_only(self, view: str, expected_generation: int | None) -> dict:
        return await self._send("navigate_to", {"view": view}, expected_generation)

    async def _target(
        self, command: str, target: str | None, expected_generation: int | None
    ) -> dict:
        definition = self._catalog.targets.get(target)
        if definition is None or not self._catalog.target_supports_command(target, command):
            return {"status": "error", "code": "unsupported_target"}

        current_ui_state = self._get_ui_state()
        if (
            current_ui_state is None
            or current_ui_state.get("currentPage") != definition["view"]
            or target not in current_ui_state.get("visibleElements", [])
        ):
            return {"status": "error", "code": "target_not_visible"}

        return await self._send(command, {"target": target}, expected_generation)

    async def _focus_target(
        self, target: str | None, expected_generation: int | None
    ) -> dict:
        definition = self._catalog.targets.get(target)
        if definition is None:
            return {"status": "error", "code": "unsupported_target"}

        view = definition["view"]
        current_ui_state = self._get_ui_state()
        navigation = None
        screen_already_visible = (
            current_ui_state is not None
            and current_ui_state.get("currentPage") == view
            and target in current_ui_state.get("visibleElements", [])
        )
        if not screen_already_visible:
            navigation = await self._navigate_only(view, expected_generation)
            if navigation.get("status") != "confirmed":
                return navigation

        scroll = await self._send("scroll_to", {"target": target}, expected_generation)
        if scroll.get("status") != "confirmed":
            return {
                "status": "error",
                "code": "scroll_failed",
                "view": view,
                "screenConfirmed": navigation is not None,
                "navigation": navigation,
                "scroll": scroll,
            }
        highlight = await self._send("highlight", {"target": target}, expected_generation)
        if highlight.get("status") != "confirmed":
            return {
                "status": "error",
                "code": "highlight_failed",
                "view": view,
                "screenConfirmed": navigation is not None,
                "navigation": navigation,
                "scroll": scroll,
                "highlight": highlight,
            }
        return {
            "status": "confirmed",
            "action": "focus_target",
            "view": view,
            "target": target,
            "label": definition["label"],
            "navigation": navigation,
            "scroll": scroll,
            "highlight": highlight,
        }

    async def _search_team_member(
        self, query: str | None, expected_generation: int | None
    ) -> dict:
        query = query.strip() if isinstance(query, str) else ""
        if not query:
            return {"status": "error", "code": "empty_query"}

        current_ui_state = self._get_ui_state()
        navigation = None
        if current_ui_state is None or current_ui_state.get("currentPage") != "team":
            navigation = await self._navigate_only("team", expected_generation)
            if navigation.get("status") != "confirmed":
                return navigation

        search = await self._send(
            "search_team_member", {"query": query}, expected_generation
        )
        if search.get("status") != "confirmed":
            return {
                **search,
                "view": "team",
                "screenConfirmed": navigation is not None,
            }
        return {
            "status": "confirmed",
            "action": "search_team_member",
            "view": "team",
            "navigation": navigation,
            **{key: search[key] for key in ("query", "matchCount", "matchedNames")},
        }

    def response_for(self, command: str, payload: dict, result: dict) -> str:
        if result.get("status") != "confirmed":
            return self._catalog.data["errorMessage"]
        if command == "navigate_to":
            return self._catalog.views[payload["view"]]["successMessage"]
        if command == "search_team_member":
            count = result["matchCount"]
            if count == 0:
                return f"I couldn't find a Team member named {result['query']}."
            names = ", ".join(result["matchedNames"])
            if count == 1:
                return f"I found {names} and highlighted the result in Team."
            return f"I found {count} matching Team members: {names}."
        target = self._catalog.targets[payload["target"]]
        if command == "scroll_to":
            return f"I've centered {target['label']}."
        if command == "highlight":
            return f"I've highlighted {target['label']}."
        return target["successMessage"]

    def build_tools(
        self,
        context: LLMContext,
        on_resolution: Callable[[str, dict], None] | None = None,
        on_action_result: Callable[[str, dict, dict], Awaitable[None]] | None = None,
        get_session_generation: Callable[[], int] | None = None,
    ) -> dict[str, FunctionSchema]:
        async def finish_tool_call(params: FunctionCallParams, result: dict) -> None:
            context.set_tools([])
            context.set_tool_choice("auto")
            await params.result_callback(result)

        async def handle_navigation(params: FunctionCallParams) -> None:
            logger.opt(colors=True).debug("<yellow>[UI]</yellow> Resolution path: function_call")
            if on_resolution:
                on_resolution("function_call", {"command": "navigate_to"})
            payload = {"view": params.arguments.get("view")}
            session_generation = get_session_generation() if get_session_generation else None
            result = await self.execute("navigate_to", payload)
            if get_session_generation and session_generation != get_session_generation():
                return
            if on_action_result:
                await on_action_result("navigate_to", payload, result)
            await finish_tool_call(
                params,
                result,
            )

        async def handle_highlight(params: FunctionCallParams) -> None:
            logger.opt(colors=True).debug("<yellow>[UI]</yellow> Resolution path: function_call")
            if on_resolution:
                on_resolution("function_call", {"command": "highlight"})
            payload = {"target": params.arguments.get("target")}
            session_generation = get_session_generation() if get_session_generation else None
            result = await self.execute("highlight", payload)
            if get_session_generation and session_generation != get_session_generation():
                return
            if on_action_result:
                await on_action_result("highlight", payload, result)
            await finish_tool_call(
                params,
                result,
            )

        async def handle_scroll(params: FunctionCallParams) -> None:
            logger.opt(colors=True).debug("<yellow>[UI]</yellow> Resolution path: function_call")
            if on_resolution:
                on_resolution("function_call", {"command": "scroll_to"})
            payload = {"target": params.arguments.get("target")}
            session_generation = get_session_generation() if get_session_generation else None
            result = await self.execute("scroll_to", payload)
            if get_session_generation and session_generation != get_session_generation():
                return
            if on_action_result:
                await on_action_result("scroll_to", payload, result)
            await finish_tool_call(
                params,
                result,
            )

        async def handle_focus(params: FunctionCallParams) -> None:
            logger.opt(colors=True).debug("<yellow>[UI]</yellow> Resolution path: function_call")
            if on_resolution:
                on_resolution("function_call", {"command": "focus_target"})
            payload = {"target": params.arguments.get("target")}
            session_generation = get_session_generation() if get_session_generation else None
            result = await self.execute("focus_target", payload)
            if get_session_generation and session_generation != get_session_generation():
                return
            if on_action_result:
                await on_action_result("focus_target", payload, result)
            await finish_tool_call(params, result)

        async def handle_team_search(params: FunctionCallParams) -> None:
            logger.opt(colors=True).debug("<yellow>[UI]</yellow> Resolution path: function_call")
            if on_resolution:
                on_resolution("function_call", {"command": "search_team_member"})
            payload = {"query": params.arguments.get("query")}
            session_generation = get_session_generation() if get_session_generation else None
            result = await self.execute("search_team_member", payload)
            if get_session_generation and session_generation != get_session_generation():
                return
            if on_action_result:
                await on_action_result("search_team_member", payload, result)
            await finish_tool_call(params, result)

        return {
            "navigate_to": FunctionSchema(
                name="navigate_to",
                description=self._catalog.commands["navigate_to"]["description"],
                properties={
                    "view": {
                        "type": "string",
                        "enum": list(self._catalog.view_ids),
                        "description": "The destination requested by the user.",
                    }
                },
                required=["view"],
                handler=handle_navigation,
            ),
            "highlight": FunctionSchema(
                name="highlight",
                description=self._catalog.commands["highlight"]["description"],
                properties={
                    "target": {
                        "type": "string",
                        "enum": list(self._catalog.targets_for_command("highlight")),
                    }
                },
                required=["target"],
                handler=handle_highlight,
            ),
            "scroll_to": FunctionSchema(
                name="scroll_to",
                description=self._catalog.commands["scroll_to"]["description"],
                properties={
                    "target": {
                        "type": "string",
                        "enum": list(self._catalog.targets_for_command("scroll_to")),
                    }
                },
                required=["target"],
                handler=handle_scroll,
            ),
            "focus_target": FunctionSchema(
                name="focus_target",
                description=self._catalog.commands["focus_target"]["description"],
                properties={
                    "target": {
                        "type": "string",
                        "enum": list(self._catalog.target_ids),
                    }
                },
                required=["target"],
                handler=handle_focus,
            ),
            "search_team_member": FunctionSchema(
                name="search_team_member",
                description=self._catalog.commands["search_team_member"]["description"],
                properties={
                    "query": {
                        "type": "string",
                        "description": "The member name requested by the user.",
                    }
                },
                required=["query"],
                handler=handle_team_search,
            ),
        }


class UIActionToolChoiceProcessor(FrameProcessor):
    """Handles explicit UI requests before the LLM and configures its limited fallback."""

    def __init__(
        self,
        context: LLMContext,
        catalog: UIControlCatalog,
        tools: dict[str, FunctionSchema],
        execute_action: Callable[[str, dict], Awaitable[dict]],
        response_for: Callable[[str, dict, dict], str],
        on_resolution: Callable[[str, dict], None] | None = None,
        on_action_result: Callable[[str, dict, dict], Awaitable[None]] | None = None,
        get_turn_generation: Callable[[], int] | None = None,
        get_session_generation: Callable[[], int] | None = None,
        get_current_view: Callable[[], str | None] | None = None,
    ):
        super().__init__()
        self._context = context
        self._catalog = catalog
        self._tools = tools
        self._execute_action = execute_action
        self._response_for = response_for
        self._on_resolution = on_resolution
        self._on_action_result = on_action_result
        self._get_turn_generation = get_turn_generation or (lambda: 0)
        self._get_session_generation = get_session_generation
        self._get_current_view = get_current_view or (lambda: None)
        self._previous_transcript = ""
        self._previous_transcript_at = 0.0

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if not isinstance(frame, TranscriptionFrame):
            await self.push_frame(frame, direction)
            return

        frame_session_generation = (
            self._get_session_generation() if self._get_session_generation else None
        )
        text = frame.text.casefold()
        now = time.monotonic()
        previous_text = (
            self._previous_transcript if now - self._previous_transcript_at <= 6.0 else ""
        )
        current_view = self._get_current_view()
        intent = resolve_ui_intent(
            text,
            self._catalog,
            previous_text,
            current_view,
        )
        self._previous_transcript = text
        self._previous_transcript_at = now

        if intent is not None:
            command, payload = intent
            self._context.set_tools([])
            self._context.set_tool_choice("auto")
            logger.opt(colors=True).debug("<yellow>[UI]</yellow> Resolution path: deterministic")
            logger.opt(colors=True).debug("<yellow>[UI]</yellow> Deterministic intent: {} {}", command, payload)
            if self._on_resolution:
                self._on_resolution("deterministic", {"command": command, **payload})
            await self.push_frame(
                ConsumedUserTurnFrame(generation=self._get_turn_generation()),
                direction,
            )
            if (
                self._get_session_generation
                and frame_session_generation != self._get_session_generation()
            ):
                return
            result = await self._execute_action(command, payload)
            if (
                self._get_session_generation
                and frame_session_generation != self._get_session_generation()
            ):
                return
            if self._on_action_result:
                await self._on_action_result(command, payload, result)
            response = self._response_for(command, payload, result)
            logger.opt(colors=True).debug("<yellow>[UI]</yellow> Deterministic result: {}", result)
            self._previous_transcript = ""
            self._previous_transcript_at = 0.0
            await self.push_frame(record_deterministic_turn(self._context, frame.text, response), direction)
            return

        selected_tool_name = fallback_tool_name(text, self._catalog, current_view)
        selected_tool = self._tools.get(selected_tool_name) if selected_tool_name else None
        self._context.set_tools([selected_tool] if selected_tool else [])
        self._context.set_tool_choice("required" if selected_tool else "auto")
        if selected_tool is None:
            logger.opt(colors=True).debug("<yellow>[UI]</yellow> Resolution path: conversation")
            if self._on_resolution:
                self._on_resolution("conversation", {})
        else:
            logger.opt(colors=True).debug(
                "<yellow>[UI]</yellow> Resolution path: function_call candidate ({})", selected_tool_name
            )
            if self._on_resolution:
                self._on_resolution(
                    "function_call_candidate", {"command": selected_tool_name}
                )
        await self.push_frame(frame, direction)

    def reset_session(self) -> None:
        self._previous_transcript = ""
        self._previous_transcript_at = 0.0
