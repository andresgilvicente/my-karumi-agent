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
    ):
        self._catalog = catalog
        self._send_command = send_command
        self._get_ui_state = get_ui_state

    async def execute(self, command: str, payload: dict) -> dict:
        if command == "navigate_to":
            return await self._navigate(payload.get("view"))
        if command in {"highlight", "scroll_to"}:
            return await self._target(command, payload.get("target"))
        return {"status": "error", "code": "unsupported_action"}

    async def _navigate(self, view: str | None) -> dict:
        if view not in self._catalog.views:
            return {"status": "error", "code": "unsupported_view"}

        navigation = await self._send_command("navigate_to", {"view": view})
        if navigation.get("status") != "confirmed":
            return navigation

        default_action = self._catalog.views[view]["defaultAction"]
        # Navigation's confirmed destination makes its catalog default action valid even before
        # the asynchronous ui_state publication for the new route arrives.
        focus = await self._send_command(default_action["command"], {"target": default_action["target"]})
        if focus.get("status") != "confirmed":
            return {
                "status": "error",
                "code": "focus_action_failed",
                "navigation": navigation,
                "focus": focus,
            }

        return {
            "status": "confirmed",
            "action": "navigate_to",
            "view": view,
            "navigation": navigation,
            "focus": focus,
        }

    async def _target(self, command: str, target: str | None) -> dict:
        definition = self._catalog.targets.get(target)
        if definition is None or definition["command"] != command:
            return {"status": "error", "code": "unsupported_target"}

        current_ui_state = self._get_ui_state()
        if (
            current_ui_state is None
            or current_ui_state.get("currentPage") != definition["view"]
            or target not in current_ui_state.get("visibleElements", [])
        ):
            return {"status": "error", "code": "target_not_visible"}

        return await self._send_command(command, {"target": target})

    def response_for(self, command: str, payload: dict, result: dict) -> str:
        if result.get("status") != "confirmed":
            return self._catalog.data["errorMessage"]
        if command == "navigate_to":
            return self._catalog.views[payload["view"]]["successMessage"]
        return self._catalog.targets[payload["target"]]["successMessage"]

    def build_tools(
        self,
        context: LLMContext,
        on_resolution: Callable[[str, dict], None] | None = None,
    ) -> dict[str, FunctionSchema]:
        async def finish_tool_call(params: FunctionCallParams, result: dict) -> None:
            context.set_tools([])
            context.set_tool_choice("auto")
            await params.result_callback(result)

        async def handle_navigation(params: FunctionCallParams) -> None:
            logger.opt(colors=True).debug("<yellow>[UI]</yellow> Resolution path: function_call")
            if on_resolution:
                on_resolution("function_call", {"command": "navigate_to"})
            await finish_tool_call(
                params,
                await self.execute("navigate_to", {"view": params.arguments.get("view")}),
            )

        async def handle_highlight(params: FunctionCallParams) -> None:
            logger.opt(colors=True).debug("<yellow>[UI]</yellow> Resolution path: function_call")
            if on_resolution:
                on_resolution("function_call", {"command": "highlight"})
            await finish_tool_call(
                params,
                await self.execute("highlight", {"target": params.arguments.get("target")}),
            )

        async def handle_scroll(params: FunctionCallParams) -> None:
            logger.opt(colors=True).debug("<yellow>[UI]</yellow> Resolution path: function_call")
            if on_resolution:
                on_resolution("function_call", {"command": "scroll_to"})
            await finish_tool_call(
                params,
                await self.execute("scroll_to", {"target": params.arguments.get("target")}),
            )

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
    ):
        super().__init__()
        self._context = context
        self._catalog = catalog
        self._tools = tools
        self._execute_action = execute_action
        self._response_for = response_for
        self._on_resolution = on_resolution
        self._previous_transcript = ""
        self._previous_transcript_at = 0.0

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if not isinstance(frame, TranscriptionFrame):
            await self.push_frame(frame, direction)
            return

        text = frame.text.casefold()
        now = time.monotonic()
        previous_text = (
            self._previous_transcript if now - self._previous_transcript_at <= 6.0 else ""
        )
        intent = resolve_ui_intent(text, self._catalog, previous_text)
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
            result = await self._execute_action(command, payload)
            response = self._response_for(command, payload, result)
            logger.opt(colors=True).debug("<yellow>[UI]</yellow> Deterministic result: {}", result)
            self._previous_transcript = ""
            self._previous_transcript_at = 0.0
            await self.push_frame(record_deterministic_turn(self._context, frame.text, response), direction)
            return

        selected_tool_name = fallback_tool_name(text, self._catalog)
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
