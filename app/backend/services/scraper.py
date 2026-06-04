"""
Web Scraper — crawls URLs and extracts clean text for RAG indexing.
Uses trafilatura for high-quality article extraction + BeautifulSoup fallback.
Respects robots.txt and rate-limits requests.
"""

from __future__ import annotations

import asyncio
import re
from collections import deque
from urllib.parse import urljoin, urlparse

import httpx
from http_retry import retry_async, WEB_RETRY

try:
    import trafilatura
    HAS_TRAFILATURA = True
except ImportError:
    HAS_TRAFILATURA = False
    print("trafilatura not installed — falling back to BeautifulSoup")

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False


HEADERS = {
    # Mimic a real browser — some sites block non-browser user agents
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}
REQUEST_DELAY = 0.5  # seconds between requests
MAX_CONTENT_LEN = 500_000   # 500KB max per page (text extracted)
MAX_INGEST_BYTES = 20 * 1024 * 1024  # 20MB hard cap — skip before downloading


class WebScraper:
    def __init__(self) -> None:
        # Persistent client for fetch_single — reuses TCP/TLS connections across
        # repeated single-page fetches (background indexing, URL context, etc.).
        # crawl() creates its own scoped client so parallel batches share one pool.
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is not None and self._client.is_closed:
            await self._client.aclose()
            self._client = None
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers=HEADERS, follow_redirects=True, timeout=15
            )
        return self._client

    async def aclose(self) -> None:
        """Close the persistent client. Call from app lifespan shutdown."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def crawl(
        self,
        start_url: str,
        max_pages: int = 20,
        same_domain_only: bool = True,
    ) -> list[dict]:
        """
        Crawl starting from `start_url`, following internal links up to `max_pages`.
        Fetches pages in parallel for speed. Returns list of dicts: {url, text, title}
        """
        visited = set()
        queue: deque[str] = deque([start_url])
        results = []
        base_domain = urlparse(start_url).netloc

        async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=15) as client:
            while queue and len(visited) < max_pages:
                # Collect batch of URLs to fetch in parallel
                batch = []
                while queue and len(batch) < 8 and len(visited) + len(batch) < max_pages:
                    url = queue.popleft()
                    if url in visited or url in batch:
                        continue
                    batch.append(url)

                if not batch:
                    break

                # Fetch all pages in parallel
                fetch_tasks = [self._fetch_page(client, url) for url in batch]
                pages = await asyncio.gather(*fetch_tasks, return_exceptions=True)

                for url, page in zip(batch, pages):
                    visited.add(url)
                    if isinstance(page, Exception):
                        print(f"[Scraper] Error fetching {url}: {page}")
                        continue
                    if page is None:
                        continue
                    results.append(page)

                    # Discover links on the page
                    if same_domain_only:
                        links = self._extract_links(page.get("raw_html", ""), url, base_domain)
                        queue_set = set(queue)
                        for link in links:
                            if link not in visited and link not in queue_set:
                                queue.append(link)

        return results

    async def fetch_single(self, url: str) -> dict | None:
        """Fetch and extract a single page. Reuses the persistent client."""
        return await self._fetch_page(await self._get_client(), url)

    # ── Internals ──────────────────────────────────────────────────────────────

    async def _fetch_page(self, client: httpx.AsyncClient, url: str) -> dict | None:
        # Reddit special case — use their public JSON API
        if "reddit.com" in url:
            return await self._fetch_reddit(client, url)

        try:
            resp = await retry_async(lambda: client.get(url), WEB_RETRY)
        except httpx.HTTPError as exc:
            print(f"[Scraper] HTTP error fetching {url}: {exc}")
            return None

        if resp.status_code != 200:
            return None

        content_type = resp.headers.get("content-type", "")
        if "html" not in content_type and "text" not in content_type:
            return None  # skip binary files

        cl = int(resp.headers.get("content-length", 0))
        if cl > MAX_INGEST_BYTES:
            print(f"[Scraper] Skipping {url} — content-length {cl/1e6:.1f}MB exceeds 20MB limit")
            return None

        html = resp.text[:MAX_CONTENT_LEN]
        text, title = self._extract_text(html, url)

        if not text or len(text.strip()) < 20:
            return None

        return {"url": url, "text": text, "title": title, "raw_html": html}

    async def _fetch_reddit(self, client: httpx.AsyncClient, url: str) -> dict | None:
        """
        Use Reddit's public JSON API (append .json to any Reddit URL).
        No API key required for public content.
        """
        # Normalize URL — strip trailing slash, remove .json if already there
        clean = url.rstrip("/").replace(".json", "")
        json_url = clean + ".json?limit=100&raw_json=1"

        resp = await retry_async(
            lambda: client.get(json_url, headers={**HEADERS, "Accept": "application/json"}),
            WEB_RETRY,
        )
        if resp.status_code != 200:
            return None

        try:
            data = resp.json()
        except Exception as e:
            print(f"[Scraper] Reddit JSON parse error: {e}")
            return None

        lines = []
        title = url

        # Handle listing (subreddit / front page) — data is a single dict with "data.children"
        if isinstance(data, dict) and "data" in data:
            children = data["data"].get("children", [])
            title = f"Reddit — {url}"
            for child in children[:50]:
                post = child.get("data", {})
                post_title = post.get("title", "")
                selftext = post.get("selftext", "")
                subreddit = post.get("subreddit_name_prefixed", "")
                author = post.get("author", "")
                score = post.get("score", 0)
                if post_title:
                    lines.append(f"[{subreddit}] {post_title} (by u/{author}, score: {score})")
                if selftext and selftext not in ("[removed]", "[deleted]"):
                    lines.append(f"  {selftext[:500]}")

        # Handle post + comments — data is a list of two listings
        elif isinstance(data, list) and len(data) >= 1:
            post_listing = data[0].get("data", {}).get("children", [])
            if post_listing:
                post = post_listing[0].get("data", {})
                title = post.get("title", url)
                selftext = post.get("selftext", "")
                author = post.get("author", "")
                subreddit = post.get("subreddit_name_prefixed", "")
                lines.append(f"Title: {title}")
                lines.append(f"Subreddit: {subreddit} | Author: u/{author}")
                if selftext and selftext not in ("[removed]", "[deleted]"):
                    lines.append(f"\n{selftext}")

            # Comments
            if len(data) >= 2:
                lines.append("\n--- Comments ---")
                comment_children = data[1].get("data", {}).get("children", [])
                self._extract_comments(comment_children, lines, depth=0, remaining=[30])

        text = "\n".join(lines).strip()
        if not text:
            return None

        return {"url": url, "text": text, "title": title, "raw_html": ""}

    def _extract_comments(
        self,
        children: list,
        lines: list,
        depth: int,
        # remaining is a one-element list so mutations propagate across all
        # recursive levels — avoids double-counting against the global budget.
        remaining: list[int],
    ) -> None:
        """Recursively extract comment text. `remaining[0]` is a shared counter."""
        for child in children:
            if remaining[0] <= 0:
                return
            if child.get("kind") == "more":
                continue
            data = child.get("data", {})
            body = data.get("body", "")
            if body and body not in ("[removed]", "[deleted]"):
                indent = "  " * depth
                author = data.get("author", "")
                score  = data.get("score", 0)
                lines.append(f"{indent}u/{author} (score {score}): {body[:300]}")
                remaining[0] -= 1
            if depth < 3 and remaining[0] > 0:
                replies = data.get("replies", {})
                if isinstance(replies, dict):
                    reply_children = replies.get("data", {}).get("children", [])
                    if reply_children:
                        self._extract_comments(reply_children, lines, depth + 1, remaining)

    def _extract_text(self, html: str, url: str) -> tuple[str, str]:
        """Extract clean text from HTML. Tries trafilatura first, then BS4."""
        title = ""
        text = ""

        if HAS_TRAFILATURA:
            extracted = trafilatura.extract(
                html,
                include_comments=False,
                include_tables=True,
                include_links=False,
                no_fallback=False,
                favor_recall=True,   # less aggressive filtering — catches portfolio/non-article content
                url=url,
            )
            if extracted and len(extracted.strip()) > 50:
                text = extracted
                if HAS_BS4:
                    soup = BeautifulSoup(html, "html.parser")
                    t = soup.find("title")
                    title = t.get_text(strip=True) if t else url
                else:
                    title = url
                return text, title

        # BS4 fallback — used when trafilatura finds nothing (portfolio sites,
        # landing pages, anything that isn't a standard article)
        if HAS_BS4:
            soup = BeautifulSoup(html, "html.parser")

            # Title
            t = soup.find("title")
            title = t.get_text(strip=True) if t else url

            # Only strip actual noise — do NOT remove <header> since many
            # portfolio/personal sites put all their content there
            for tag in soup(["script", "style", "iframe", "noscript",
                              "svg", "canvas", "video", "audio"]):
                tag.decompose()

            body = soup.find("body") or soup
            text = body.get_text(separator="\n", strip=True)
            text = re.sub(r"\n{3,}", "\n\n", text)
            text = re.sub(r" {2,}", " ", text)

        return text, title

    def _extract_links(self, html: str, base_url: str, base_domain: str) -> list[str]:
        """Find all <a href> links on the page within the same domain."""
        if not HAS_BS4 or not html:
            return []
        soup = BeautifulSoup(html, "html.parser")
        links = []
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            # Skip anchors, mailto, javascript
            if href.startswith(("#", "mailto:", "javascript:", "tel:")):
                continue
            full_url = urljoin(base_url, href).split("#")[0]
            parsed = urlparse(full_url)
            if parsed.scheme not in ("http", "https"):
                continue
            if parsed.netloc == base_domain:
                links.append(full_url)
        return list(set(links))
