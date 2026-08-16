"""RSS packing, clustering, and ticker matching tests."""

from __future__ import annotations

import unittest
from datetime import datetime

from owly.ingest import (
    FeedItem,
    cap_per_feed,
    cluster_feed_items,
    format_items_for_prompt,
    items_for_ticker,
    round_robin_by_feed,
)


def _item(title: str, url: str, feed: str, text: str = "body") -> FeedItem:
    return FeedItem(
        title=title,
        url=url,
        published=datetime(2026, 8, 16, 12, 0),
        feed_label=feed,
        feed_url=f"https://{feed.lower().replace(' ', '')}.example/rss",
        summary=text,
        full_text=text,
    )


class IngestPackTest(unittest.TestCase):
    def test_cap_per_feed(self) -> None:
        items = [_item(f"A{i}", f"https://a.example/{i}", "Ars") for i in range(10)]
        items += [_item(f"V{i}", f"https://v.example/{i}", "Verge") for i in range(3)]
        capped = cap_per_feed(items, cap=4)
        ars = [item for item in capped if item.feed_label == "Ars"]
        verge = [item for item in capped if item.feed_label == "Verge"]
        self.assertEqual(len(ars), 4)
        self.assertEqual(len(verge), 3)

    def test_cluster_similar_titles(self) -> None:
        items = [
            _item("NVIDIA announces new GPU", "https://ars.example/1", "Ars"),
            _item("Nvidia announces new GPU today", "https://verge.example/1", "Verge"),
            _item("Unrelated bond market story", "https://hn.example/2", "HN"),
        ]
        clusters = cluster_feed_items(items)
        self.assertEqual(len(clusters), 2)

    def test_round_robin_interleaves_feeds(self) -> None:
        items = [
            _item("A1", "https://a.example/1", "Ars"),
            _item("A2", "https://a.example/2", "Ars"),
            _item("V1", "https://v.example/1", "Verge"),
        ]
        order = [item.feed_label for item in round_robin_by_feed(items)]
        self.assertEqual(order, ["Ars", "Verge", "Ars"])

    def test_format_mentions_also_covered_by(self) -> None:
        items = [
            _item("Same chip story", "https://ars.example/1", "Ars", "long " * 20),
            _item("Same chip story", "https://verge.example/1", "Verge", "long " * 20),
        ]
        packed = format_items_for_prompt(items, max_chars=20000)
        self.assertIn("Also covered by: Verge", packed)
        self.assertIn("https://ars.example/1", packed)

    def test_items_for_ticker(self) -> None:
        items = [
            _item("NVDA earnings", "https://a.example/1", "Ars", "NVIDIA beat estimates"),
            _item("Unrelated", "https://a.example/2", "Ars", "Bonds rallied"),
        ]
        matches = items_for_ticker(items, "nvda")
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].title, "NVDA earnings")


if __name__ == "__main__":
    unittest.main()
