"""Pure parsing/formatting helpers: result de-duplication, RSS parsing, the
requested-count parser, and article formatting (no I/O)."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

# ── Deduplication ──────────────────────────────────────────────────────────────

_STOPWORDS = {"the", "a", "an", "in", "of", "to", "and", "for",
              "is", "are", "on", "at", "by", "with", "from"}


def _deduplicate(items: list[dict], title_key: str, url_key: str) -> list[dict]:
    """Remove items that share a URL or have >70% title word overlap."""
    seen_urls: set[str] = set()
    seen_title_word_sets: list[set] = []
    unique = []

    for item in items:
        url        = item.get(url_key, "")
        raw_title  = item.get(title_key, "").lower()
        title_words = {w for w in re.findall(r"\w+", raw_title) if w not in _STOPWORDS}

        if url and url in seen_urls:
            continue

        too_similar = any(
            prev and title_words and
            len(title_words & prev) / min(len(title_words), len(prev)) > 0.7
            for prev in seen_title_word_sets
        )
        if too_similar:
            continue

        if url:
            seen_urls.add(url)
        seen_title_word_sets.append(title_words)
        unique.append(item)

    return unique


# ── RSS parsing (pure, no I/O) ─────────────────────────────────────────────────

def _parse_rss(xml_text: str, max_results: int) -> list[dict]:
    """Parse Google News RSS XML into a list of article dicts."""
    root = ET.fromstring(xml_text)
    parsed = []
    for item in root.findall(".//item"):
        title  = (item.findtext("title") or "").strip()
        link   = (item.findtext("link")  or "").strip()
        desc   = re.sub(r"<[^>]+>", "", item.findtext("description") or "").strip()
        pub    = (item.findtext("pubDate") or "").strip()
        source = ""
        src_el = item.find("source")
        if src_el is not None:
            source = src_el.text or ""
        parsed.append({"title": title, "url": link, "desc": desc,
                        "pub": pub, "source": source})
    return _deduplicate(parsed, title_key="title", url_key="url")[:max_results]


# Matches "20 articles", "top 5 headlines", "give me 10 stories", etc.
_COUNT_RE = re.compile(
    r"(?:top|latest|give\s+me|show\s+me|find|get|fetch|list)?\s*(\d+)\s*"
    r"(?:news|stories|story|articles?|headlines?|results?|updates?)\b"
    r"|\b(a\s+few|several|some)\s+(?:news|stories|articles?|headlines?)\b",
    re.IGNORECASE,
)


def _parse_requested_count(query: str, default: int = 5, maximum: int = 25) -> int:
    """Extract an explicit article count from the user's query."""
    m = _COUNT_RE.search(query)
    if m:
        if m.group(1):
            return min(int(m.group(1)), maximum)
        if m.group(2):
            return 5  # "a few" / "several" / "some"
    return default


def _format_articles(articles: list[dict], desc_cap: int = 200) -> str:
    lines = []
    for i, r in enumerate(articles, 1):
        src = f" — {r['source']}" if r["source"] else ""
        # Trim pub date to just "Day, DD Mon YYYY" (drop time + timezone)
        pub = r["pub"]
        if pub:
            pub = ", ".join(pub.split(", ")[:2]) if ", " in pub else pub
        desc = r["desc"][:desc_cap] if r["desc"] else ""
        line = f"[{i}] {r['title']}{src} ({pub})"
        if desc:
            line += f"\n{desc}"
        lines.append(line)
    return "\n\n".join(lines)


