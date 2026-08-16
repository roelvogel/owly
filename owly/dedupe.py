"""Deduplication for digest and stock items."""

from __future__ import annotations

import re
from typing import Iterable

from owly.models import DigestItem, DigestResult, StockDigestResult, StockItem
from owly.urls import canonicalize_url

_TICKER_PREFIX = re.compile(r"^\$?[A-Z]{1,5}\s*[-:]\s*", re.IGNORECASE)
_WS = re.compile(r"\s+")


def _normalize_title(title: str) -> str:
    text = title.strip().lower()
    text = _TICKER_PREFIX.sub("", text)
    text = _WS.sub(" ", text)
    return text


def _item_fingerprints(item: StockItem) -> set[str]:
    keys: set[str] = set()
    title_key = _normalize_title(item.title)
    if title_key:
        keys.add(f"title:{title_key}")
    for url in item.sources:
        norm = canonicalize_url(url)
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


def _digest_fingerprints(item: DigestItem) -> set[str]:
    keys: set[str] = set()
    title_key = _normalize_title(item.title)
    if title_key:
        keys.add(f"title:{title_key}")
    for url in item.sources:
        norm = canonicalize_url(url)
        if norm:
            keys.add(f"url:{norm}")
    return keys


def dedupe_digest_items(result: DigestResult) -> DigestResult:
    """Drop duplicate main-digest stories that share a title or source URL."""
    seen: set[str] = set()
    kept: list[DigestItem] = []
    for item in result.items:
        fingerprints = _digest_fingerprints(item)
        if fingerprints and fingerprints & seen:
            continue
        seen.update(fingerprints)
        kept.append(item)
    if not kept:
        return result
    return result.model_copy(update={"items": kept})
