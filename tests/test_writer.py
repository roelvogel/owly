"""Cursor writer unit tests (no live agent)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from owly.config import Settings
from owly.models import DigestResult
from owly.structured import parse_structured
from owly.writer import CursorWriter


class WriterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.settings = Settings(
            xai_api_key="test-key",
            cursor_api_key="test-cursor",
            output_dir=root / "editions",
            data_dir=root / "data",
        )
        self.settings.output_dir.mkdir()
        self.settings.data_dir.mkdir()
        import owly.config as config_mod

        config_mod._settings = self.settings

    def tearDown(self) -> None:
        self.tmp.cleanup()
        import owly.config as config_mod

        config_mod._settings = None

    def test_parse_structured_strips_fences(self) -> None:
        raw = """```json
{"items": [{"title": "Hello", "summary": "Body text for the item.", "sources": ["https://example.com/a"], "origin": "rss"}]}
```"""
        parsed = parse_structured(raw, DigestResult)
        self.assertEqual(parsed.items[0].title, "Hello")

    def test_write_main_digest_uses_prompt_fn(self) -> None:
        payload = (
            '{"items": [{"title": "From Cursor", "summary": "'
            + ("word " * 80)
            + '", "sources": ["https://example.com/a"], "origin": "rss"}]}'
        )

        def fake_prompt(message: str):
            self.assertIn("Do not search the web or X", message)
            self.assertIn("Never write a full article from a headline", message)
            self.assertIn("RSS HERE", message)
            self.assertIn("https://x.com/a/status/1", message)
            self.assertIn("6-12", message)
            return payload, 11, 22

        writer = CursorWriter(prompt_fn=fake_prompt)
        result, in_tok, out_tok = writer.write_main_digest(
            "RSS HERE",
            ["AI agents"],
            "- https://x.com/a/status/1 hello",
            "the last 12 hours",
        )
        self.assertEqual(result.items[0].title, "From Cursor")
        self.assertEqual(in_tok, 11)
        self.assertEqual(out_tok, 22)


if __name__ == "__main__":
    unittest.main()
