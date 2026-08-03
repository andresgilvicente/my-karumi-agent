"""What the injected bundle does to a page we do not own.

`build.mjs` asserts statically that no capability-bearing code (WebRTC, media
capture, sockets) is in the bundle. These tests make the runtime claim: loading
it into a third-party page adds exactly one global, opens nothing, and leaves
the document's own content alone.

This matters because we ship the whole of `@pipecat-ai/client-js` — its Parcel
build cannot be tree-shaken — so "the dead code really is dead" needs to be
demonstrated rather than assumed.
"""

from __future__ import annotations

import pytest

from karumi_agent.browser.playwright_adapter import PlaywrightBrowserAdapter

pytestmark = pytest.mark.browser


async def test_adds_no_globals_beyond_its_own(adapter: PlaywrightBrowserAdapter) -> None:
    """Anything beyond `__karumi` would be namespace pollution in someone's app.

    Playwright's own driver hooks (`__playwright*`, `__pw*`) are exempt: they
    come from automating the browser at all, not from the bundle, and they are
    present whether or not we inject anything.
    """
    extras = await adapter.page.evaluate(
        """
        () => {
          const ours = new Set(["__karumi", "__karumiEmitSnapshot"]);
          const driver = /^__(playwright|pw)/;
          return Object.getOwnPropertyNames(window)
            .filter((k) => k.startsWith("__") && !ours.has(k) && !driver.test(k));
        }
        """
    )
    assert extras == []


async def test_exposes_the_expected_api(adapter: PlaywrightBrowserAdapter) -> None:
    surface = await adapter.page.evaluate(
        "() => Object.keys(window.__karumi).filter(k => typeof window.__karumi[k] === 'function')"
    )
    for name in (
        "snapshot",
        "startStream",
        "stopStream",
        "resolve",
        "describe",
        "highlight",
        "selectText",
        "setStatus",
        "setCaption",
    ):
        assert name in surface, name


async def test_opens_no_connections(adapter: PlaywrightBrowserAdapter) -> None:
    """A bundle injected into a customer's page must not phone anywhere."""
    requests: list[str] = []
    adapter.page.on("request", lambda request: requests.append(request.url))

    await adapter.observe(force=True)
    await adapter.page.wait_for_timeout(600)

    external = [url for url in requests if not url.startswith(adapter.page.url.rsplit("/", 1)[0])]
    assert external == [], external


async def test_overlay_is_hidden_from_the_accessibility_tree(
    adapter: PlaywrightBrowserAdapter,
) -> None:
    """Our own chrome must not show up as part of the page being demoed."""
    await adapter.set_status("listening", "Listening")
    await adapter.set_caption("this caption is ours, not the page's")

    snapshot = await adapter.observe(force=True)
    names = {node.name for node in snapshot.root.walk() if node.name}

    assert "this caption is ours, not the page's" not in names
    assert not any("karumi" in (name or "").lower() for name in names)


async def test_does_not_disturb_the_page_content(adapter: PlaywrightBrowserAdapter) -> None:
    """The site is read, never rewritten."""
    heading = await adapter.page.evaluate("() => document.querySelector('h1').textContent")
    nav_items = await adapter.page.evaluate("() => document.querySelectorAll('nav button').length")

    assert heading == "Fixture Console"
    assert nav_items == 3
