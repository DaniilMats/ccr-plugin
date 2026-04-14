from __future__ import annotations

import unittest

from util import FIXTURES_DIR, load_module, normalize_fixture_path, read_fixture


class TestRepomap(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_module("repomap_module", "quality/scripts/repomap.py")
        cls.fixture_repo = FIXTURES_DIR / "go_repo"

    def test_render_markdown_matches_snapshot(self) -> None:
        rendered = self.module._render_markdown(self.fixture_repo, ["internal/auth/jwt.go"])
        normalized = normalize_fixture_path(rendered, self.fixture_repo, "<FIXTURE_REPO>")
        expected = read_fixture("go_repo/expected_repomap.md").strip()
        self.assertEqual(normalized.strip(), expected)


if __name__ == "__main__":
    unittest.main()
