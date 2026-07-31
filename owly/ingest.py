"""RSS ingestion with full-text extraction."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Optional
from urllib.parse import quote

import feedparser
import httpx
import trafilatura

from owly.config import get_settings
from owly.db import get_db, is_seen

logger = logging.getLogger(__name__)

MORSS_BASE = "https://morss.it"


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


def _fetch_url(url: str, timeout: float = 30.0) -> Optional[str]:
    try:
        with httpx.Client(follow_redirects=True, timeout=timeout) as client:
            response = client.get(url, headers={"User-Agent": "Owly/0.1 (+local news curator)"})
            response.raise_for_status()
            return response.text
    except httpx.HTTPError as exc:
        logger.warning("Failed to fetch %s: %s", url, exc)
        return None


def _extract_full_text(page_html: str, url: str) -> str:
    text = trafilatura.extract(
        page_html,
        url=url,
        include_comments=False,
        include_tables=False,
        favor_precision=True,
    )
    return (text or "").strip()


def _fetch_via_morss(feed_url: str) -> Optional[str]:
    morss_url = f"{MORSS_BASE}/{quote(feed_url, safe='')}"
    logger.info("Fetching feed via morss.it: %s", feed_url)
    return _fetch_url(morss_url, timeout=45.0)


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
        full_text = summary
        items.append(
            FeedItem(
                title=title,
                url=url,
                published=published,
                feed_label=feed_label,
                feed_url=feed_url,
                summary=summary,
                full_text=full_text,
            )
        )
    return items


def _enrich_full_text(item: FeedItem) -> FeedItem:
    html = _fetch_url(item.url)
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
) -> list[FeedItem]:
    settings = get_settings()
    window_hours = hours if hours is not None else settings.ingestion_hours

    content: Optional[str] = None
    if use_morss:
        content = _fetch_via_morss(feed_url)
    if not content:
        content = _fetch_url(feed_url)
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

    enriched = [_enrich_full_text(item) for item in filtered]
    return enriched


def fetch_all_rss(skip_seen: bool = True) -> list[FeedItem]:
    all_items: list[FeedItem] = []
    with get_db() as conn:
        from owly.db import list_sources

        feeds = list_sources(conn, source_type="rss", enabled_only=True)
    for feed in feeds:
        items = fetch_rss_items(
            feed.value,
            feed.label or feed.value,
            use_morss=feed.use_morss,
            skip_seen=skip_seen,
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


def format_items_for_prompt(items: list[FeedItem], max_chars: int = 8000, excerpt_chars: int = 400) -> str:
    """Serialize feed items into a compact context block for Grok."""
    blocks: list[str] = []
    total = 0
    for item in items:
        published = item.published.isoformat() if item.published else "unknown"
        text = item.full_text or item.summary
        if len(text) > excerpt_chars:
            text = text[:excerpt_chars].rstrip() + "..."
        block = (
            f"Title: {item.title}\n"
            f"URL: {item.url}\n"
            f"Feed: {item.feed_label}\n"
            f"Published: {published}\n"
            f"Excerpt (full article downloaded locally):\n{text}\n"
        )
        if total + len(block) > max_chars:
            break
        blocks.append(block)
        total += len(block)
    return "\n---\n".join(blocks)
