"""CLI entrypoint for generating Owly editions."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from owly.config import get_settings
from owly.db import (
    Source,
    add_edition,
    create_run,
    finish_run,
    get_db,
    list_sources,
    mark_seen,
    prune_seen_items,
)
from owly.dedupe import dedupe_digest_items, dedupe_stock_results
from owly.grok import GrokClient
from owly.ingest import (
    FeedItem,
    fetch_all_rss,
    format_items_for_prompt,
    hydrate_digest_from_rss,
    items_for_ticker,
)
from owly.models import DigestResult, StockDigestResult
from owly.render import edition_filename, render_combined_stocks_edition, render_main_edition
from owly.urls import canonicalize_url

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def detect_edition_slot(now: datetime | None = None) -> str:
    current = now or datetime.now()
    return "morning" if current.hour < 15 else "evening"


def published_sources(
    digest: DigestResult,
    stock_digests: list[StockDigestResult],
) -> list[tuple[str, str | None]]:
    """URLs that actually appeared in an edition — the only ones that should be marked seen."""
    rows: list[tuple[str, str | None]] = []
    for item in digest.items:
        for url in item.sources:
            rows.append((url, item.title))
    for stock_digest in stock_digests:
        for item in stock_digest.items:
            for url in item.sources:
                rows.append((url, item.title))
    return rows


def _log_unselected_rss(digest: DigestResult, rss_items: list[FeedItem]) -> None:
    selected = {canonicalize_url(url) for item in digest.items for url in item.sources}
    for item in rss_items:
        if canonicalize_url(item.url) not in selected:
            logger.info("RSS not selected this run: %s (%s)", item.title, item.url)


def _stock_digest_for_source(
    grok: GrokClient,
    stock: Source,
    rss_items: list[FeedItem],
) -> tuple[StockDigestResult, int, int]:
    matches = items_for_ticker(rss_items, stock.value, stock.label or "")
    rss_context = ""
    if matches:
        rss_context = format_items_for_prompt(
            matches,
            max_chars=12000,
            excerpt_chars=2000,
            min_body_chars=0,
        )
        logger.info("Stock %s: %d matching RSS items", stock.value, len(matches))
    return grok.generate_stock_digest(
        stock.value,
        company_name=stock.label,
        rss_context=rss_context,
    )


def run_edition(edition_slot: str | None = None, dry_run: bool = False) -> int:
    settings = get_settings()
    settings.ensure_dirs()
    slot = edition_slot or detect_edition_slot()
    now = datetime.now()

    logger.info("Starting Owly %s edition", slot)

    with get_db() as conn:
        run_id = create_run(conn, slot)

    started = time.perf_counter()
    total_in = 0
    total_out = 0
    stock_links: list[tuple[str, str]] = []

    try:
        rss_items = fetch_all_rss(skip_seen=True)
        logger.info("Collected %d RSS items after filtering", len(rss_items))

        with get_db() as conn:
            topics = [s.value for s in list_sources(conn, source_type="topic", enabled_only=True)]
            stock_sources = list_sources(conn, source_type="stock", enabled_only=True)

        if dry_run:
            logger.info("Dry run: skipping xAI search and Cursor writing (%d RSS items)", len(rss_items))
            with get_db() as conn:
                finish_run(
                    conn,
                    run_id,
                    "dry_run",
                    duration_ms=int((time.perf_counter() - started) * 1000),
                )
            return 0

        grok = GrokClient()
        rss_context = format_items_for_prompt(rss_items)

        digest, in_tok, out_tok = grok.generate_main_digest(rss_context, topics)
        digest = dedupe_digest_items(digest)
        digest = hydrate_digest_from_rss(digest, rss_items)
        _log_unselected_rss(digest, rss_items)
        total_in += in_tok
        total_out += out_tok

        stock_digests: list[StockDigestResult] = []
        if stock_sources:
            workers = max(1, min(settings.stock_max_workers, len(stock_sources)))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [
                    pool.submit(_stock_digest_for_source, grok, stock, rss_items)
                    for stock in stock_sources
                ]
                for stock, future in zip(stock_sources, futures):
                    stock_digest, s_in, s_out = future.result()
                    total_in += s_in
                    total_out += s_out
                    stock_digests.append(stock_digest)
                    logger.info("Stock %s: %d items", stock.value, len(stock_digest.items))

        stocks_path: Path | None = None
        if stock_sources:
            deduped = dedupe_stock_results(stock_digests)
            stocks_filename = edition_filename(now, slot, "stocks")
            stocks_md = render_combined_stocks_edition(deduped, slot, now)
            stocks_path = settings.output_dir / stocks_filename
            stocks_path.write_text(stocks_md, encoding="utf-8")
            stock_links.append(("Stocks", stocks_filename))
            logger.info("Wrote combined stock edition: %s", stocks_path)

        main_filename = edition_filename(now, slot)
        main_md = render_main_edition(digest, slot, now, stock_links)
        main_path = settings.output_dir / main_filename
        main_path.write_text(main_md, encoding="utf-8")
        logger.info("Wrote main edition: %s", main_path)

        seen = published_sources(digest, stock_digests)
        with get_db() as conn:
            mark_seen(conn, seen, run_id=run_id)
            prune_seen_items(conn)
            add_edition(
                conn,
                run_id,
                "main",
                str(main_path),
                f"Owly Digest — {now.strftime('%Y-%m-%d')} ({slot.capitalize()})",
            )
            if stocks_path is not None:
                add_edition(
                    conn,
                    run_id,
                    "stocks",
                    str(stocks_path),
                    f"Stocks — {now.strftime('%Y-%m-%d')} ({slot.capitalize()})",
                )
            finish_run(
                conn,
                run_id,
                "success",
                input_tokens=total_in,
                output_tokens=total_out,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )

        logger.info(
            "Edition complete. Tokens in=%d out=%d",
            total_in,
            total_out,
        )
        return 0

    except Exception as exc:
        logger.exception("Edition run failed")
        with get_db() as conn:
            finish_run(
                conn,
                run_id,
                "failed",
                error=str(exc),
                input_tokens=total_in,
                output_tokens=total_out,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate Owly e-ink news editions")
    parser.add_argument(
        "--edition",
        choices=["morning", "evening"],
        help="Edition slot (default: auto-detect from time of day)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch RSS only, skip Grok API calls",
    )
    args = parser.parse_args(argv)
    return run_edition(edition_slot=args.edition, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
