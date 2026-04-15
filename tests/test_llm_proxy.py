from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from util import REPO_ROOT, load_module


class FakeAdapter:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def run(self, prompt: str, scope: str | None = None, thread_id: str | None = None, timeout: int = 300):
        self.calls.append(
            {
                "prompt": prompt,
                "scope": scope,
                "thread_id": thread_id,
                "timeout": timeout,
            }
        )
        return self.responses.pop(0)


class TestLLMProxy(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_module("llm_proxy_module", "quality/scripts/llm-proxy/llm_proxy.py")

    def test_run_proxy_dry_run_writes_output_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_file = Path(tmp) / "proxy.json"
            result = self.module.run_proxy(
                prompt="Review this diff",
                provider="codex",
                dry_run=True,
                output_file=str(output_file),
            )

            written = json.loads(output_file.read_text(encoding="utf-8"))
            self.assertEqual(result["exit_code"], 0)
            self.assertTrue(result["schema_valid"])
            self.assertEqual(result["schema_retries"], 0)
            self.assertIn("[dry-run]", result["response"])
            self.assertEqual(written, result)

    def test_run_proxy_rejects_unknown_provider(self) -> None:
        result = self.module.run_proxy(prompt="hi", provider="unknown-provider")
        self.assertEqual(result["exit_code"], 1)
        self.assertIn("Unknown provider", result["error"])

    def test_run_proxy_retries_schema_and_preserves_thread_id(self) -> None:
        schema = {
            "type": "object",
            "required": ["status"],
            "properties": {
                "status": {"type": "string"},
            },
        }

        with tempfile.TemporaryDirectory() as tmp:
            schema_path = Path(tmp) / "schema.json"
            schema_path.write_text(json.dumps(schema), encoding="utf-8")

            fake = FakeAdapter(
                [
                    self.module.ProxyResponse(
                        response=json.dumps({"missing": True}),
                        thread_id="thread-1",
                        exit_code=0,
                    ),
                    self.module.ProxyResponse(
                        response=json.dumps({"status": "ok"}),
                        thread_id=None,
                        exit_code=0,
                    ),
                ]
            )

            with patch.object(self.module, "_build_adapter", return_value=fake):
                result = self.module.run_proxy(
                    prompt="Original request",
                    provider="codex",
                    scope="file:internal/auth/jwt.go",
                    timeout=30,
                    response_schema=str(schema_path),
                )

        self.assertEqual(result["exit_code"], 0)
        self.assertTrue(result["schema_valid"])
        self.assertEqual(result["schema_retries"], 1)
        self.assertEqual(result["thread_id"], "thread-1")
        self.assertEqual(result["response"], json.dumps({"status": "ok"}))
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(fake.calls[0]["scope"], "file:internal/auth/jwt.go")
        self.assertIsNone(fake.calls[1]["scope"])
        self.assertIn("did not match the required schema", fake.calls[1]["prompt"])
        self.assertIn("Original request", fake.calls[1]["prompt"])
        self.assertIn("missing required field 'status'", fake.calls[1]["prompt"])

    def test_run_proxy_returns_schema_read_error_without_invoking_provider(self) -> None:
        fake = FakeAdapter([])
        with patch.object(self.module, "_build_adapter", return_value=fake):
            result = self.module.run_proxy(
                prompt="Original request",
                provider="codex",
                response_schema="/definitely/missing/schema.json",
            )

        self.assertEqual(result["exit_code"], 1)
        self.assertIn("Cannot read schema file", result["error"])
        self.assertEqual(fake.calls, [])


if __name__ == "__main__":
    unittest.main()
