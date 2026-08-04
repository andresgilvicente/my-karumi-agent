"""Shared test fixtures."""

from __future__ import annotations

import pytest

from karumi_agent.domain.snapshot import PageSnapshot, Viewport, parse_aria_snapshot

# Captured verbatim from https://metric-master-suite505.apps.rebolt.ai/ on
# 2026-08-04 via page.aria_snapshot(mode="ai", boxes=True). Kept as a literal so
# the parser is tested against real Playwright output rather than our idea of it.
# Note what it shows: the site's landing route is a sign-in wall.
REAL_LOGIN_SNAPSHOT = """\
- generic [ref=e2] [box=0,0,1440,900]:
  - generic [ref=e4] [box=496,274,448,352]:
    - generic [ref=e5] [box=497,275,446,106]:
      - heading "Welcome back" [level=3] [ref=e6] [box=521,299,398,32]
      - paragraph [ref=e7] [box=521,337,398,20]: Sign in to access the chat application
    - generic [ref=e8] [box=497,381,446,244]:
      - generic [ref=e9] [box=521,381,398,180]:
        - generic [ref=e10] [box=521,381,398,56]:
          - text: Email
          - textbox "Email" [ref=e12] [box=521,405,398,32]:
            - /placeholder: Enter your email
        - generic [ref=e13] [box=521,453,398,56]:
          - text: Password
          - generic [ref=e14] [box=521,477,398,32]:
            - textbox "Password" [ref=e15] [box=521,477,398,32]:
              - /placeholder: Enter your password
            - button [ref=e16] [box=891,485,16,16]
        - button "Sign In" [disabled] [box=521,525,398,36]
      - button "Don't have an account? Sign up" [ref=e21] [box=608,579,223,20]
  - region "Notifications alt+T" [box=0,900,1440,0]
"""

# A dashboard-shaped snapshot exercising the parts the login page does not:
# navigation, headings, a table, links with /url, offscreen content, a modal.
DASHBOARD_SNAPSHOT = """\
- generic [ref=e1] [box=0,0,1440,900]:
  - navigation [ref=e2] [box=0,0,240,900]:
    - link "Dashboard" [ref=e3] [cursor=pointer] [box=8,64,224,40]:
      - /url: /dashboard
    - link "Analytics" [ref=e4] [cursor=pointer] [box=8,108,224,40]:
      - /url: /analytics
    - link "Team" [ref=e5] [cursor=pointer] [box=8,152,224,40]:
      - /url: /team
  - main [ref=e10] [box=240,0,1200,900]:
    - heading "Overview" [level=1] [ref=e11] [box=272,32,400,40]
    - generic [ref=e12] [box=272,96,300,120]:
      - heading "Revenue" [level=3] [ref=e13] [box=288,112,268,24]
      - generic [ref=e14] [box=288,140,268,40]: $128,430
    - button "Export Report" [ref=e20] [cursor=pointer] [box=1100,32,120,36]
    - button "Delete workspace" [ref=e21] [cursor=pointer] [box=1100,80,140,36]
    - table [ref=e30] [box=272,240,1100,300]:
      - row [ref=e31] [box=272,240,1100,40]: Name Role Status
      - row [ref=e32] [box=272,280,1100,40]: Ana Diaz Admin Active
    - paragraph [ref=e40] [box=272,1400,1100,60]: Footer note far below the fold
"""


@pytest.fixture
def login_snapshot() -> PageSnapshot:
    """The real sign-in wall from the demo target."""
    return parse_aria_snapshot(
        REAL_LOGIN_SNAPSHOT,
        url="https://metric-master-suite505.apps.rebolt.ai/",
        title="Rebolt Generated App",
        viewport=Viewport(1440, 900),
        snapshot_id="snap-login",
        captured_at=1_000_000.0,
    )


@pytest.fixture
def dashboard_snapshot() -> PageSnapshot:
    """A dashboard-shaped page with navigation, a table and offscreen content."""
    return parse_aria_snapshot(
        DASHBOARD_SNAPSHOT,
        url="https://example.test/dashboard",
        title="Overview",
        viewport=Viewport(1440, 900),
        snapshot_id="snap-dash",
        captured_at=1_000_000.0,
    )
