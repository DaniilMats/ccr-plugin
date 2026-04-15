from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from util import load_module


class TestCLIAdapters(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.codex_module = load_module("codex_adapter_module", "quality/scripts/llm-proxy/adapters/codex.py")
        cls.gemini_module = load_module("gemini_adapter_module", "quality/scripts/llm-proxy/adapters/gemini.py")

    def test_codex_adapter_uses_xhigh_reasoning_effort(self) -> None:
        adapter = self.codex_module.CodexAdapter()
        captured: dict[str, object] = {}

        def fake_run_subprocess(cmd, timeout, env=None, input_text=None):
            captured["cmd"] = list(cmd)
            captured["timeout"] = timeout
            captured["env"] = dict(env or {})
            return "ok", "", 0, False

        with patch.object(adapter, "_run_subprocess", side_effect=fake_run_subprocess):
            result = adapter.run(
                prompt="Review this diff",
                scope="file:internal/auth/jwt.go",
                thread_id="thread-123",
                timeout=42,
            )

        self.assertEqual(result.response, "ok")
        self.assertEqual(result.thread_id, "thread-123")
        self.assertEqual(captured["timeout"], 42)
        self.assertIn("model=gpt-5.4", captured["cmd"])
        self.assertIn("model_reasoning_effort=xhigh", captured["cmd"])
        self.assertEqual(captured["env"]["CODEX_THREAD_ID"], "thread-123")
        self.assertTrue(any(str(part).endswith(".json") for part in captured["cmd"]))

    def test_gemini_adapter_injects_high_thinking_alias_via_runtime_home(self) -> None:
        adapter = self.gemini_module.GeminiAdapter()
        captured: dict[str, object] = {}

        with tempfile.TemporaryDirectory() as source_root_tmp:
            source_root = Path(source_root_tmp)
            source_settings_dir = source_root / ".gemini"
            source_settings_dir.mkdir(parents=True)
            (source_settings_dir / "settings.json").write_text(
                json.dumps(
                    {
                        "model": {"name": "gemini-3.1-pro-preview"},
                        "ui": {"theme": "Ayu"},
                        "modelConfigs": {
                            "customAliases": {
                                "existing-alias": {
                                    "modelConfig": {
                                        "model": "gemini-2.5-flash",
                                    }
                                }
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            (source_settings_dir / "google_accounts.json").write_text("{}", encoding="utf-8")

            def fake_run_subprocess(cmd, timeout, env=None, input_text=None):
                captured["cmd"] = list(cmd)
                captured["timeout"] = timeout
                captured["env"] = dict(env or {})
                runtime_home = Path(captured["env"]["GEMINI_CLI_HOME"])
                runtime_settings = json.loads((runtime_home / ".gemini" / "settings.json").read_text(encoding="utf-8"))
                captured["runtime_settings"] = runtime_settings
                captured["copied_google_accounts"] = (runtime_home / ".gemini" / "google_accounts.json").is_file()
                return "ok", "", 0, False

            with patch.dict(self.gemini_module.os.environ, {"GEMINI_CLI_HOME": str(source_root)}, clear=False):
                with patch.object(adapter, "_run_subprocess", side_effect=fake_run_subprocess):
                    result = adapter.run(
                        prompt="Review this diff",
                        scope="file:internal/auth/jwt.go",
                        thread_id="session-456",
                        timeout=99,
                    )

        self.assertEqual(result.response, "ok")
        self.assertEqual(result.thread_id, "session-456")
        self.assertEqual(captured["timeout"], 99)
        self.assertEqual(captured["cmd"][0], "gemini")
        self.assertEqual(captured["cmd"][1:4], ["-m", adapter.DEFAULT_MODEL_ALIAS, "-p"])
        self.assertEqual(captured["env"]["GEMINI_SESSION_ID"], "session-456")
        self.assertTrue(captured["copied_google_accounts"])

        runtime_settings = captured["runtime_settings"]
        self.assertEqual(runtime_settings["ui"]["theme"], "Ayu")
        self.assertIn("existing-alias", runtime_settings["modelConfigs"]["customAliases"])
        alias_payload = runtime_settings["modelConfigs"]["customAliases"][adapter.DEFAULT_MODEL_ALIAS]
        self.assertEqual(alias_payload["modelConfig"]["model"], adapter.DEFAULT_MODEL)
        self.assertEqual(alias_payload["modelConfig"]["generateContentConfig"]["thinkingConfig"]["thinkingLevel"], "HIGH")
        self.assertEqual(alias_payload["modelConfig"]["generateContentConfig"]["temperature"], 1)
        self.assertEqual(alias_payload["modelConfig"]["generateContentConfig"]["topP"], 0.95)
        self.assertEqual(alias_payload["modelConfig"]["generateContentConfig"]["topK"], 64)


if __name__ == "__main__":
    unittest.main()
