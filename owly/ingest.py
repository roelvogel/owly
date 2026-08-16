"""RSS ingestion with full-text extraction."""

from __future__ import annotations

import logging
import re
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from typing import Iterator, Optional
from urllib.parse import quote

import feedparser
import httpx
import trafilatura

from owly.config import get_settings
from owly.db import get_db, is_seen
from owly.models import DigestResult
from owly.urls import canonicalize_url

logger = logging.getLogger(__name__)

MORSS_BASE = "https://morss.it"
_PROMPT_MAX_CHARS = 40000
_EXCERPT_CHARS = 3000
_PER_FEED_CAP = 8
_ENRICH_WORKERS = 6
_SKIP_FETCH_IF_CHARS = 800
_CLUSTER_SIMILARITY = 0.72
_HYDRATE_KEEP_CHARS = 400
_HYDRATE_FALLBACK_MAX = 900
_USER_AGENT = "Owly/0.1 (+local news curator)"


@dataclass
class FeedItem:
    title: str
    url: str
    published: Optional[datetime]
    feed_label: str
    feed_url: str
    summary: str
    full_text: str


def _parse_published(entry: dict) -> Optional[datetime]:
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            try:
                return datetime(*parsed[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                pass
    for key in ("published", "updated"):
        raw = entry.get(key)
        if raw:
            try:
                dt = parsedate_to_datetime(raw)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except (TypeError, ValueError):
                pass
    return None


def _entry_url(entry: dict) -> Optional[str]:
    link = entry.get("link")
    if link:
        return link.strip()
    links = entry.get("links") or []
    for item in links:
        if item.get("rel") == "alternate" and item.get("href"):
            return item["href"].strip()
    return None


def _entry_summary(entry: dict) -> str:
    for key in ("summary", "description", "content"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return _strip_html(value)
        if isinstance(value, list) and value:
            first = value[0]
            if isinstance(first, dict):
                val = first.get("value", "")
                if val.strip():
                    return _strip_html(val)
    return ""


def _strip_html(text: str) -> str:
    if "<" not in text:
        return text.strip()
    cleaned = re.sub(r"<[^>]+>", " ", text)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


@contextmanager
def _http_client(timeout: float = 30.0) -> Iterator[httpx.Client]:
    client = httpx.Client(
        follow_redirects=True,
        timeout=timeout,
        headers={"User-Agent": _USER_AGENT},
        limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
    )
    try:
        yield client
    finally:
        client.close()


def _fetch_url(
    url: str,
    timeout: float = 30.0,
    client: Optional[httpx.Client] = None,
) -> Optional[str]:
    own_client = client is None
    http = client or httpx.Client(
        follow_redirects=True,
        timeout=timeout,
        headers={"User-Agent": _USER_AGENT},
    )
    try:
        response = http.get(url, timeout=timeout)
        response.raise_for_status()
        return response.text
    except httpx.HTTPError as exc:
        logger.warning("Failed to fetch %s: %s", url, exc)
        return None
    finally:
        if own_client:
            http.close()


def _extract_full_text(page_html: str, url: str) -> str:
    text = trafilatura.extract(
        page_html,
        url=url,
        include_comments=False,
        include_tables=False,
        favor_precision=True,
    )
    return (text or "").strip()


def _fetch_via_morss(feed_url: str, client: Optional[httpx.Client] = None) -> Optional[str]:
    morss_url = f"{MORSS_BASE}/{quote(feed_url, safe='')}"
    logger.info("Fetching feed via morss.it: %s", feed_url)
    return _fetch_url(morss_url, timeout=45.0, client=client)


def _parse_feed_content(content: str, feed_url: str, feed_label: str) -> list[FeedItem]:
    parsed = feedparser.parse(content)
    items: list[FeedItem] = []
    for entry in parsed.entries:
        url = _entry_url(entry)
        if not url:
            continue
        title = (entry.get("title") or "Untitled").strip()
        published = _parse_published(entry)
        summary = _entry_summary(entry)
        items.append(
            FeedItem(
                title=title,
                url=url,
                published=published,
                feed_label=feed_label,
                feed_url=feed_url,
                summary=summary,
                full_text=summary,
            )
        )
    return items


def _enrich_full_text(item: FeedItem, client: Optional[httpx.Client] = None) -> FeedItem:
    if item.full_text and len(item.full_text) >= _SKIP_FETCH_IF_CHARS:
        return item
    html = _fetch_url(item.url, client=client)
    if not html:
        return item
    extracted = _extract_full_text(html, item.url)
    if extracted:
        item.full_text = extracted
    return item


def _within_window(published: Optional[datetime], hours: int) -> bool:
    if published is None:
        return True
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    return published >= cutoff


def fetch_rss_items(
    feed_url: str,
    feed_label: str,
    use_morss: bool = False,
    hours: Optional[int] = None,
    skip_seen: bool = True,
    client: Optional[httpx.Client] = None,
) -> list[FeedItem]:
    settings = get_settings()
    window_hours = hours if hours is not None else settings.ingestion_hours

    content: Optional[str] = None
    if use_morss:
        content = _fetch_via_morss(feed_url, client=client)
    if not content:
        content = _fetch_url(feed_url, client=client)
    if not content:
        logger.error("Could not fetch RSS feed: %s", feed_url)
        return []

    items = _parse_feed_content(content, feed_url, feed_label)

    filtered: list[FeedItem] = []
    with get_db() as conn:
        for item in items:
            if not _within_window(item.published, window_hours):
                continue
            if skip_seen and is_seen(conn, item.url):
                continue
            filtered.append(item)

    return _enrich_items(filtered, client)


def _enrich_items(items: list[FeedItem], client: Optional[httpx.Client]) -> list[FeedItem]:
    if not items:
        return []
    if len(items) == 1:
        return [_enrich_full_text(items[0], client)]
    workers = min(_ENRICH_WORKERS, len(items))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda item: _enrich_full_text(item, client), items))


def fetch_all_rss(skip_seen: bool = True) -> list[FeedItem]:
    all_items: list[FeedItem] = []
    with get_db() as conn:
        from owly.db import list_sources

        feeds = list_sources(conn, source_type="rss", enabled_only=True)
    with _http_client() as client:
        for feed in feeds:
            items = fetch_rss_items(
                feed.value,
                feed.label or feed.value,
                use_morss=feed.use_morss,
                skip_seen=skip_seen,
                client=client,
            )
            all_items.extend(items)
            logger.info("Fetched %d items from %s", len(items), feed.label or feed.value)
    return all_items


def items_for_ticker(items: list[FeedItem], ticker: str) -> list[FeedItem]:
    needle = ticker.upper().lstrip("$")
    matches: list[FeedItem] = []
    for item in items:
        blob = f"{item.title} {item.summary} {item.full_text}".upper()
        if needle in blob or f"${needle}" in blob:
            matches.append(item)
    return matches


def title_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()


def cap_per_feed(items: list[FeedItem], cap: int = _PER_FEED_CAP) -> list[FeedItem]:
    counts: dict[str, int] = {}
    capped: list[FeedItem] = []
    for item in items:
        key = item.feed_url or item.feed_label
        used = counts.get(key, 0)
        if used >= cap:
            continue
        counts[key] = used + 1
        capped.append(item)
    return capped


def cluster_feed_items(
    items: list[FeedItem],
    similarity: float = _CLUSTER_SIMILARITY,
) -> list[list[FeedItem]]:
    """Group near-duplicate stories by canonical URL or title similarity."""
    clusters: list[list[FeedItem]] = []
    cluster_urls: list[str] = []
    cluster_titles: list[str] = []
    for item in items:
        url = canonicalize_url(item.url)
        title = title_key(item.title)
        matched: Optional[int] = None
        for index, cluster in enumerate(clusters):
            if url and url == cluster_urls[index]:
                matched = index
                break
            other_title = cluster_titles[index]
            if title and other_title and SequenceMatcher(None, title, other_title).ratio() >= similarity:
                matched = index
                break
        if matched is None:
            clusters.append([item])
            cluster_urls.append(url)
            cluster_titles.append(title)
        else:
            clusters[matched].append(item)
    return clusters


def round_robin_by_feed(items: list[FeedItem]) -> list[FeedItem]:
    buckets: dict[str, deque[FeedItem]] = defaultdict(deque)
    order: list[str] = []
    for item in items:
        key = item.feed_label or item.feed_url
        if key not in buckets:
            order.append(key)
        buckets[key].append(item)
    interleaved: list[FeedItem] = []
    while any(buckets.values()):
        for key in order:
            if buckets[key]:
                interleaved.append(buckets[key].popleft())
    return interleaved


def format_items_for_prompt(
    items: list[FeedItem],
    max_chars: int = _PROMPT_MAX_CHARS,
    excerpt_chars: int = _EXCERPT_CHARS,
    per_feed_cap: int = _PER_FEED_CAP,
) -> str:
    """Serialize feed items into a context block, fairly sampled across feeds."""
    capped = cap_per_feed(items, per_feed_cap)
    clusters = cluster_feed_items(capped)
    representatives = [cluster[0] for cluster in clusters]
    extras_by_id = {
        id(cluster[0]): cluster[1:]
        for cluster in clusters
        if len(cluster) > 1
    }
    ordered = round_robin_by_feed(representatives)

    blocks: list[str] = []
    total = 0
    for item in ordered:
        published = item.published.isoformat() if item.published else "unknown"
        text = item.full_text or item.summary
        truncated = len(text) > excerpt_chars
        if truncated:
            text = text[:excerpt_chars].rstrip() + "..."
        label = "Article text (partial)" if truncated else "Article text (full)"
        aliases = extras_by_id.get(id(item), [])
        also = ""
        if aliases:
            names = ", ".join(sorted({alias.feed_label for alias in aliases if alias.feed_label}))
            also = f"Also covered by: {names}\n"
        block = (
            f"Title: {item.title}\n"
            f"URL: {item.url}\n"
            f"Feed: {item.feed_label}\n"
            f"Published: {published}\n"
            f"{also}"
            f"{label} (downloaded locally):\n{text}\n"
        )
        if total + len(block) > max_chars:
            break
        blocks.append(block)
        total += len(block)
    return "\n---\n".join(blocks)


# Soft cap for hydrated RSS bodies in the edition (chars). Truncate at a paragraph break when possible.
_RSS_BODY_MAX_CHARS = 2500

_HEADING_LINE = re.compile(r"^(#{1,6})\s+(.+)$")
_TRAILING_HASH_HEADING = re.compile(r"^(.+?)\s+#{1,6}\s*$")
_TOC_LINE = re.compile(
    r"(?i)^(in this article:?|table of contents:?|contents:?|references for this article:?|"
    r"feedback and questions are welcome!?|share this:?|related (posts|articles):?|"
    r"subscribe to|leave a (comment|reply)|posted (on|by)|written by)\b.*$"
)
_TOC_BULLET = re.compile(r"^[-*]\s+.+$")
_SEPARATOR_LINE = re.compile(r"^[=-]{3,}\s*$")


def sanitize_article_body(text: str, max_chars: int = _RSS_BODY_MAX_CHARS) -> str:
    """Clean RSS full text for digest display: demote headings, drop chrome, cap length."""
    if not text or not text.strip():
        return ""

    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    cleaned: list[str] = []
    in_toc = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            in_toc = False
            if cleaned and cleaned[-1] != "":
                cleaned.append("")
            continue

        if _TOC_LINE.match(stripped):
            in_toc = True
            continue

        if in_toc and _TOC_BULLET.match(stripped):
            continue
        in_toc = False

        if _SEPARATOR_LINE.match(stripped):
            continue

        heading = _HEADING_LINE.match(stripped)
        if heading:
            cleaned.append(heading.group(2).strip())
            cleaned.append("")
            continue

        trailing = _TRAILING_HASH_HEADING.match(stripped)
        if trailing and len(trailing.group(1)) < 80:
            cleaned.append(trailing.group(1).strip())
            cleaned.append("")
            continue

        cleaned.append(stripped)

    body_lines: list[str] = []
    for line in cleaned:
        if line == "" and (not body_lines or body_lines[-1] == ""):
            continue
        body_lines.append(line)
    body = "\n".join(body_lines).strip()

    if len(body) <= max_chars:
        return body

    truncated = body[:max_chars]
    break_at = truncated.rfind("\n\n")
    if break_at < max_chars // 2:
        for sep in (". ", ".\n", "! ", "? "):
            idx = truncated.rfind(sep)
            if idx >= max_chars // 2:
                break_at = idx + 1
                break
        else:
            break_at = -1
    if break_at >= max_chars // 2:
        truncated = truncated[:break_at].rstrip()
    else:
        truncated = truncated.rstrip()
    return truncated + "\n\n[…]"


def _match_rss_item(digest_item_title: str, sources: list[str], rss_items: list[FeedItem]) -> Optional[FeedItem]:
    url_map = {canonicalize_url(item.url): item for item in rss_items}
    for url in sources:
        feed_item = url_map.get(canonicalize_url(url))
        if feed_item is not None:
            return feed_item
    needle = title_key(digest_item_title)
    if not needle:
        return None
    best: Optional[FeedItem] = None
    best_ratio = 0.0
    for item in rss_items:
        ratio = SequenceMatcher(None, needle, title_key(item.title)).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best = item
    if best is not None and best_ratio >= 0.85:
        return best
    return None


def hydrate_digest_from_rss(digest: DigestResult, rss_items: list[FeedItem]) -> DigestResult:
    """Fill empty/short RSS-sourced summaries from local text; keep Grok-written articles."""
    hydrated_items = []
    for digest_item in digest.items:
        if len(digest_item.summary.strip()) >= _HYDRATE_KEEP_CHARS:
            hydrated_items.append(digest_item)
            continue
        feed_item = _match_rss_item(digest_item.title, digest_item.sources, rss_items)
        if feed_item is None:
            hydrated_items.append(digest_item)
            continue
        body = feed_item.full_text or feed_item.summary
        if body:
            cleaned = sanitize_article_body(body, max_chars=_HYDRATE_FALLBACK_MAX)
            hydrated_items.append(digest_item.model_copy(update={"summary": cleaned}))
        else:
            hydrated_items.append(digest_item)
    return digest.model_copy(update={"items": hydrated_items})
