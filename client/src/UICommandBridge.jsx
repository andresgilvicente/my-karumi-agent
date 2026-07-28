import { RTVIEvent } from "@pipecat-ai/client-js";
import {
  useRTVIClientEvent,
  useUICommandHandler,
  useUIEventSender,
} from "@pipecat-ai/client-react";
import { useCallback, useEffect, useRef, useState } from "react";
import catalog from "./ui-demo-catalog.json";

const SUPPORTED_COMMANDS = Object.keys(catalog.commands);
const SUPPORTED_VIEWS = Object.keys(catalog.views);
const SUPPORTED_TARGETS = Object.keys(catalog.targets);

function pageState(routeName) {
  const currentPage = routeName === "dashboard-goals" ? "dashboard" : routeName;
  const selectedTab = routeName === "dashboard" ? "overview" : routeName === "dashboard-goals" ? "goals" : null;
  const visibleElements = SUPPORTED_TARGETS.filter((target) => catalog.targets[target].view === currentPage);

  return {
    currentPage,
    selectedTab,
    visibleElements,
    availableActions: [
      ...SUPPORTED_VIEWS.map((view) => `navigate_to:${view}`),
      ...visibleElements.map((target) => `${catalog.targets[target].command}:${target}`),
    ],
  };
}

function waitForElement(elementId, timeoutMs = 2000) {
  return new Promise((resolve, reject) => {
    const startedAt = performance.now();

    function check() {
      const element = document.getElementById(elementId);
      if (element) {
        resolve(element);
      } else if (performance.now() - startedAt >= timeoutMs) {
        reject(new Error("target_not_found"));
      } else {
        window.requestAnimationFrame(check);
      }
    }

    check();
  });
}

function UICommandBridge({ routeName }) {
  const sendUIEvent = useUIEventSender();
  const [ready, setReady] = useState(false);
  const revisionRef = useRef(0);
  const processedCommandsRef = useRef(new Map());

  const sendResult = useCallback(
    (command, commandId, result) => {
      const payload = { command, commandId, ...result };
      processedCommandsRef.current.set(commandId, payload);
      sendUIEvent("ui_action_result", payload);
    },
    [sendUIEvent],
  );

  const runCommand = useCallback(
    async (command, payload, execute) => {
      const commandId = payload?.commandId;
      if (typeof commandId !== "string" || !commandId) return;

      const previousResult = processedCommandsRef.current.get(commandId);
      if (previousResult) {
        sendUIEvent("ui_action_result", previousResult);
        return;
      }

      try {
        const result = await execute(payload);
        sendResult(command, commandId, { status: "ok", ...result });
      } catch (error) {
        sendResult(command, commandId, {
          status: "error",
          code: error?.message || "action_failed",
        });
      }
    },
    [sendResult, sendUIEvent],
  );

  const handleNavigate = useCallback(
    (payload) =>
      runCommand("navigate_to", payload, async ({ view }) => {
        const destination = catalog.views[view];
        if (!destination) throw new Error("unsupported_view");
        window.location.hash = destination.hash;
        await waitForElement(destination.elementId);
        return { currentPage: view };
      }),
    [runCommand],
  );

  const handleTargetAction = useCallback(
    (command, payload) =>
      runCommand(command, payload, async ({ target }) => {
        const targetDefinition = catalog.targets[target];
        if (!targetDefinition || targetDefinition.command !== command) {
          throw new Error("unsupported_target");
        }
        const element = await waitForElement(target);
        if (command === "highlight") {
          element.classList.remove("voice-highlight");
          void element.offsetWidth;
          element.classList.add("voice-highlight");
          window.setTimeout(() => element.classList.remove("voice-highlight"), 2200);
        } else {
          const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
          element.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "center" });
          await new Promise((resolve) => window.setTimeout(resolve, reduceMotion ? 0 : 500));
        }
        return { currentPage: pageState(routeName).currentPage, target };
      }),
    [routeName, runCommand],
  );

  const handleHighlight = useCallback(
    (payload) => handleTargetAction("highlight", payload),
    [handleTargetAction],
  );
  const handleScroll = useCallback(
    (payload) => handleTargetAction("scroll_to", payload),
    [handleTargetAction],
  );

  useUICommandHandler("navigate_to", handleNavigate);
  useUICommandHandler("highlight", handleHighlight);
  useUICommandHandler("scroll_to", handleScroll);

  useRTVIClientEvent(
    RTVIEvent.BotReady,
    useCallback(() => setReady(true), []),
  );
  useRTVIClientEvent(
    RTVIEvent.Disconnected,
    useCallback(() => {
      setReady(false);
      processedCommandsRef.current.clear();
    }, []),
  );

  useEffect(() => {
    if (!ready) return;
    revisionRef.current += 1;
    sendUIEvent("ui_state", {
      uiContractVersion: catalog.uiContractVersion,
      revision: revisionRef.current,
      supportedCommands: SUPPORTED_COMMANDS,
      supportedViews: SUPPORTED_VIEWS,
      supportedTargets: SUPPORTED_TARGETS,
      ...pageState(routeName),
    });
  }, [ready, routeName, sendUIEvent]);

  return null;
}

export default UICommandBridge;
