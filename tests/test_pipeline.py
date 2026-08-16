"""Integration tests that do not require a live xAI API key."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from owly.config import Settings
from owly.db import add_source, get_db, init_db, is_seen, list_editions
from owly.dedupe import dedupe_digest_items, dedupe_stock_results
from owly.grok import GrokClient
from owly.ingest import FeedItem, hydrate_digest_from_rss, sanitize_article_body
from owly.models import DigestItem, DigestResult, StockDigestResult, StockItem
from owly.render import render_combined_stocks_edition, render_main_edition
from owly.run import published_sources


class PipelineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.output_dir = root / "editions"
        self.data_dir = root / "data"
        self.output_dir.mkdir()
        self.data_dir.mkdir()

        self.settings = Settings(
            xai_api_key="test-key",
            output_dir=self.output_dir,
            data_dir=self.data_dir,
        )
        init_db(self.settings.db_path)

    def tearDown(self) -> None:
        self.tmp.cleanup()
        import owly.config as config_mod

        config_mod._settings = None

    def test_render_main_and_combined_stocks_editions(self) -> None:
        digest = DigestResult(
            items=[
                DigestItem(
                    title="AI breakthrough",
                    summary="Researchers announced a new model.",
                    sources=["https://example.com/ai"],
                )
            ]
        )
        stocks = [
            StockDigestResult(
                ticker="NVDA",
                items=[
                    StockItem(
                        title="NVDA earnings chatter",
                        summary="Analysts discuss guidance.",
                        sources=["https://x.com/example/status/1"],
                    )
                ],
            )
        ]

        main_md = render_main_edition(
            digest,
            "morning",
            datetime(2026, 7, 11, 7, 0),
            stock_links=[("Stocks", "edition_2026-07-11_morning_stocks.md")],
        )
        stocks_md = render_combined_stocks_edition(stocks, "morning", datetime(2026, 7, 11, 7, 0))

        self.assertIn("# Owly Digest", main_md)
        self.assertIn("## In this edition", main_md)
        self.assertIn("1. AI breakthrough", main_md)
        self.assertIn("## 1. AI breakthrough", main_md)
        self.assertIn("1 article from RSS and X", main_md)
        self.assertNotIn("https://example.com/ai", main_md)
        self.assertNotIn("Why it matters", main_md)
        self.assertNotIn("**Sources**", main_md)
        self.assertIn("## Stocks", main_md)
        self.assertIn("# Stocks", stocks_md)
        self.assertIn("## NVDA", stocks_md)
        self.assertIn("### 1. NVDA earnings chatter", stocks_md)
        self.assertNotIn("emoji", main_md)

    def test_hydrate_digest_from_rss_replaces_summary_with_full_text(self) -> None:
        rss_url = "https://example.com/article"
        digest = DigestResult(
            items=[
                DigestItem(
                    title="Story",
                    summary="Short placeholder from Grok.",
                    sources=[rss_url],
                ),
                DigestItem(
                    title="X post",
                    summary="Longer AI-written article about the topic.",
                    sources=["https://x.com/example/status/1"],
                ),
            ]
        )
        rss_items = [
            FeedItem(
                title="Story",
                url=rss_url,
                published=datetime.now(),
                feed_label="Test",
                feed_url="https://example.com/feed",
                summary="RSS summary fallback.",
                full_text="Complete article body from the RSS feed with many paragraphs.",
            )
        ]

        hydrated = hydrate_digest_from_rss(digest, rss_items)

        self.assertEqual(
            hydrated.items[0].summary,
            "Complete article body from the RSS feed with many paragraphs.",
        )
        self.assertEqual(
            hydrated.items[1].summary,
            "Longer AI-written article about the topic.",
        )

    def test_hydrate_keeps_long_grok_rewrite(self) -> None:
        rss_url = "https://example.com/article"
        rewrite = "Lead paragraph. " * 40
        digest = DigestResult(
            items=[
                DigestItem(
                    title="Story",
                    summary=rewrite,
                    sources=[rss_url],
                )
            ]
        )
        rss_items = [
            FeedItem(
                title="Story",
                url=rss_url,
                published=datetime.now(),
                feed_label="Test",
                feed_url="https://example.com/feed",
                summary="RSS summary fallback.",
                full_text="Raw dump that should not replace a real rewrite.",
            )
        ]
        hydrated = hydrate_digest_from_rss(digest, rss_items)
        self.assertEqual(hydrated.items[0].summary, rewrite)

    def test_hydrate_matches_canonical_url(self) -> None:
        digest = DigestResult(
            items=[
                DigestItem(
                    title="Story",
                    summary="Short.",
                    sources=["https://www.example.com/article/?utm_source=rss"],
                )
            ]
        )
        rss_items = [
            FeedItem(
                title="Story",
                url="https://example.com/article",
                published=datetime.now(),
                feed_label="Test",
                feed_url="https://example.com/feed",
                summary="short",
                full_text="Canonical body from the feed.",
            )
        ]
        hydrated = hydrate_digest_from_rss(digest, rss_items)
        self.assertEqual(hydrated.items[0].summary, "Canonical body from the feed.")

    def test_sanitize_article_body_demotes_headings_and_strips_chrome(self) -> None:
        raw = """Harvesting SSH Credentials

In this article::
- Intro and short summary
- the Data
- the Tech

Feedback and questions are welcome!

## Intro
Locations of honeypots

## Create password hash file
Run hashcat on the dump.

Related articles:
- Other post
"""
        cleaned = sanitize_article_body(raw, max_chars=5000)
        self.assertNotIn("## ", cleaned)
        self.assertNotIn("In this article", cleaned)
        self.assertNotIn("Feedback and questions", cleaned)
        self.assertNotIn("Related articles", cleaned)
        self.assertIn("Intro", cleaned)
        self.assertIn("Create password hash file", cleaned)
        self.assertIn("Run hashcat on the dump.", cleaned)

    def test_sanitize_article_body_caps_length_at_paragraph(self) -> None:
        paragraphs = [f"Paragraph {i}. " + ("word " * 40) for i in range(20)]
        raw = "\n\n".join(paragraphs)
        cleaned = sanitize_article_body(raw, max_chars=800)
        self.assertLessEqual(len(cleaned), 820)
        self.assertTrue(cleaned.endswith("[…]"))
        self.assertNotIn("Paragraph 19", cleaned)

    def test_hydrate_applies_sanitize_and_cap(self) -> None:
        rss_url = "https://example.com/long"
        long_body = "## Deep dive\n\n" + ("A long sentence about firmware. " * 200)
        digest = DigestResult(
            items=[
                DigestItem(
                    title="Long story",
                    summary="placeholder",
                    sources=[rss_url],
                )
            ]
        )
        rss_items = [
            FeedItem(
                title="Long story",
                url=rss_url,
                published=datetime.now(),
                feed_label="Test",
                feed_url="https://example.com/feed",
                summary="short",
                full_text=long_body,
            )
        ]

        hydrated = hydrate_digest_from_rss(digest, rss_items)
        body = hydrated.items[0].summary
        self.assertNotIn("## ", body)
        self.assertIn("Deep dive", body)
        self.assertLessEqual(len(body), 950)
        self.assertTrue(body.endswith("[…]"))

    def test_dedupe_stock_results_across_tickers(self) -> None:
        shared_url = "https://x.com/example/status/99"
        results = [
            StockDigestResult(
                ticker="NVDA",
                items=[
                    StockItem(
                        title="Chip rally",
                        summary="NVDA leads semis.",
                        sources=[shared_url],
                    )
                ],
            ),
            StockDigestResult(
                ticker="AMD",
                items=[
                    StockItem(
                        title="Chip rally",
                        summary="AMD follows NVDA.",
                        sources=[shared_url],
                    )
                ],
            ),
        ]
        deduped = dedupe_stock_results(results)
        self.assertEqual(len(deduped[0].items), 1)
        self.assertEqual(len(deduped[1].items), 0)

    def test_stock_prompts_include_campaigns_and_market_launches(self) -> None:
        client = GrokClient.__new__(GrokClient)
        client.settings = self.settings
        collect = client._collect_stock_posts_prompt("LMND", "Lemonade", broad=False)
        broad = client._collect_stock_posts_prompt("LMND", "Lemonade", broad=True)
        curate = client._curate_stock_posts_prompt(
            "LMND", "Lemonade", "- https://x.com/example/status/1", permissive=False
        )
        for prompt in (collect, broad, curate):
            self.assertIn("ad/marketing campaigns", prompt)
            self.assertIn("new state/market launches", prompt)
        self.assertIn("do not drop them as fluff", curate)

    def test_stock_digest_collect_then_curate(self) -> None:
        client = GrokClient.__new__(GrokClient)
        client.settings = self.settings
        client._collect_stock_posts = Mock(
            return_value=(
                "- https://x.com/example/status/1 @analyst $LMND guidance raised\n",
                200,
                100,
            )
        )
        client._curate_stock_posts = Mock(
            return_value=(
                StockDigestResult(
                    ticker="LMND",
                    items=[
                        StockItem(
                            title="Guidance revised higher",
                            summary="Renters expansion accelerating.",
                            sources=["https://x.com/example/status/1"],
                        )
                    ],
                ),
                150,
                80,
            )
        )
        client._recover_stock_digest = Mock()

        result, in_tok, out_tok = client.generate_stock_digest("LMND", company_name="Lemonade")

        self.assertEqual(len(result.items), 1)
        client._collect_stock_posts.assert_called_once_with("LMND", "Lemonade")
        client._curate_stock_posts.assert_called_once()
        client._recover_stock_digest.assert_not_called()
        self.assertEqual(in_tok, 350)
        self.assertEqual(out_tok, 180)

    def test_stock_digest_permissive_curate_on_empty_first_pass(self) -> None:
        client = GrokClient.__new__(GrokClient)
        client.settings = self.settings
        client._collect_stock_posts = Mock(
            return_value=(
                "- https://x.com/example/status/2 @trader $MU support holding\n",
                100,
                50,
            )
        )
        client._curate_stock_posts = Mock(
            side_effect=[
                (StockDigestResult(ticker="MU", items=[]), 80, 40),
                (
                    StockDigestResult(
                        ticker="MU",
                        items=[
                            StockItem(
                                title="Support holding",
                                summary="Technical discussion on key levels.",
                                sources=["https://x.com/example/status/2"],
                            )
                        ],
                    ),
                    90,
                    45,
                ),
            ]
        )
        client._recover_stock_digest = Mock()

        result, _, _ = client.generate_stock_digest("MU", company_name="Micron")

        self.assertEqual(len(result.items), 1)
        self.assertEqual(client._curate_stock_posts.call_count, 2)
        client._curate_stock_posts.assert_any_call("MU", "Micron", client._collect_stock_posts.return_value[0])
        client._curate_stock_posts.assert_any_call(
            "MU",
            "Micron",
            client._collect_stock_posts.return_value[0],
            permissive=True,
        )

    def test_stock_digest_recovers_when_collect_finds_nothing(self) -> None:
        client = GrokClient.__new__(GrokClient)
        client.settings = self.settings
        client._collect_stock_posts = Mock(
            side_effect=[
                ("No posts found.", 50, 20),
                ("Still nothing.", 60, 25),
            ]
        )
        client._curate_stock_posts = Mock()
        client._recover_stock_digest = Mock(
            return_value=(
                StockDigestResult(
                    ticker="LMND",
                    items=[
                        StockItem(
                            title="Recovered item",
                            summary="Found via fallback search.",
                            sources=["https://x.com/example/status/3"],
                        )
                    ],
                ),
                120,
                60,
            )
        )

        result, _, _ = client.generate_stock_digest("LMND", company_name="Lemonade")

        self.assertEqual(len(result.items), 1)
        self.assertEqual(client._collect_stock_posts.call_count, 2)
        client._curate_stock_posts.assert_not_called()
        client._recover_stock_digest.assert_called_once_with("LMND", "Lemonade")

    @patch("owly.run.GrokClient")
    @patch("owly.run.fetch_all_rss")
    def test_run_writes_edition_files(self, mock_fetch, mock_grok_cls) -> None:
        import owly.config as config_mod

        config_mod._settings = self.settings

        from owly.run import run_edition

        mock_fetch.return_value = [
            FeedItem(
                title="Sample",
                url="https://example.com/sample",
                published=datetime.now(),
                feed_label="Test",
                feed_url="https://example.com/feed",
                summary="Summary",
                full_text="Full text content",
            ),
            FeedItem(
                title="Skipped story",
                url="https://example.com/skipped",
                published=datetime.now(),
                feed_label="Test",
                feed_url="https://example.com/feed",
                summary="Not selected",
                full_text="This must remain unseen.",
            ),
        ]

        mock_client = mock_grok_cls.return_value
        mock_client.generate_main_digest.return_value = (
            DigestResult(
                items=[
                    DigestItem(
                        title="Headline",
                        summary="Body text.",
                        sources=["https://example.com/sample"],
                    )
                ]
            ),
            100,
            200,
        )
        mock_client.generate_stock_digest.return_value = (
            StockDigestResult(
                ticker="NVDA",
                items=[
                    StockItem(
                        title="NVDA news",
                        summary="Stock update.",
                        sources=["https://x.com/nvda"],
                    )
                ],
            ),
            50,
            80,
        )

        with get_db(self.settings.db_path) as conn:
            add_source(conn, "stock", "NVDA", "NVIDIA")

        result = run_edition(edition_slot="morning")
        self.assertEqual(result, 0)

        main_files = list(self.output_dir.glob("edition_*_morning.md"))
        stock_files = list(self.output_dir.glob("edition_*_morning_stocks.md"))
        self.assertEqual(len(main_files), 1)
        self.assertEqual(len(stock_files), 1)
        main_content = main_files[0].read_text(encoding="utf-8")
        self.assertIn("Headline", main_content)
        self.assertIn("Full text content", main_content)
        self.assertNotIn("Body text.", main_content)
        self.assertIn("## NVDA", stock_files[0].read_text(encoding="utf-8"))

        with get_db(self.settings.db_path) as conn:
            editions = list_editions(conn)
            self.assertTrue(is_seen(conn, "https://example.com/sample"))
            self.assertFalse(is_seen(conn, "https://example.com/skipped"))
        self.assertGreaterEqual(len(editions), 2)

    def test_published_sources_excludes_unselected_rss(self) -> None:
        digest = DigestResult(
            items=[
                DigestItem(
                    title="Picked",
                    summary="Body",
                    sources=["https://example.com/picked"],
                )
            ]
        )
        rows = published_sources(digest, [])
        self.assertEqual(rows, [("https://example.com/picked", "Picked")])

    def test_dedupe_digest_items_by_url(self) -> None:
        digest = DigestResult(
            items=[
                DigestItem(title="One", summary="a" * 50, sources=["https://example.com/a"]),
                DigestItem(title="Two", summary="b" * 50, sources=["https://example.com/a"]),
            ]
        )
        deduped = dedupe_digest_items(digest)
        self.assertEqual(len(deduped.items), 1)

    def test_main_digest_prompt_asks_for_rewrite_and_topic_mix(self) -> None:
        client = GrokClient.__new__(GrokClient)
        client.settings = self.settings
        client._call_structured = Mock(
            return_value=(
                DigestResult(
                    items=[
                        DigestItem(
                            title="T",
                            summary="S" * 50,
                            sources=["https://example.com/a"],
                        )
                    ]
                ),
                1,
                1,
            )
        )
        client.generate_main_digest("RSS HERE", ["AI agents", "chips"])
        args, kwargs = client._call_structured.call_args
        prompt = args[0]
        self.assertIn("6-12", prompt)
        self.assertIn("rewrite", prompt.lower())
        self.assertIn("at least one item per topic", prompt)
        self.assertIn("last 12 hours", prompt)
        tool_types = {tool["type"] for tool in kwargs["tools"]}
        self.assertEqual(tool_types, {"x_search", "web_search"})
        self.assertEqual(kwargs["max_output_tokens"], 16384)

    def test_stock_placeholder_keeps_guidance_with_no_significant(self) -> None:
        client = GrokClient.__new__(GrokClient)
        kept = StockItem(
            title="Guidance: no significant change in outlook",
            summary="CFO reiterated full-year guide.",
            sources=["https://x.com/example/status/1"],
        )
        dropped = StockItem(
            title="No news",
            summary="Quiet session.",
            sources=["https://x.com/example/status/2"],
        )
        self.assertFalse(client._is_placeholder(kept))
        self.assertTrue(client._is_placeholder(dropped))

    def test_stock_digest_passes_rss_context(self) -> None:
        client = GrokClient.__new__(GrokClient)
        client.settings = self.settings
        client._collect_stock_posts = Mock(
            return_value=("- https://x.com/example/status/1 $NVDA\n", 10, 5)
        )
        client._curate_stock_posts = Mock(
            side_effect=[
                (StockDigestResult(ticker="NVDA", items=[]), 8, 4),
                (
                    StockDigestResult(
                        ticker="NVDA",
                        items=[
                            StockItem(
                                title="From RSS",
                                summary="Earnings in the feed.",
                                sources=["https://example.com/nvda"],
                            )
                        ],
                    ),
                    8,
                    4,
                ),
            ]
        )
        client._recover_stock_digest = Mock()
        result, _, _ = client.generate_stock_digest(
            "NVDA",
            company_name="NVIDIA",
            rss_context="Title: NVDA earnings\n",
        )
        self.assertEqual(len(result.items), 1)
        for call in client._curate_stock_posts.call_args_list:
            self.assertEqual(call.kwargs.get("rss_context"), "Title: NVDA earnings\n")


if __name__ == "__main__":
    unittest.main()
