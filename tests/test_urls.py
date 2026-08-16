"""URL canonicalization tests."""

from __future__ import annotations

import unittest

from owly.urls import canonicalize_url


class UrlTest(unittest.TestCase):
    def test_strips_tracking_and_slash(self) -> None:
        self.assertEqual(
            canonicalize_url("https://www.Example.com/story/?utm_source=rss&utm_medium=feed"),
            "https://example.com/story",
        )

    def test_maps_twitter_to_x(self) -> None:
        self.assertEqual(
            canonicalize_url("https://twitter.com/foo/status/1/"),
            "https://x.com/foo/status/1",
        )

    def test_empty(self) -> None:
        self.assertEqual(canonicalize_url("  "), "")


if __name__ == "__main__":
    unittest.main()
