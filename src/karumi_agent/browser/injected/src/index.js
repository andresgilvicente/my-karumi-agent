/**
 * Karumi page-side agent.
 *
 * Injected into every frame of the target page via Playwright's
 * `addInitScript`. The target site is never modified — this bundle lives in the
 * browser realm we control and only reads the page.
 *
 * Observation is delegated wholesale to Pipecat's own accessibility walker
 * (`@pipecat-ai/client-js`), which is the same code the official React UI
 * Worker client uses. That gives us byte-identical `A11ySnapshot` payloads —
 * including WeakMap-backed refs that stay stable across snapshots while an
 * element remains mounted, `[offscreen]` viewport tagging, and text-selection
 * serialization — without reimplementing any of it.
 *
 * Two things deliberately do *not* happen here:
 *
 * - **Ref namespacing.** The walker allocates refs per JS realm, so two frames
 *   will both hand out `e7`. Qualifying them in-page would need each frame to
 *   learn its own id from the host, which races against the first snapshot.
 *   The host knows which frame a message came from, so it does the namespacing
 *   on ingest and strips it again on resolve. This file only ever sees bare
 *   walker refs.
 * - **Actuation.** `resolve()` hands a live Element back to Playwright, which
 *   performs real, trusted input with its own actionability checks. Synthetic
 *   JS clicks would bypass exactly the behaviour a demo needs to be honest
 *   about.
 */

import {
  A11ySnapshotStreamer,
  findElementByRef,
  findRefForElement,
  serializeSelection,
  snapshotDocument,
} from "@pipecat-ai/client-js";

import {
  clearHighlight,
  highlightElement,
  setCaption,
  setStatus,
} from "./overlay.js";

/** Name of the Playwright binding that receives streamed snapshots. */
const EMIT_BINDING = "__karumiEmitSnapshot";

let streamer = null;
let sequence = 0;

function pageInfo() {
  return {
    url: location.href,
    title: document.title,
    scroll_y: Math.round(window.scrollY),
    scroll_height: Math.round(document.documentElement.scrollHeight),
    viewport_height: Math.round(window.innerHeight),
    ready_state: document.readyState,
  };
}

/**
 * Capture a snapshot and stamp it with what the host needs to reason about
 * ordering. `captured_at` comes from the walker; `seq` is a monotonic per-realm
 * counter that disambiguates two snapshots taken in the same millisecond.
 */
function capture(options = {}) {
  const snapshot = snapshotDocument(undefined, {
    trackViewport: options.trackViewport !== false,
  });
  snapshot.seq = ++sequence;
  snapshot.page = pageInfo();
  return snapshot;
}

function emit(snapshot) {
  const sink = window[EMIT_BINDING];
  if (typeof sink !== "function") return;
  try {
    // The binding returns a promise; a rejection here (host tearing down
    // mid-navigation) must not break the streamer's event loop.
    Promise.resolve(sink(snapshot)).catch(() => {});
  } catch {
    /* host went away */
  }
}

/** Resolve a walker ref to a live Element in this frame, or null. */
function resolve(ref) {
  const element = findElementByRef(ref);
  if (!element || !element.isConnected) return null;
  return element;
}

/**
 * A compact, verification-oriented description of an element.
 *
 * Deliberately small: this is evidence for "did the action land?", not context
 * for the model. Geometry and interactability come back so the host can tell a
 * real state change from a no-op.
 */
function describe(ref) {
  const element = resolve(ref);
  if (!element) return null;

  const box = element.getBoundingClientRect();
  const style = getComputedStyle(element);
  const tag = element.tagName.toLowerCase();
  const isField = tag === "input" || tag === "textarea" || tag === "select";

  return {
    ref,
    tag,
    role: element.getAttribute("role") || null,
    type: element.getAttribute("type") || null,
    name: (element.getAttribute("aria-label") || element.textContent || "")
      .trim()
      .slice(0, 120),
    // Password values are never read back, mirroring the walker's own policy.
    value:
      isField && element.type !== "password" ? String(element.value ?? "") : null,
    checked: typeof element.checked === "boolean" ? element.checked : null,
    disabled:
      element.hasAttribute("disabled") ||
      element.getAttribute("aria-disabled") === "true",
    expanded: element.getAttribute("aria-expanded"),
    selected: element.getAttribute("aria-selected"),
    focused: document.activeElement === element,
    visible:
      box.width > 0 &&
      box.height > 0 &&
      style.visibility !== "hidden" &&
      style.display !== "none" &&
      Number(style.opacity) !== 0,
    in_viewport:
      box.bottom > 0 &&
      box.right > 0 &&
      box.top < window.innerHeight &&
      box.left < window.innerWidth,
    rect: {
      x: Math.round(box.x),
      y: Math.round(box.y),
      width: Math.round(box.width),
      height: Math.round(box.height),
    },
  };
}

/** Select an element's text (or a sub-range), for deixis during a demo. */
function selectText(ref, startOffset, endOffset) {
  const element = resolve(ref);
  if (!element) return false;

  if (element.tagName === "INPUT" || element.tagName === "TEXTAREA") {
    element.focus();
    element.setSelectionRange(startOffset ?? 0, endOffset ?? element.value.length);
    return true;
  }

  const selection = window.getSelection();
  if (!selection) return false;
  const range = document.createRange();
  range.selectNodeContents(element);
  selection.removeAllRanges();
  selection.addRange(range);
  return true;
}

const api = {
  snapshot: capture,

  /**
   * Begin pushing snapshots to the host on DOM mutation, focus change,
   * scroll-end, resize, and visibility change.
   */
  startStream(options = {}) {
    if (streamer) return false;
    streamer = new A11ySnapshotStreamer(() => emit(capture(options)), {
      debounceMs: options.debounceMs ?? 250,
      trackViewport: options.trackViewport !== false,
    });
    streamer.start();
    return true;
  },

  stopStream() {
    if (!streamer) return false;
    streamer.stop();
    streamer = null;
    return true;
  },

  /** Force an immediate push, bypassing the debounce (used after actions). */
  pushSnapshot(options = {}) {
    const snapshot = capture(options);
    emit(snapshot);
    return snapshot;
  },

  resolve,
  describe,
  selectText,
  pageInfo,
  selection: serializeSelection,
  refForElement: findRefForElement,

  highlight(ref, durationMs) {
    const element = resolve(ref);
    if (!element) return false;
    highlightElement(element, durationMs);
    return true;
  },

  clearHighlight,
  setStatus,
  setCaption,
};

// Idempotent: `addInitScript` runs once per document, but a page that
// re-executes bundles (HMR, some SPA shells) must not clobber a live streamer.
if (!window.__karumi) {
  Object.defineProperty(window, "__karumi", {
    value: api,
    configurable: true,
    enumerable: false,
    writable: false,
  });
}
