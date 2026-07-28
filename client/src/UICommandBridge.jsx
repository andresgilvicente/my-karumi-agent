import { RTVIEvent } from "@pipecat-ai/client-js";
import {
  useRTVIClientEvent,
  useUICommandHandler,
  useUIEventSender,
} from "@pipecat-ai/client-react";
import { useCallback, useEffect, useRef, useState } from "react";
import catalog from "./ui-demo-catalog.json";

const SUPPORTED_COMMANDS = Object.entries(catalog.commands)
  .filter(([, definition]) => definition.clientCommand !== false)
  .map(([command]) => command);
const SUPPORTED_VIEWS = Object.keys(catalog.views);
const SUPPORTED_TARGETS = Object.keys(catalog.targets);
const highlightTimers = new WeakMap();

function pageState(routeName) {
  const currentPage = routeName === "dashboard-goals" ? "dashboard" : routeName;
  const selectedTab = routeName === "dashboard" ? "overview" : routeName === "dashboard-goals" ? "goals" : null;
  const visibleElements = SUPPORTED_TARGETS.filter(
    (target) =>
      catalog.targets[target].view === currentPage && routeName !== "dashboard-goals",
  );

  return {
    currentPage,
    selectedTab,
    visibleElements,
    availableActions: [
      ...SUPPORTED_VIEWS.map((view) => `navigate_to:${view}`),
      ...visibleElements.flatMap((target) =>
        catalog.targets[target].allowedCommands.map((command) => `${command}:${target}`),
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

function waitForCondition(condition, timeoutMs = 2000) {
  return new Promise((resolve, reject) => {
    const startedAt = performance.now();

    function check() {
      if (condition()) {
        resolve();
      } else if (performance.now() - startedAt >= timeoutMs) {
        reject(new Error("ui_update_timeout"));
      } else {
        window.requestAnimationFrame(check);
      }
    }

    check();
  });
}

function prefersReducedMotion() {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

async function centerElement(element) {
  if (!element.isConnected) throw new Error("target_not_found");
  const reduceMotion = prefersReducedMotion();
  const initialBounds = element.getBoundingClientRect();
  const targetDocumentCenter = initialBounds.top + window.scrollY + initialBounds.height / 2;
  const scrollingElement = document.scrollingElement ?? document.documentElement;
  const maxScroll = Math.max(0, scrollingElement.scrollHeight - window.innerHeight);
  const targetScroll = Math.min(
    maxScroll,
    Math.max(0, targetDocumentCenter - window.innerHeight / 2),
  );
  const needsScroll = Math.abs(window.scrollY - targetScroll) > 2;

  if (reduceMotion || !needsScroll) {
    window.scrollTo({ top: targetScroll, behavior: "auto" });
    await new Promise((resolve) => window.requestAnimationFrame(resolve));
  } else {
    await new Promise((resolve) => {
      let timeoutId;
      const finish = () => {
        window.clearTimeout(timeoutId);
        document.removeEventListener("scrollend", finish);
        window.removeEventListener("scrollend", finish);
        resolve();
      };
      timeoutId = window.setTimeout(finish, 900);
      document.addEventListener("scrollend", finish, { once: true });
      window.addEventListener("scrollend", finish, { once: true });
      window.scrollTo({ top: targetScroll, behavior: "smooth" });
    });
  }
  if (!element.isConnected) throw new Error("target_not_found");
  const bounds = element.getBoundingClientRect();
  const targetCenter = bounds.top + bounds.height / 2;
  const currentDocumentCenter = targetCenter + window.scrollY;
  const currentMaxScroll = Math.max(
    0,
    (document.scrollingElement ?? document.documentElement).scrollHeight - window.innerHeight,
  );
  const expectedScroll = Math.min(
    currentMaxScroll,
    Math.max(0, currentDocumentCenter - window.innerHeight / 2),
  );
  const expectedCenter = currentDocumentCenter - expectedScroll;
  const centerTolerance = Math.max(24, Math.min(48, window.innerHeight * 0.06));
  if (
    Math.abs(window.scrollY - expectedScroll) > centerTolerance
    || Math.abs(targetCenter - expectedCenter) > centerTolerance
  ) {
    throw new Error("target_not_centered");
  }
}

async function highlightElement(element) {
  if (!element.isConnected) throw new Error("target_not_found");
  element.classList.remove("voice-highlight");
  void element.offsetWidth;
  element.classList.add("voice-highlight");
  await new Promise((resolve) => window.requestAnimationFrame(resolve));
  if (!element.isConnected) throw new Error("target_not_found");
  const previousTimer = highlightTimers.get(element);
  if (previousTimer) window.clearTimeout(previousTimer);
  highlightTimers.set(
    element,
    window.setTimeout(() => {
      element.classList.remove("voice-highlight");
      highlightTimers.delete(element);
    }, 2200),
  );
}

function UICommandBridge({
  routeName,
  teamMembers,
  filterTeamMembers,
  teamQuery,
  setTeamQuery,
}) {
  const sendUIEvent = useUIEventSender();
  const [ready, setReady] = useState(false);
  const revisionRef = useRef(0);
  const processedCommandsRef = useRef(new Map());
  const sessionEpochRef = useRef(0);
  const uiSessionIdRef = useRef(null);

  const sendResult = useCallback(
    (command, commandId, result) => {
      const payload = { command, commandId, ...result };
      sendUIEvent("ui_action_result", payload);
      return payload;
    },
    [sendUIEvent],
  );

  const runCommand = useCallback(
    async (command, payload, execute) => {
      const commandId = payload?.commandId;
      if (typeof commandId !== "string" || !commandId) return;
      if (payload.uiSessionId !== uiSessionIdRef.current) return;
      const requestKey = JSON.stringify({ command, ...payload, commandId: undefined });

      const previous = processedCommandsRef.current.get(commandId);
      if (previous) {
        if (previous.requestKey !== requestKey) {
          sendResult(command, commandId, {
            uiSessionId: payload.uiSessionId,
            status: "error",
            code: "command_id_reused",
          });
          return;
        }
        const previousResult = previous.result ?? (await previous.promise);
        if (previous.sessionEpoch === sessionEpochRef.current) {
          sendUIEvent("ui_action_result", previousResult);
        }
        return;
      }

      const sessionEpoch = sessionEpochRef.current;
      const promise = (async () => {
        try {
          const result = await execute(payload);
          return {
            command,
            commandId,
            uiSessionId: payload.uiSessionId,
            status: "ok",
            ...result,
          };
        } catch (error) {
          return {
            command,
            commandId,
            uiSessionId: payload.uiSessionId,
            status: "error",
            code: error?.message || "action_failed",
          };
        }
      })();
      const entry = { requestKey, promise, result: null, sessionEpoch };
      processedCommandsRef.current.set(commandId, entry);
      const result = await promise;
      entry.result = result;
      if (sessionEpoch === sessionEpochRef.current) sendUIEvent("ui_action_result", result);
    },
    [sendResult, sendUIEvent],
  );

  const handleNavigate = useCallback(
    (payload) =>
      runCommand("navigate_to", payload, async ({ view }) => {
        const destination = catalog.views[view];
        if (!destination) throw new Error("unsupported_view");
        window.location.hash = destination.hash;
        await waitForCondition(() => window.location.hash === destination.hash);
        const destinationElement = await waitForElement(destination.elementId);
        window.scrollTo({ top: 0, behavior: "instant" });
        await new Promise((resolve) =>
          window.requestAnimationFrame(() => window.requestAnimationFrame(resolve)),
        );
        if (
          !destinationElement.isConnected
          || window.location.hash !== destination.hash
          || window.scrollY > 2
        ) {
          throw new Error("navigation_not_settled");
        }
        return { currentPage: view };
      }),
    [runCommand],
  );

  const handleTargetAction = useCallback(
    (command, payload) =>
      runCommand(command, payload, async ({ target }) => {
        const targetDefinition = catalog.targets[target];
        if (!targetDefinition || !targetDefinition.allowedCommands.includes(command)) {
          throw new Error("unsupported_target");
        }
        const targetHash = catalog.views[targetDefinition.view].hash;
        if (window.location.hash !== targetHash) throw new Error("target_not_visible");
        const element = await waitForElement(target);
        if (command === "highlight") {
          await highlightElement(element);
        } else {
          await centerElement(element);
        }
        if (!element.isConnected || window.location.hash !== targetHash) {
          throw new Error("target_not_visible");
        }
        return { currentPage: targetDefinition.view, target };
      }),
    [runCommand],
  );

  const handleTeamSearch = useCallback(
    (payload) =>
      runCommand("search_team_member", payload, async ({ query }) => {
        const normalizedQuery = typeof query === "string" ? query.trim().toLowerCase() : "";
        if (!normalizedQuery) throw new Error("empty_query");

        const input = await waitForElement("team-search");
        setTeamQuery(query.trim());
        await waitForCondition(() => input.value.trim().toLowerCase() === normalizedQuery);

        const matches = filterTeamMembers(teamMembers, query);
        const focusElement = matches.length
          ? await waitForElement(`team-member-${matches[0].id}`)
          : await waitForElement("team-member-list");
        await centerElement(focusElement);
        if (matches.length) await highlightElement(focusElement);

        return {
          currentPage: "team",
          query: query.trim(),
          matchCount: matches.length,
          matchedNames: matches.map((member) => member.name),
        };
      }),
    [filterTeamMembers, runCommand, setTeamQuery, teamMembers],
  );

  const handleClearSearch = useCallback(
    (payload) =>
      runCommand("clear_team_search", payload, async () => {
        const input = await waitForElement("team-search");
        setTeamQuery("");
        await waitForCondition(() => input.value === "");
        const list = await waitForElement("team-member-list");
        await centerElement(list);
        return { currentPage: "team", cleared: true };
      }),
    [runCommand, setTeamQuery],
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
  useUICommandHandler("search_team_member", handleTeamSearch);
  useUICommandHandler("clear_team_search", handleClearSearch);

  useRTVIClientEvent(
    RTVIEvent.BotReady,
    useCallback(() => {
      uiSessionIdRef.current ??= crypto.randomUUID();
      setReady(true);
    }, []),
  );
  useRTVIClientEvent(
    RTVIEvent.Disconnected,
    useCallback(() => {
      setReady(false);
      sessionEpochRef.current += 1;
      uiSessionIdRef.current = null;
      processedCommandsRef.current.clear();
    }, []),
  );

  useEffect(() => {
    if (!ready) return;
    revisionRef.current += 1;
    sendUIEvent("ui_state", {
      uiSessionId: uiSessionIdRef.current,
      uiContractVersion: catalog.uiContractVersion,
      revision: revisionRef.current,
      supportedCommands: SUPPORTED_COMMANDS,
      supportedViews: SUPPORTED_VIEWS,
      supportedTargets: SUPPORTED_TARGETS,
      teamSearchQuery: teamQuery,
      ...pageState(routeName),
    });
  }, [ready, routeName, sendUIEvent, teamQuery]);

  return null;
}

export default UICommandBridge;
