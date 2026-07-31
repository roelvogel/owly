"""Smoke tests for the authenticated JSON API."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from owly.config import Settings
from owly import dashboard
from owly.dashboard import app
from owly.db import init_db


class ApiTest(unittest.TestCase):
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
            owly_api_key="test-api-key",
        )
        init_db(self.settings.db_path)

        import owly.config as config_mod

        config_mod._settings = self.settings
        with dashboard._run_lock:
            dashboard._run_in_progress = False
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.tmp.cleanup()
        import owly.config as config_mod

        config_mod._settings = None
        with dashboard._run_lock:
            dashboard._run_in_progress = False

    def test_status_requires_api_key(self) -> None:
        response = self.client.get("/api/status")
        self.assertEqual(response.status_code, 401)

    def test_status_with_valid_key(self) -> None:
        response = self.client.get(
            "/api/status",
            headers={"X-Api-Key": "test-api-key"},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("run_in_progress", data)
        self.assertIn("latest_run", data)
        self.assertFalse(data["run_in_progress"])

    def test_editions_empty_list(self) -> None:
        response = self.client.get(
            "/api/editions",
            headers={"X-Api-Key": "test-api-key"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["editions"], [])

    def test_run_returns_started(self) -> None:
        with patch("owly.dashboard.start_run", return_value="started"):
            response = self.client.post(
                "/api/run",
                headers={"X-Api-Key": "test-api-key"},
                json={"edition": "morning"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "started")


if __name__ == "__main__":
    unittest.main()
