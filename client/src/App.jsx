import { useEffect, useRef, useState } from "react";
import "./App.css";
import UICommandBridge from "./UICommandBridge.jsx";
import VoiceControls from "./VoiceControls.jsx";

const ROUTES = {
  home: { view: "home", title: "Home" },
  dashboard: { view: "dashboard", title: "Dashboard", panel: "overview" },
  "dashboard-goals": { view: "dashboard", title: "Dashboard", panel: "goals" },
  analytics: { view: "analytics", title: "Analytics" },
  team: { view: "team", title: "Team" },
};

const NAVIGATION = [
  { route: "home", label: "Home", index: "01" },
  { route: "dashboard", label: "Dashboard", index: "02" },
  { route: "analytics", label: "Analytics", index: "03" },
  { route: "team", label: "Team", index: "04" },
];

const TEAM_MEMBERS = [
  {
    name: "Olivia Chen",
    email: "olivia@saasapp.io",
    role: "Admin",
    team: "Leadership",
    initials: "OC",
    avatar: "avatar-warm",
  },
  {
    name: "Ethan Miller",
    email: "ethan@saasapp.io",
    role: "Product Manager",
    team: "Product",
    initials: "EM",
    avatar: "avatar-blue",
  },
  {
    name: "Sophia Patel",
    email: "sophia@saasapp.io",
    role: "Analyst",
    team: "Growth",
    initials: "SP",
    avatar: "avatar-green",
  },
  {
    name: "Liam Brown",
    email: "liam@saasapp.io",
    role: "Engineer",
    team: "Engineering",
    initials: "LB",
    avatar: "avatar-purple",
  },
  {
    name: "Ava Martin",
    email: "ava@saasapp.io",
    role: "Designer",
    team: "Product",
    initials: "AM",
    avatar: "avatar-pink",
  },
  {
    name: "Noah Williams",
    email: "noah@saasapp.io",
    role: "Admin",
    team: "Engineering",
    initials: "NW",
    avatar: "avatar-orange",
  },
  {
    name: "Mia Davis",
    email: "mia@saasapp.io",
    role: "Marketing Lead",
    team: "Growth",
    initials: "MD",
    avatar: "avatar-cyan",
  },
  {
    name: "Lucas Anderson",
    email: "lucas@saasapp.io",
    role: "Support Lead",
    team: "Customer Success",
    initials: "LA",
    avatar: "avatar-sand",
  },
];

function getRouteFromHash() {
  const route = window.location.hash.slice(1);
  return ROUTES[route] ? route : "home";
}

function App() {
  const [routeName, setRouteName] = useState(getRouteFromHash);
  const route = ROUTES[routeName];

  useEffect(() => {
    function updateRoute() {
      const nextRoute = window.location.hash.slice(1);

      if (!ROUTES[nextRoute]) {
        window.history.replaceState(null, "", "#home");
        setRouteName("home");
        return;
      }

      setRouteName(nextRoute);
    }

    updateRoute();
    window.addEventListener("hashchange", updateRoute);
    return () => window.removeEventListener("hashchange", updateRoute);
  }, []);

  useEffect(() => {
    document.title = `${route.title} | Karumi Agent`;
    window.scrollTo({ top: 0, behavior: "auto" });
  }, [route]);

  function skipToContent(event) {
    event.preventDefault();
    const mainContent = document.getElementById("main-content");
    mainContent?.focus();
    mainContent?.scrollIntoView({ block: "start" });
  }

  return (
    <>
      <a className="skip-link" href="#main-content" onClick={skipToContent}>
        Skip to main content
      </a>

      <UICommandBridge routeName={routeName} />

      <div className="app-shell">
        <Sidebar activeView={route.view} />

        <div className="workspace">
          <Topbar title={route.title} />

          <main id="main-content" tabIndex="-1">
            {route.view === "home" && <HomeView />}
            {route.view === "dashboard" && <DashboardView panel={route.panel} />}
            {route.view === "analytics" && <AnalyticsView />}
            {route.view === "team" && <TeamView />}
          </main>
        </div>
      </div>
    </>
  );
}

function Sidebar({ activeView }) {
  return (
    <aside className="sidebar">
      <a className="brand" href="#home" aria-label="Metric Master Suite home">
        <span className="brand-mark" aria-hidden="true">
          M
        </span>
        <span>Metric Master</span>
      </a>

      <nav className="primary-navigation" aria-label="Primary navigation">
        {NAVIGATION.map((item) => (
          <a
            key={item.route}
            className="nav-link"
            href={`#${item.route}`}
            aria-current={activeView === item.route ? "page" : undefined}
          >
            <span className="nav-index" aria-hidden="true">
              {item.index}
            </span>
            <span>{item.label}</span>
          </a>
        ))}
      </nav>

      <div className="sidebar-footer">
        <span className="environment-dot" aria-hidden="true"></span>
        Local demo workspace
      </div>
    </aside>
  );
}

function Topbar({ title }) {
  return (
    <header className="topbar">
      <div>
        <p className="topbar-label">Workspace</p>
        <h1 id="page-title">{title}</h1>
      </div>

      <div className="topbar-actions">
        <VoiceControls />
        <div className="profile-summary" aria-label="Current user">
          <span className="avatar avatar-dark" aria-hidden="true">
            AM
          </span>
          <span className="profile-copy">
            <strong>Alex Morgan</strong>
            <small>Product Lead</small>
          </span>
        </div>
      </div>
    </header>
  );
}

function HomeView() {
  const featuresRef = useRef(null);
  const miniChart = ["38%", "52%", "46%", "68%", "61%", "79%", "92%"];

  function showFeatures() {
    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    featuresRef.current?.scrollIntoView({
      behavior: reduceMotion ? "auto" : "smooth",
      block: "start",
    });
  }

  return (
    <section className="view home-view" aria-labelledby="home-title">
      <div className="hero-panel">
        <div className="hero-copy">
          <p className="eyebrow">Metric Master Suite</p>
          <h2 id="home-title">Build Better with SaaSApp</h2>
          <p className="hero-description">
            Turn complex product signals into clear decisions with one focused workspace for
            performance, goals, analytics, and teams.
          </p>
          <div className="button-row">
            <a className="button button-primary" href="#dashboard">
              Get Started
            </a>
            <button className="button button-secondary" type="button" onClick={showFeatures}>
              Learn More
            </button>
          </div>
        </div>

        <div className="hero-summary" aria-label="Workspace summary">
          <div className="summary-heading">
            <div>
              <span>Weekly performance</span>
              <strong>Healthy</strong>
            </div>
            <span className="status-pill">+12.4%</span>
          </div>
          <div
            className="mini-chart"
            role="img"
            aria-label="Weekly performance trend increased overall across seven days."
          >
            {miniChart.map((height, index) => (
              <span key={index} style={{ "--bar-height": height }}></span>
            ))}
          </div>
          <div className="summary-stats">
            <div>
              <span>Revenue</span>
              <strong>$124.5k</strong>
            </div>
            <div>
              <span>Users</span>
              <strong>8,429</strong>
            </div>
          </div>
        </div>
      </div>

      <div className="home-features" id="home-features" ref={featuresRef}>
        <div className="section-heading compact-heading">
          <div>
            <p className="eyebrow">One connected view</p>
            <h3>Everything needed to keep momentum</h3>
          </div>
        </div>

        <div className="feature-grid">
          <FeatureCard
            number="01"
            title="Track performance"
            description="Keep revenue, adoption, conversion, and growth visible at a glance."
          />
          <FeatureCard
            number="02"
            title="Stay on target"
            description="Follow the goals that matter and identify where attention is needed."
          />
          <FeatureCard
            number="03"
            title="Understand traffic"
            description="See how visitors arrive and how effectively they move through the product."
          />
        </div>
      </div>
    </section>
  );
}

function FeatureCard({ number, title, description }) {
  return (
    <article className="feature-card">
      <span className="feature-number">{number}</span>
      <h4>{title}</h4>
      <p>{description}</p>
    </article>
  );
}

function DashboardView({ panel }) {
  const tabs = [
    { panel: "overview", route: "dashboard", label: "Overview", id: "overview-tab" },
    { panel: "goals", route: "dashboard-goals", label: "Goals", id: "goals-tab" },
  ];

  function handleTabKeyDown(event, index) {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;

    event.preventDefault();
    const direction = event.key === "ArrowRight" ? 1 : -1;
    const nextTab = tabs[(index + direction + tabs.length) % tabs.length];
    window.location.hash = nextTab.route;
    window.requestAnimationFrame(() => document.getElementById(nextTab.id)?.focus());
  }

  return (
    <section className="view" aria-labelledby="dashboard-title">
      <SectionHeading
        eyebrow="Performance center"
        title="Dashboard"
        description="Monitor your most important business signals and active targets."
        aside="Last 30 days"
        titleId="dashboard-title"
      />

      <div className="tabs" role="tablist" aria-label="Dashboard sections">
        {tabs.map((tab, index) => {
          const isActive = tab.panel === panel;
          return (
            <a
              key={tab.panel}
              id={tab.id}
              className="tab"
              href={`#${tab.route}`}
              role="tab"
              aria-controls={`dashboard-${tab.panel}`}
              aria-selected={isActive}
              tabIndex={isActive ? 0 : -1}
              onKeyDown={(event) => handleTabKeyDown(event, index)}
            >
              {tab.label}
            </a>
          );
        })}
      </div>

      {panel === "overview" ? <DashboardOverview /> : <DashboardGoals />}
    </section>
  );
}

function DashboardOverview() {
  return (
    <div id="dashboard-overview" role="tabpanel" aria-labelledby="overview-tab">
      <div className="metric-grid">
        <MetricCard
          id="total-revenue-card"
          label="Total Revenue"
          trend="+12.5%"
          value="$124,500"
          description="Compared with $110,650 last month"
        />
        <MetricCard
          id="active-users-card"
          label="Active Users"
          trend="+8.2%"
          value="8,429"
          description="639 more active users this month"
        />
        <MetricCard
          id="conversion-rate-card"
          label="Conversion Rate"
          trend="+0.4%"
          value="3.24%"
          description="Up from 2.84% in the previous period"
        />
        <MetricCard
          id="growth-rate-card"
          label="Growth Rate"
          trend="+5.1%"
          value="18.6%"
          description="Strongest growth in the last quarter"
        />
      </div>

      <article className="content-card performance-card" id="performance-overview">
        <CardHeading
          title="Performance Overview"
          description="Monthly revenue against the current operating plan."
          legend="Revenue"
        />
        <BarChart
          ariaLabel="Revenue increased from January to June"
          points={[
            ["Jan", "48%"],
            ["Feb", "56%"],
            ["Mar", "51%"],
            ["Apr", "69%"],
            ["May", "78%"],
            ["Jun", "92%"],
          ]}
        />
      </article>
    </div>
  );
}

function DashboardGoals() {
  return (
    <div id="dashboard-goals" role="tabpanel" aria-labelledby="goals-tab">
      <div className="goals-intro">
        <div>
          <h3>Active Goals</h3>
          <p>Three company-wide targets are currently in progress.</p>
        </div>
        <span className="status-pill neutral-pill">Q3 2026</span>
      </div>

      <div className="goal-list">
        <GoalCard
          id="revenue-target-goal"
          category="Revenue"
          title="Revenue Target"
          description="Reach $120,000 in monthly recurring revenue."
          progressLabel="$84,000 of $120,000"
          progress={70}
          remaining="47 days remaining"
        />
        <GoalCard
          id="user-acquisition-goal"
          category="Acquisition"
          title="User Acquisition"
          description="Grow the active customer base to 10,000 users."
          progressLabel="6,200 of 10,000 users"
          progress={62}
          remaining="89 days remaining"
        />
        <GoalCard
          id="project-completion-goal"
          category="Delivery"
          title="Project Completion"
          description="Complete the 24 initiatives in the product roadmap."
          progressLabel="18 of 24 initiatives"
          progress={75}
          remaining="21 days remaining"
        />
      </div>
    </div>
  );
}

function GoalCard({ id, category, title, description, progressLabel, progress, remaining }) {
  return (
    <article className="goal-card" id={id}>
      <div className="goal-main">
        <span className="goal-category">{category}</span>
        <h4>{title}</h4>
        <p>{description}</p>
      </div>
      <div className="goal-progress">
        <div className="progress-heading">
          <span>{progressLabel}</span>
          <strong>{progress}%</strong>
        </div>
        <progress value={progress} max="100" aria-label={`${title} is ${progress} percent complete`}>
          {progress}%
        </progress>
        <small>{remaining}</small>
      </div>
    </article>
  );
}

function AnalyticsView() {
  return (
    <section className="view" aria-labelledby="analytics-title">
      <SectionHeading
        eyebrow="Audience insights"
        title="Analytics"
        description="Understand how visitors discover and engage with the product."
        aside="Jun 28 - Jul 27"
        titleId="analytics-title"
      />

      <div className="metric-grid">
        <MetricCard
          id="page-views-card"
          label="Page Views"
          trend="+16.2%"
          value="248,900"
          description="34,700 more views than last period"
        />
        <MetricCard
          id="click-through-rate-card"
          label="Click-through Rate"
          trend="+0.7%"
          value="4.8%"
          description="Above the 4.1% operating benchmark"
        />
        <MetricCard
          id="session-duration-card"
          label="Avg. Session Duration"
          trend="+18 sec"
          value="3m 42s"
          description="Visitors are spending more time in product"
        />
        <MetricCard
          id="analytics-conversion-card"
          label="Conversion Rate"
          trend="+0.4%"
          value="3.24%"
          description="8,064 converted sessions this period"
        />
      </div>

      <div className="analytics-grid">
        <article className="content-card traffic-card" id="traffic-overview">
          <CardHeading
            title="Traffic Overview"
            description="Daily visitors across the last seven days."
            legend="Visitors"
          />
          <BarChart
            className="traffic-chart"
            ariaLabel="Traffic peaked on Friday"
            points={[
              ["Mon", "55%"],
              ["Tue", "66%"],
              ["Wed", "62%"],
              ["Thu", "75%"],
              ["Fri", "94%"],
              ["Sat", "71%"],
              ["Sun", "68%"],
            ]}
          />
        </article>

        <article className="content-card sources-card" id="traffic-sources">
          <CardHeading title="Traffic Sources" description="Where engaged visitors originate." />
          <div className="source-list">
            <SourceRow label="Direct" value="42%" dotClass="source-dark" />
            <SourceRow label="Organic Search" value="31%" dotClass="source-gray" />
            <SourceRow label="Referral" value="18%" dotClass="source-light" />
            <SourceRow label="Social" value="9%" dotClass="source-outline" />
          </div>
        </article>
      </div>
    </section>
  );
}

function TeamView() {
  const [query, setQuery] = useState("");
  const normalizedQuery = query.trim().toLowerCase();
  const visibleMembers = TEAM_MEMBERS.filter((member) =>
    `${member.name} ${member.email} ${member.role} ${member.team}`
      .toLowerCase()
      .includes(normalizedQuery),
  );

  return (
    <section className="view" aria-labelledby="team-title">
      <div className="section-heading team-heading">
        <div>
          <p className="eyebrow">People and roles</p>
          <h2 id="team-title">Team</h2>
          <p>Find collaborators and understand how the workspace is organized.</p>
        </div>
        <div className="search-control">
          <label htmlFor="team-search">Search members</label>
          <input
            id="team-search"
            type="search"
            placeholder="Name, email, role, or team"
            autoComplete="off"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
        </div>
      </div>

      <div className="team-summary-grid">
        <SummaryCard label="Total Members" value="8" detail="Across 5 teams" />
        <SummaryCard label="Pending Invites" value="2" detail="Sent this week" />
        <SummaryCard label="Admins" value="2" detail="Full workspace access" />
      </div>

      <article className="content-card member-card" id="team-member-list">
        <div className="card-heading member-heading">
          <div>
            <h3>Members</h3>
            <p>People with access to this workspace.</p>
          </div>
          <output id="team-results-status" htmlFor="team-search" aria-live="polite">
            {normalizedQuery
              ? `Showing ${visibleMembers.length} of ${TEAM_MEMBERS.length} members`
              : `Showing ${TEAM_MEMBERS.length} members`}
          </output>
        </div>

        {visibleMembers.length > 0 ? (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th scope="col">Member</th>
                  <th scope="col">Role</th>
                  <th scope="col">Team</th>
                  <th scope="col">Status</th>
                </tr>
              </thead>
              <tbody>
                {visibleMembers.map((member) => (
                  <tr key={member.email}>
                    <td>
                      <div className="member">
                        <span className={`avatar ${member.avatar}`} aria-hidden="true">
                          {member.initials}
                        </span>
                        <div>
                          <strong>{member.name}</strong>
                          <small>{member.email}</small>
                        </div>
                      </div>
                    </td>
                    <td>{member.role}</td>
                    <td>{member.team}</td>
                    <td>
                      <span className="member-status">Active</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="empty-state">No members match your search.</p>
        )}
      </article>
    </section>
  );
}

function SectionHeading({ eyebrow, title, description, aside, titleId }) {
  return (
    <div className="section-heading">
      <div>
        <p className="eyebrow">{eyebrow}</p>
        <h2 id={titleId}>{title}</h2>
        <p>{description}</p>
      </div>
      <span className="period-label">{aside}</span>
    </div>
  );
}

function MetricCard({ id, label, trend, value, description }) {
  return (
    <article className="metric-card" id={id}>
      <div className="metric-heading">
        <span>{label}</span>
        <span className="trend trend-up">{trend}</span>
      </div>
      <strong>{value}</strong>
      <p>{description}</p>
    </article>
  );
}

function CardHeading({ title, description, legend }) {
  return (
    <div className="card-heading">
      <div>
        <h3>{title}</h3>
        <p>{description}</p>
      </div>
      {legend && (
        <span className="legend">
          <span aria-hidden="true"></span>
          {legend}
        </span>
      )}
    </div>
  );
}

function BarChart({ points, ariaLabel, className = "" }) {
  return (
    <div className={`bar-chart ${className}`.trim()} role="img" aria-label={ariaLabel}>
      {points.map(([label, height]) => (
        <div className="chart-column" key={label}>
          <span style={{ "--bar-height": height }}></span>
          <small>{label}</small>
        </div>
      ))}
    </div>
  );
}

function SourceRow({ label, value, dotClass }) {
  return (
    <div className="source-row">
      <div>
        <span className={`source-dot ${dotClass}`} aria-hidden="true"></span>
        <strong>{label}</strong>
      </div>
      <span>{value}</span>
    </div>
  );
}

function SummaryCard({ label, value, detail }) {
  return (
    <article className="summary-card">
      <span>{label}</span>
      <strong>{value}</strong>
      <small>{detail}</small>
    </article>
  );
}

export default App;
