"""Cross-ticker deduplication for stock digest items."""

from __future__ import annotations

import re
from typing import Iterable
from urllib.parse import urlparse, urlunparse

from owly.models import StockDigestResult, StockItem

_TICKER_PREFIX = re.compile(r"^\$?[A-Z]{1,5}\s*[-:]\s*", re.IGNORECASE)
_WS = re.compile(r"\s+")


def _normalize_title(title: str) -> str:
    text = title.strip().lower()
    text = _TICKER_PREFIX.sub("", text)
    text = _WS.sub(" ", text)
    return text


def _normalize_url(url: str) -> str:
    url = url.strip()
    if not url:
        return ""
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    netloc = parsed.netloc.lower()
    return urlunparse((parsed.scheme.lower(), netloc, path, "", "", ""))


def _item_fingerprints(item: StockItem) -> set[str]:
    keys: set[str] = set()
    title_key = _normalize_title(item.title)
    if title_key:
        keys.add(f"title:{title_key}")
    for url in item.sources:
        norm = _normalize_url(url)
        if norm:
            keys.add(f"url:{norm}")
    return keys


def dedupe_stock_results(results: Iterable[StockDigestResult]) -> list[StockDigestResult]:
    """Keep the first ticker to claim each story; drop duplicates in later tickers."""
    seen: set[str] = set()
    deduped: list[StockDigestResult] = []

    for result in results:
        kept: list[StockItem] = []
        for item in result.items:
            fingerprints = _item_fingerprints(item)
            if not fingerprints:
                kept.append(item)
                continue
            if fingerprints & seen:
                continue
            seen.update(fingerprints)
            kept.append(item)
        deduped.append(StockDigestResult(ticker=result.ticker, items=kept))

    return deduped
