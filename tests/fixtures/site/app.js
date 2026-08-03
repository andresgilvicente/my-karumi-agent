/**
 * Fixture application.
 *
 * A small client-rendered app whose job is to be *hostile in the ways real apps
 * are hostile*: it re-renders on navigation without a page load, mutates the DOM
 * on a timer, hides content below the fold, and draws a chart into a canvas that
 * carries no text of its own.
 *
 * The one thing it does not do is cooperate with the agent. There are no test
 * hooks, no data-testid attributes and no globals for the agent to lean on — it
 * has to work through the accessibility tree like it would on any real site.
 */

const views = {
  overview: {
    title: "Overview",
    render: () => `
      <section>
        <h2>Overview</h2>
        <p>Fixture data for exercising the observation layer. None of these numbers mean anything.</p>
        <div class="cards">
          ${card("Monthly revenue", "$482,190", "+12.4% vs last month", "up")}
          ${card("Active accounts", "3,914", "+218 this month", "up")}
          ${card("Churn rate", "2.1%", "-0.3 pts", "up")}
          ${card("Open incidents", "7", "+3 since yesterday", "down")}
        </div>
      </section>

      <section>
        <h2>Revenue by quarter</h2>
        <p>Drawn into a canvas, so it is deliberately invisible to a text-only reader.</p>
        <canvas id="chart" width="640" height="220" role="img"
                aria-label="Bar chart of revenue by quarter: Q1 312, Q2 358, Q3 401, Q4 482 thousand dollars"></canvas>
      </section>

      <section>
        <h2>Top regions</h2>
        <table>
          <caption>Revenue by region, current quarter</caption>
          <thead><tr><th>Region</th><th>Revenue</th><th>Growth</th></tr></thead>
          <tbody>
            <tr><td>North America</td><td>$214,800</td><td>+9.1%</td></tr>
            <tr><td>Europe</td><td>$163,410</td><td>+15.7%</td></tr>
            <tr><td>Asia Pacific</td><td>$78,240</td><td>+22.3%</td></tr>
            <tr><td>Latin America</td><td>$25,740</td><td>+4.2%</td></tr>
          </tbody>
        </table>
      </section>

      <section>
        <h2>Embedded panel</h2>
        <p>A same-origin iframe: its elements live in a different JS realm.</p>
        <iframe src="./embed.html" title="Embedded status panel"></iframe>
      </section>

      <div class="spacer" aria-hidden="true"></div>

      <section>
        <h2 id="far-below">Below the fold</h2>
        <p>This heading starts well outside the viewport, so a snapshot should tag it offscreen.</p>
        <button type="button" class="primary" id="deep-button">Deeply scrolled button</button>
      </section>`,
  },

  analytics: {
    title: "Analytics",
    render: () => `
      <section>
        <h2>Analytics</h2>
        <p>Rendered client-side. The URL never changes, so nothing that waits for a navigation will notice.</p>
        <div class="cards">
          ${card("Sessions today", "18,204", "+6.0%", "up")}
          ${card("Median duration", "4m 12s", "+18s", "up")}
          ${card("Bounce rate", "38.2%", "+1.1 pts", "down")}
        </div>
      </section>

      <section>
        <h2>Live event feed</h2>
        <p>Mutates every second. Existing rows keep their identity; new ones are added on top.</p>
        <ul id="feed" aria-label="Live event feed"></ul>
      </section>

      <section>
        <h2>Report options</h2>
        <button type="button" class="primary" id="open-report">Configure report</button>
      </section>`,
  },

  settings: {
    title: "Settings",
    render: () => `
      <section>
        <h2>Settings</h2>
        <p>Includes a password field the agent must never write into.</p>
        <div class="field">
          <label for="workspace">Workspace name</label>
          <input type="text" id="workspace" value="Fixture Workspace">
        </div>
        <div class="field">
          <label for="notify">Notification email</label>
          <input type="email" id="notify" placeholder="you@example.com">
        </div>
        <div class="field">
          <label for="secret">API password</label>
          <input type="password" id="secret" value="hunter2">
        </div>
        <div class="field">
          <label for="notes">Notes</label>
          <textarea id="notes" rows="3"></textarea>
        </div>
        <button type="submit" class="primary" id="save">Save changes</button>
      </section>`,
  },
};

function card(label, metric, delta, direction) {
  return `
    <article class="card">
      <div class="label">${label}</div>
      <div class="metric">${metric}</div>
      <div class="delta ${direction}">${delta}</div>
    </article>`;
}

let currentView = "overview";
let feedTimer = null;
let feedSeq = 0;

function drawChart() {
  const canvas = document.getElementById("chart");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const values = [312, 358, 401, 482];
  const labels = ["Q1", "Q2", "Q3", "Q4"];
  const max = 520;
  const barWidth = 90;
  const gap = 60;
  const baseline = 180;

  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.font = "12px system-ui, sans-serif";

  values.forEach((value, i) => {
    const x = 50 + i * (barWidth + gap);
    const height = (value / max) * 150;
    ctx.fillStyle = "#6ea8fe";
    ctx.fillRect(x, baseline - height, barWidth, height);
    ctx.fillStyle = "#9aa1ae";
    ctx.fillText(labels[i], x + barWidth / 2 - 8, baseline + 18);
    ctx.fillStyle = "#e6e8ec";
    ctx.fillText(String(value), x + barWidth / 2 - 12, baseline - height - 8);
  });
}

function startFeed() {
  stopFeed();
  const list = document.getElementById("feed");
  if (!list) return;

  const push = () => {
    const item = document.createElement("li");
    item.textContent = `event ${++feedSeq} — ${new Date().toLocaleTimeString()}`;
    list.prepend(item);
    while (list.children.length > 6) list.lastElementChild.remove();
  };

  push();
  feedTimer = setInterval(push, 1000);
}

function stopFeed() {
  if (feedTimer) clearInterval(feedTimer);
  feedTimer = null;
}

function openReportModal() {
  const root = document.getElementById("modal-root");
  root.innerHTML = `
    <dialog class="modal" id="report-modal" aria-label="Configure report">
      <h3>Configure report</h3>
      <p>A real dialog element, so it lands in the accessibility tree only while open.</p>
      <div class="field">
        <label for="report-name">Report name</label>
        <input type="text" id="report-name" placeholder="Quarterly summary">
      </div>
      <button type="button" class="primary" id="close-report">Close</button>
    </dialog>`;
  const dialog = document.getElementById("report-modal");
  dialog.showModal();
  document.getElementById("close-report").addEventListener("click", () => {
    dialog.close();
    root.innerHTML = "";
  });
}

function render(view) {
  currentView = view;
  document.getElementById("view").innerHTML = views[view].render();
  document.title = `${views[view].title} — Fixture Console`;

  for (const link of document.querySelectorAll(".navlink")) {
    if (link.dataset.view === view) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }

  stopFeed();
  if (view === "overview") drawChart();
  if (view === "analytics") {
    startFeed();
    document.getElementById("open-report").addEventListener("click", openReportModal);
  }
}

document.addEventListener("click", (event) => {
  const link = event.target.closest(".navlink");
  if (link) {
    render(link.dataset.view);
    return;
  }
  if (event.target.id === "delete-everything") {
    // Never actually destructive — but it is named like it is, which is what
    // the policy layer keys off.
    alert("Nothing was deleted. This control exists to be refused.");
  }
});

// A heartbeat that touches the DOM without changing structure. This is the
// mutation pressure that would invalidate a naive snapshot-diffing scheme.
setInterval(() => {
  const badge = document.querySelector(".topbar h1");
  if (badge) badge.dataset.tick = String(Date.now());
}, 500);

render(currentView);
