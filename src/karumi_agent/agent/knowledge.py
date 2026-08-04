"""Progressive site learning.

Starts the moment a URL is known -- while the agent is still saying hello -- and
keeps running in the background as the conversation goes on. Nothing here is ever
on the hot path of a turn: a slow fetch must never delay an answer.

Two paths, chosen per page:

* **Plain HTTP + text extraction** for server-rendered pages. Verified: both
  `rebolt.ai` and `karumi.ai` ship their full copy in the initial HTML, so a
  browser is pure overhead for them.
* **The live browser snapshot** for the page actually being demonstrated, which
  is client-rendered and behind the operator's session.

Everything is bounded -- pages, depth, wall-clock, and bytes per page -- because
this runs against sites we do not control and must not be able to run away.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from karumi_agent.browser.guards import origin_of, registrable_suffix
from karumi_agent.logging import get_logger

log = get_logger("knowledge")

_MAX_BYTES_PER_PAGE = 600_000
_REQUEST_TIMEOUT_S = 10.0
_SKIP_EXTENSIONS = (
    ".pdf",
    ".zip",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".webp",
    ".mp4",
    ".mp3",
    ".css",
    ".js",
    ".ico",
    ".woff",
    ".woff2",
    ".xml",
)


@dataclass(slots=True)
class PageKnowledge:
    """What we learned about one URL."""

    url: str
    title: str = ""
    description: str = ""
    headings: list[str] = field(default_factory=list)
    summary: str = ""
    #: Where the claim came from, so nothing is asserted without a source.
    source: str = "http"

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "headings": self.headings[:8],
            "summary": self.summary[:400],
            "source": self.source,
        }


@dataclass(slots=True)
class SiteKnowledge:
    """The incremental map of a site."""

    root_url: str
    pages: dict[str, PageKnowledge] = field(default_factory=dict)
    product_name: str = ""
    tagline: str = ""
    started_at: float = field(default_factory=time.time)
    finished: bool = False

    @property
    def page_count(self) -> int:
        return len(self.pages)

    def add(self, page: PageKnowledge) -> None:
        self.pages[page.url] = page
        if not self.product_name and page.title:
            # The landing page's title is the best cheap guess at the product
            # name; the agent still verifies it against what is on screen.
            self.product_name = _clean_title(page.title)
        if not self.tagline and page.description:
            self.tagline = page.description

    def as_prompt_block(self) -> str:
        """Render what we know, for the model's context.

        Kept short and explicitly sourced. This is background knowledge, not a
        licence to make claims the page does not support.
        """
        if not self.pages:
            return ""
        lines = ["<site_knowledge>"]
        if self.product_name:
            lines.append(f"Product (from the site's own pages): {self.product_name}")
        if self.tagline:
            lines.append(f"How the site describes itself: {self.tagline}")
        lines.append(f"Pages read so far: {self.page_count}")
        for page in list(self.pages.values())[:6]:
            heading_text = "; ".join(page.headings[:4])
            if heading_text:
                lines.append(f"- {page.url}: {heading_text}")
        lines.append("</site_knowledge>")
        return "\n".join(lines)

    def as_dict(self) -> dict[str, Any]:
        return {
            "root_url": self.root_url,
            "product_name": self.product_name,
            "tagline": self.tagline,
            "page_count": self.page_count,
            "finished": self.finished,
            "pages": [p.as_dict() for p in self.pages.values()],
        }


class _TextExtractor(HTMLParser):
    """Pull title, meta description, headings and body text out of HTML.

    Hand-rolled rather than pulling in a readability dependency: we need four
    specific things, and `html.parser` is in the standard library and cannot be
    tricked into executing anything.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.description = ""
        self.headings: list[str] = []
        self._chunks: list[str] = []
        self._skip_depth = 0
        self._in_title = False
        self._heading_tag: str | None = None
        self._heading_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript", "svg"}:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag in {"h1", "h2", "h3"}:
            self._heading_tag = tag
            self._heading_parts = []
        elif tag == "meta":
            attributes = dict(attrs)
            name = (attributes.get("name") or attributes.get("property") or "").lower()
            if name in {"description", "og:description"} and not self.description:
                self.description = (attributes.get("content") or "").strip()[:300]

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"}:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag == "title":
            self._in_title = False
        elif tag == self._heading_tag:
            text = _collapse(" ".join(self._heading_parts))
            if text and len(self.headings) < 24:
                self.headings.append(text)
            self._heading_tag = None
            self._heading_parts = []

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data
        if self._heading_tag:
            self._heading_parts.append(data)
        if len(self._chunks) < 4000:
            self._chunks.append(data)

    @property
    def text(self) -> str:
        return _collapse(" ".join(self._chunks))


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _clean_title(title: str) -> str:
    """Strip the " | Site name" suffix conventions."""
    # Typographic dashes are intentional: sites really do use them.
    for separator in ("|", "—", "–", " - "):  # noqa: RUF001
        if separator in title:
            head = title.split(separator)[0].strip()
            if len(head) >= 3:
                return head
    return title.strip()


def _is_followable(url: str, root: str) -> bool:
    """Same-site, http(s), not an asset."""
    if not url.startswith(("http://", "https://")):
        return False
    if url.lower().endswith(_SKIP_EXTENSIONS):
        return False
    host = urlparse(url).hostname or ""
    root_host = urlparse(root).hostname or ""
    if not host or not root_host:
        return False
    return registrable_suffix(host) == registrable_suffix(root_host)


_LINK_RE = re.compile(r'href=["\']([^"\'#]+)', re.IGNORECASE)


class KnowledgeWorker:
    """Learns about a site in the background, within hard budgets."""

    def __init__(
        self,
        *,
        max_pages: int = 12,
        max_depth: int = 2,
        budget_s: float = 120.0,
    ) -> None:
        self._max_pages = max_pages
        self._max_depth = max_depth
        self._budget_s = budget_s
        self.knowledge: SiteKnowledge | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self, url: str) -> None:
        """Begin learning. Returns immediately.

        Called before the greeting finishes, which is the point: by the time the
        operator asks their first question, several pages are already read.
        """
        self.knowledge = SiteKnowledge(root_url=url)
        self._task = asyncio.create_task(self._run(url))

    async def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _run(self, root: str) -> None:
        assert self.knowledge is not None
        deadline = time.perf_counter() + self._budget_s
        queue: list[tuple[str, int]] = [(root, 0)]
        seen: set[str] = set()

        headers = {
            # Identify honestly. A site owner reading their logs should be able
            # to tell what this was.
            "User-Agent": "KarumiAgent/0.1 (local demo agent; +https://github.com/)",
            "Accept": "text/html,application/xhtml+xml",
        }
        try:
            async with httpx.AsyncClient(
                timeout=_REQUEST_TIMEOUT_S, follow_redirects=True, headers=headers
            ) as client:
                while queue and len(self.knowledge.pages) < self._max_pages:
                    if time.perf_counter() > deadline:
                        log.info("knowledge budget reached", pages=self.knowledge.page_count)
                        break
                    url, depth = queue.pop(0)
                    normalised = url.split("#")[0].rstrip("/") or url
                    if normalised in seen:
                        continue
                    seen.add(normalised)

                    page, links = await self._fetch(client, url)
                    if page is None:
                        continue
                    self.knowledge.add(page)
                    log.debug("learned page", url=url, pages=self.knowledge.page_count)

                    if depth < self._max_depth:
                        for link in links[:20]:
                            if link.split("#")[0].rstrip("/") not in seen:
                                queue.append((link, depth + 1))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # background work, never fatal
            log.warning(f"knowledge worker stopped: {type(exc).__name__}: {exc}")
        finally:
            if self.knowledge is not None:
                self.knowledge.finished = True
                log.info("knowledge complete", pages=self.knowledge.page_count)

    async def _fetch(
        self, client: httpx.AsyncClient, url: str
    ) -> tuple[PageKnowledge | None, list[str]]:
        try:
            response = await client.get(url)
        except httpx.HTTPError as exc:
            log.debug(f"fetch failed {url}: {exc}")
            return None, []

        if response.status_code >= 400:
            return None, []
        if "html" not in response.headers.get("content-type", ""):
            return None, []

        body = response.text[:_MAX_BYTES_PER_PAGE]
        parser = _TextExtractor()
        with contextlib.suppress(Exception):
            parser.feed(body)

        text = parser.text
        page = PageKnowledge(
            url=str(response.url),
            title=_collapse(parser.title),
            description=parser.description,
            headings=parser.headings,
            summary=text[:600],
            source="http",
        )
        # A client-rendered shell has almost no text; recording it as knowledge
        # would be recording nothing. The live snapshot covers those pages.
        if len(text) < 200 and not parser.headings:
            page.summary = ""

        links = [urljoin(str(response.url), match) for match in _LINK_RE.findall(body)]
        followable = [
            link for link in dict.fromkeys(links) if _is_followable(link, origin_of(url) or url)
        ]
        return page, followable
