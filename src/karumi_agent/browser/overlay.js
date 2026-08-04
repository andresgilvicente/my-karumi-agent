/*
 * Karumi glass pane: a status overlay for a page we do not control.
 *
 * Injected through Playwright's add_init_script, which runs in the main world via
 * CDP's Page.addScriptToEvaluateOnNewDocument. That matters: the script never
 * becomes a <script> element, so a strict Content-Security-Policy cannot block
 * it. The same reasoning drives every technique below.
 *
 *   <karumi-glass>            a novel tag name, so no site CSS selector matches it
 *   popover="manual"          renders in the top layer, above every z-index and
 *                             even a native <dialog>
 *   attachShadow({closed})    site CSS cannot style us; site JS cannot reach in
 *   adoptedStyleSheets        constructable stylesheets are CSSOM, not elements,
 *                             so `style-src` does not apply. Using addStyleTag or
 *                             an inline <style> here WOULD be blocked by CSP.
 *   pointer-events: none      we never steal a click. It also means Playwright's
 *                             aria snapshot never assigns us a ref, because
 *                             mode="ai" only refs elements that receive pointer
 *                             events -- so the overlay is invisible to the model.
 *
 * This layer is presentation only. It carries no product knowledge, and nothing
 * the agent decides depends on it. The authoritative record of whether an action
 * was verified is the JSONL run log, never what is drawn here.
 */
(() => {
  "use strict";

  const KEY = "__KARUMI_KEY__";
  if (window[KEY]) return;

  const STATES = {
    idle: { label: "Ready", color: "#64748b" },
    listening: { label: "Listening", color: "#22c55e" },
    thinking: { label: "Thinking", color: "#a855f7" },
    speaking: { label: "Speaking", color: "#3b82f6" },
    learning: { label: "Learning the site", color: "#06b6d4" },
    executing: { label: "Doing it", color: "#f59e0b" },
    verified: { label: "Verified", color: "#10b981" },
    waiting_login: { label: "Waiting for you to sign in", color: "#eab308" },
    error: { label: "Error", color: "#ef4444" },
  };

  const CSS = `
    :host { all: initial; }
    .chip {
      position: fixed; top: 16px; right: 16px;
      display: flex; align-items: center; gap: 8px;
      padding: 8px 14px; border-radius: 999px;
      font: 500 13px/1.2 ui-sans-serif, system-ui, -apple-system, sans-serif;
      color: #f8fafc; background: rgba(15, 23, 42, 0.92);
      box-shadow: 0 4px 16px rgba(0,0,0,0.28);
      transition: opacity 140ms ease;
    }
    .dot { width: 8px; height: 8px; border-radius: 50%; background: #64748b; }
    .dot.pulse { animation: pulse 1.15s ease-in-out infinite; }
    @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.35; } }
    .caption {
      position: fixed; left: 50%; bottom: 28px; transform: translateX(-50%);
      max-width: min(1000px, 82vw);
      padding: 12px 20px; border-radius: 12px;
      font: 400 17px/1.45 ui-sans-serif, system-ui, -apple-system, sans-serif;
      color: #f8fafc; background: rgba(15, 23, 42, 0.90);
      box-shadow: 0 6px 24px rgba(0,0,0,0.32);
      text-align: center; text-wrap: balance;
      opacity: 0; transition: opacity 160ms ease;
    }
    .caption.show { opacity: 1; }
    .box {
      position: fixed; border-radius: 6px; box-sizing: border-box;
      border: 2px solid #f59e0b;
      box-shadow: 0 0 0 3px rgba(245,158,11,0.22), 0 0 0 9999px rgba(2,6,23,0.34);
      transition: opacity 120ms ease;
    }
    .box.verified { border-color: #10b981; box-shadow: 0 0 0 3px rgba(16,185,129,0.22), 0 0 0 9999px rgba(2,6,23,0.30); }
    .box-label {
      position: fixed; padding: 3px 8px; border-radius: 6px;
      font: 500 12px/1.2 ui-sans-serif, system-ui, sans-serif;
      color: #0f172a; background: #f59e0b; white-space: nowrap;
    }
    .box-label.verified { background: #10b981; color: #f0fdf4; }
  `;

  let host = null;
  let shadow = null;
  let chipEl = null;
  let dotEl = null;
  let labelEl = null;
  let captionEl = null;
  /** @type {{selector: string|null, el: Element|null, box: HTMLElement, label: HTMLElement|null, verified: boolean}[]} */
  let highlights = [];
  let raf = 0;
  let currentState = "idle";

  const ACTIVE_STATES = new Set(["listening", "thinking", "speaking", "learning", "executing"]);

  function build() {
    host = document.createElement("karumi-glass");
    host.setAttribute("popover", "manual");
    const s = host.style;
    s.inset = "0";
    s.width = "100%";
    s.height = "100%";
    s.maxWidth = "none";
    s.maxHeight = "none";
    s.padding = "0";
    s.margin = "0";
    s.border = "none";
    s.overflow = "visible";
    s.background = "transparent";
    s.pointerEvents = "none";
    s.zIndex = "2147483647";

    shadow = host.attachShadow({ mode: "closed" });
    try {
      const sheet = new CSSStyleSheet();
      sheet.replaceSync(CSS);
      shadow.adoptedStyleSheets = [sheet];
    } catch (_) {
      // Older engines without constructable stylesheets. A <style> element is
      // CSP-visible, but by then we have no better option.
      const style = document.createElement("style");
      style.textContent = CSS;
      shadow.appendChild(style);
    }

    chipEl = document.createElement("div");
    chipEl.className = "chip";
    dotEl = document.createElement("span");
    dotEl.className = "dot";
    labelEl = document.createElement("span");
    chipEl.appendChild(dotEl);
    chipEl.appendChild(labelEl);

    captionEl = document.createElement("div");
    captionEl.className = "caption";

    shadow.appendChild(chipEl);
    shadow.appendChild(captionEl);
    applyState(currentState);
  }

  function install() {
    if (!document.documentElement) return;
    if (!host) build();
    // Re-append if the page removed us, or if it appended something after us.
    if (!document.documentElement.contains(host) || host.nextElementSibling) {
      document.documentElement.appendChild(host);
    }
    bringToFront();
  }

  function bringToFront() {
    try {
      host.hidePopover();
      host.showPopover();
    } catch (_) {
      /* popover unsupported, or not connected yet; the z-index fallback stands */
    }
  }

  function applyState(name) {
    const state = STATES[name] || STATES.idle;
    currentState = name;
    if (!dotEl) return;
    dotEl.style.background = state.color;
    dotEl.classList.toggle("pulse", ACTIVE_STATES.has(name));
    labelEl.textContent = state.label;
    chipEl.style.opacity = name === "idle" ? "0.55" : "1";
  }

  function reposition() {
    for (const h of highlights) {
      const el = h.el;
      if (!el || !el.isConnected) {
        h.box.style.opacity = "0";
        if (h.label) h.label.style.opacity = "0";
        continue;
      }
      const r = el.getBoundingClientRect();
      h.box.style.opacity = "1";
      h.box.style.left = r.x + "px";
      h.box.style.top = r.y + "px";
      h.box.style.width = r.width + "px";
      h.box.style.height = r.height + "px";
      if (h.label) {
        h.label.style.opacity = "1";
        h.label.style.left = r.x + "px";
        h.label.style.top = Math.max(0, r.y - 22) + "px";
      }
    }
    raf = highlights.length ? requestAnimationFrame(reposition) : 0;
  }

  function clearHighlights() {
    for (const h of highlights) {
      h.box.remove();
      if (h.label) h.label.remove();
    }
    highlights = [];
    if (raf) {
      cancelAnimationFrame(raf);
      raf = 0;
    }
  }

  const api = {
    setState(name) {
      install();
      applyState(name);
    },
    setCaption(text) {
      install();
      if (!captionEl) return;
      captionEl.textContent = text || "";
      captionEl.classList.toggle("show", Boolean(text));
    },
    /**
     * Highlight an element. `element` is passed by Playwright as an element
     * handle, so we never re-query the DOM from a selector the page could shadow.
     */
    highlight(element, label, verified) {
      install();
      if (!element) return false;
      const box = document.createElement("div");
      box.className = "box" + (verified ? " verified" : "");
      shadow.appendChild(box);
      let labelEl2 = null;
      if (label) {
        labelEl2 = document.createElement("div");
        labelEl2.className = "box-label" + (verified ? " verified" : "");
        labelEl2.textContent = label;
        shadow.appendChild(labelEl2);
      }
      highlights.push({ el: element, box, label: labelEl2, verified: Boolean(verified) });
      if (!raf) raf = requestAnimationFrame(reposition);
      return true;
    },
    clearHighlights,
    reinstall: install,
    state: () => currentState,
  };

  Object.defineProperty(window, KEY, { value: api, enumerable: false, configurable: false });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", install, { once: true });
  } else {
    install();
  }
  // A SPA that replaces documentElement's children would drop us; this is the
  // same cheap containment check Playwright's own highlight uses, no observer.
  setInterval(install, 1000);
})();
