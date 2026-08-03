/**
 * Karumi presentation overlay.
 *
 * Lives in a closed shadow root attached to `documentElement`, so the host
 * page's CSS cannot reach it and our styles cannot leak into the page. Nothing
 * here reads or writes page state — it is purely additive chrome for the demo:
 * an agent status pill, a caption bar, and a highlight ring that tracks an
 * element while the page scrolls.
 */

const HOST_ID = "karumi-overlay-host";

const CSS = `
:host { all: initial; }
.layer {
  position: fixed; inset: 0; pointer-events: none; z-index: 2147483647;
  font: 500 14px/1.4 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
}
.pill {
  position: absolute; top: 16px; right: 16px;
  display: flex; align-items: center; gap: 8px;
  padding: 8px 14px; border-radius: 999px;
  background: rgba(17, 19, 24, .88); color: #f4f4f5;
  box-shadow: 0 4px 24px rgba(0,0,0,.28);
  opacity: 0; transform: translateY(-6px); transition: opacity .18s, transform .18s;
}
.pill.on { opacity: 1; transform: none; }
.dot { width: 9px; height: 9px; border-radius: 50%; background: #71717a; flex: none; }
.pill[data-state="listening"] .dot { background: #34d399; animation: pulse 1.4s ease-in-out infinite; }
.pill[data-state="thinking"]  .dot { background: #fbbf24; animation: pulse .8s ease-in-out infinite; }
.pill[data-state="speaking"]  .dot { background: #60a5fa; animation: pulse .6s ease-in-out infinite; }
.pill[data-state="acting"]    .dot { background: #c084fc; animation: pulse .6s ease-in-out infinite; }
.pill[data-state="error"]     .dot { background: #f87171; }
@keyframes pulse { 0%,100% { opacity: 1 } 50% { opacity: .35 } }

.caption {
  position: absolute; left: 50%; bottom: 28px; transform: translate(-50%, 8px);
  max-width: min(760px, calc(100vw - 64px));
  padding: 12px 20px; border-radius: 14px;
  background: rgba(17, 19, 24, .9); color: #f4f4f5;
  box-shadow: 0 8px 32px rgba(0,0,0,.32);
  opacity: 0; transition: opacity .18s, transform .18s;
}
.caption.on { opacity: 1; transform: translate(-50%, 0); }

.ring {
  position: absolute; border-radius: 8px;
  border: 2px solid #c084fc; box-shadow: 0 0 0 4px rgba(192,132,252,.22);
  opacity: 0; transition: opacity .15s;
}
.ring.on { opacity: 1; }
`;

let refs = null;

function mount() {
  if (refs) return refs;
  if (!document.documentElement) return null;

  const host = document.createElement("div");
  host.id = HOST_ID;
  // Keep the host itself out of the layout and out of the a11y tree, so the
  // walker never reports overlay chrome as part of the page.
  host.setAttribute("aria-hidden", "true");
  host.style.cssText = "all:initial;position:fixed;top:0;left:0;width:0;height:0";

  const root = host.attachShadow({ mode: "closed" });
  const style = document.createElement("style");
  style.textContent = CSS;

  const layer = document.createElement("div");
  layer.className = "layer";
  layer.innerHTML = `
    <div class="pill" part="pill"><span class="dot"></span><span class="label"></span></div>
    <div class="caption"></div>
    <div class="ring"></div>`;

  root.append(style, layer);
  document.documentElement.appendChild(host);

  refs = {
    host,
    pill: layer.querySelector(".pill"),
    pillLabel: layer.querySelector(".label"),
    caption: layer.querySelector(".caption"),
    ring: layer.querySelector(".ring"),
  };
  return refs;
}

/** Re-attach if the page replaced documentElement's children (SPA reset). */
function ensure() {
  if (refs && refs.host.isConnected) return refs;
  refs = null;
  return mount();
}

export function setStatus(state, label) {
  const r = ensure();
  if (!r) return;
  if (!state) {
    r.pill.classList.remove("on");
    return;
  }
  r.pill.dataset.state = state;
  r.pillLabel.textContent = label || state;
  r.pill.classList.add("on");
}

export function setCaption(text) {
  const r = ensure();
  if (!r) return;
  if (!text) {
    r.caption.classList.remove("on");
    return;
  }
  r.caption.textContent = text;
  r.caption.classList.add("on");
}

let ringToken = 0;

/**
 * Draw the highlight ring around `element` for `durationMs`, following it as
 * the page scrolls or reflows. Returns the token identifying this highlight so
 * a later call can supersede it.
 */
export function highlightElement(element, durationMs = 2200) {
  const r = ensure();
  if (!r || !element) return 0;

  const token = ++ringToken;
  const deadline = performance.now() + durationMs;

  const track = () => {
    if (token !== ringToken) return;
    if (!element.isConnected || performance.now() > deadline) {
      if (token === ringToken) r.ring.classList.remove("on");
      return;
    }
    const box = element.getBoundingClientRect();
    r.ring.style.left = `${box.left - 4}px`;
    r.ring.style.top = `${box.top - 4}px`;
    r.ring.style.width = `${box.width + 8}px`;
    r.ring.style.height = `${box.height + 8}px`;
    r.ring.classList.add("on");
    requestAnimationFrame(track);
  };

  requestAnimationFrame(track);
  return token;
}

export function clearHighlight() {
  const r = ensure();
  if (!r) return;
  ringToken++;
  r.ring.classList.remove("on");
}
