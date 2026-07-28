import { RTVIEvent } from "@pipecat-ai/client-js";
import {
  useRTVIClientEvent,
  useUICommandHandler,
  useUIEventSender,
} from "@pipecat-ai/client-react";
import { useCallback, useEffect, useRef, useState } from "react";

const VIEW_TARGETS = {
  dashboard: { route: "dashboard", elementId: "dashboard-title" },
  analytics: { route: "analytics", elementId: "analytics-title" },
  team: { route: "team", elementId: "team-title" },
};

const HIGHLIGHT_TARGETS = new Set(["total-revenue-card", "traffic-overview"]);
const SCROLL_TARGETS = new Set(["team-member-list"]);

function pageState(routeName) {
  const currentPage = routeName === "dashboard-goals" ? "dashboard" : routeName;
  const selectedTab = routeName === "dashboard" ? "overview" : routeName === "dashboard-goals" ? "goals" : null;
  const visibleElements = {
    dashboard: ["total-revenue-card"],
    analytics: ["traffic-overview"],
    team: ["team-member-list"],
  }[currentPage] ?? [];

  return {
    currentPage,
    selectedTab,
    visibleElements,
    availableActions: [
      "navigate_to:dashboard",
      "navigate_to:analytics",
      "navigate_to:team",
      ...visibleElements.map((target) =>
        target === "team-member-list" ? `scroll_to:${target}` : `highlight:${target}`,
      ),
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
    (commandId, result) => {
      const payload = { commandId, ...result };
      processedCommandsRef.current.set(commandId, payload);
      sendUIEvent("ui_action_result", payload);
    },
    [sendUIEvent],
  );

  const runCommand = useCallback(
    async (payload, execute) => {
      const commandId = payload?.commandId;
      if (typeof commandId !== "string" || !commandId) return;

      const previousResult = processedCommandsRef.current.get(commandId);
      if (previousResult) {
        sendUIEvent("ui_action_result", previousResult);
        return;
      }

      try {
        const result = await execute(payload);
        sendResult(commandId, { status: "ok", ...result });
      } catch (error) {
        sendResult(commandId, {
          status: "error",
          code: error?.message || "action_failed",
        });
      }
    },
    [sendResult, sendUIEvent],
  );

  const handleNavigate = useCallback(
    (payload) =>
      runCommand(payload, async ({ view }) => {
        const destination = VIEW_TARGETS[view];
        if (!destination) throw new Error("unsupported_view");
        window.location.hash = destination.route;
        await waitForElement(destination.elementId);
        return { currentPage: view };
      }),
    [runCommand],
  );

  const handleHighlight = useCallback(
    (payload) =>
      runCommand(payload, async ({ target }) => {
        if (!HIGHLIGHT_TARGETS.has(target)) throw new Error("unsupported_target");
        const element = await waitForElement(target);
        element.classList.remove("voice-highlight");
        void element.offsetWidth;
        element.classList.add("voice-highlight");
        window.setTimeout(() => element.classList.remove("voice-highlight"), 2200);
        return { currentPage: pageState(routeName).currentPage, target };
      }),
    [routeName, runCommand],
  );

  const handleScroll = useCallback(
    (payload) =>
      runCommand(payload, async ({ target }) => {
        if (!SCROLL_TARGETS.has(target)) throw new Error("unsupported_target");
        const element = await waitForElement(target);
        const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
        element.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "center" });
        await new Promise((resolve) => window.setTimeout(resolve, reduceMotion ? 0 : 500));
        return { currentPage: pageState(routeName).currentPage, target };
      }),
    [routeName, runCommand],
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
      revision: revisionRef.current,
      ...pageState(routeName),
    });
  }, [ready, routeName, sendUIEvent]);

  return null;
}

export default UICommandBridge;
