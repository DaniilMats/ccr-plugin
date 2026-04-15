from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from util import load_module


class TestBaseAdapter(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_module("base_adapter_module", "quality/scripts/llm-proxy/adapters/base.py")

    def _adapter(self):
        module = self.module

        class DummyAdapter(module.BaseAdapter):
            def run(self, prompt: str, scope: str | None = None, thread_id: str | None = None, timeout: int = 300):
                return module.ProxyResponse(response=prompt, thread_id=thread_id)

        return DummyAdapter("")

    def test_proxy_response_to_dict_includes_schema_violations_only_when_present(self) -> None:
        response = self.module.ProxyResponse(response="ok", schema_retries=1)
        self.assertNotIn("schema_violations", response.to_dict())

        response.schema_violations = ["bad schema"]
        self.assertEqual(response.to_dict()["schema_violations"], ["bad schema"])

    def test_build_scope_context_formats_known_scopes(self) -> None:
        adapter = self._adapter()
        self.assertEqual(adapter._build_scope_context("commit:abc123"), "[Review scope: git commit abc123]\n\n")
        self.assertEqual(adapter._build_scope_context("branch:main"), "[Review scope: git diff from branch base main]\n\n")
        self.assertEqual(adapter._build_scope_context("uncommitted"), "[Review scope: uncommitted changes (git diff HEAD)]\n\n")
        self.assertEqual(adapter._build_scope_context("file:internal/auth/jwt.go"), "[Review scope: file internal/auth/jwt.go]\n\n")
        self.assertEqual(adapter._build_scope_context(None), "")

    def test_run_subprocess_returns_not_found_for_missing_command(self) -> None:
        adapter = self._adapter()
        stdout, stderr, returncode, timed_out = adapter._run_subprocess(["definitely-not-a-real-command"], timeout=1)
        self.assertEqual(stdout, "")
        self.assertEqual(returncode, 127)
        self.assertFalse(timed_out)
        self.assertIn("No such file", stderr)

    def test_save_and_load_thread_id_round_trip(self) -> None:
        adapter = self._adapter()
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp) / "threads"
            with patch.object(self.module.os.path, "expanduser", return_value=str(base_dir)):
                adapter._save_thread_id("session-1", "slug-1", "codex", "thread-123")
                loaded = adapter._load_thread_id("session-1", "slug-1", "codex")

        self.assertEqual(loaded, "thread-123")


if __name__ == "__main__":
    unittest.main()
