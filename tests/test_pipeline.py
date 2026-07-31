"""Integration tests that do not require a live xAI API key."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from owly.config import Settings, get_settings
from owly.db import add_source, get_db, init_db, list_editions
from owly.dedupe import dedupe_stock_results
from owly.grok import GrokClient
from owly.models import DigestItem, DigestResult, StockDigestResult, StockItem
from owly.render import render_combined_stocks_edition, render_main_edition


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
        self.assertIn("## 1. AI breakthrough", main_md)
        self.assertNotIn("https://example.com/ai", main_md)
        self.assertNotIn("Why it matters", main_md)
        self.assertNotIn("**Sources**", main_md)
        self.assertIn("## Stocks", main_md)
        self.assertIn("# Stocks", stocks_md)
        self.assertIn("## NVDA", stocks_md)
        self.assertIn("### 1. NVDA earnings chatter", stocks_md)
        self.assertNotIn("emoji", main_md)

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

        from owly.ingest import FeedItem
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
            )
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
        self.assertIn("Headline", main_files[0].read_text(encoding="utf-8"))
        self.assertIn("## NVDA", stock_files[0].read_text(encoding="utf-8"))

        with get_db(self.settings.db_path) as conn:
            editions = list_editions(conn)
        self.assertGreaterEqual(len(editions), 2)


if __name__ == "__main__":
    unittest.main()
