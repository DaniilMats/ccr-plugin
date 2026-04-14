from __future__ import annotations

import unittest
from pathlib import Path

from util import FIXTURES_DIR, normalize_fixture_path, read_fixture, load_module


class TestReviewContext(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_module("review_context_module", "quality/scripts/llm-proxy/review_context.py")
        cls.fixture_repo = FIXTURES_DIR / "go_repo"
        cls.artifact_text = read_fixture("go_repo/review_artifact.txt")

    def test_build_review_context_matches_snapshot(self) -> None:
        context = self.module.build_review_context(str(self.fixture_repo), self.artifact_text)
        normalized = normalize_fixture_path(context, self.fixture_repo, "<FIXTURE_REPO>")
        expected = read_fixture("go_repo/expected_review_context.md").strip()
        self.assertEqual(normalized.strip(), expected)

    def test_missing_repo_gracefully_falls_back(self) -> None:
        context = self.module.build_review_context(str(self.fixture_repo / "missing"), self.artifact_text)
        self.assertIn("Repository/package context unavailable", context)
        self.assertIn("internal/auth/jwt.go", context)


if __name__ == "__main__":
    unittest.main()
