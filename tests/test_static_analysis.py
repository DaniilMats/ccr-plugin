from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from util import FIXTURES_DIR, REPO_ROOT, load_module


class TestStaticAnalysis(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_module("static_analysis_module", "quality/scripts/llm-proxy/static_analysis.py")
        cls.script = REPO_ROOT / "quality" / "scripts" / "llm-proxy" / "static_analysis.py"
        cls.fixture_repo = FIXTURES_DIR / "go_repo"

    def test_parse_outputs_and_filter_changed_files(self) -> None:
        project_dir = str(self.fixture_repo)
        go_vet = self.module._parse_go_vet(
            "internal/auth/jwt.go:24:2: missing check\ninternal/auth/jwt_test.go:7:1: flaky test\n",
            project_dir,
        )
        staticcheck = self.module._parse_staticcheck(
            "internal/auth/jwt.go:24:2: suspicious branch (SA1000)\n",
            project_dir,
        )
        gosec = self.module._parse_gosec(
            f"[{self.fixture_repo / 'internal' / 'auth' / 'jwt.go'}:24] - G101 (Potential hardcoded credentials)\n",
            project_dir,
        )

        self.assertEqual(go_vet[0]["file"], "internal/auth/jwt.go")
        self.assertEqual(go_vet[1]["file"], "internal/auth/jwt_test.go")
        self.assertEqual(staticcheck[0]["code"], "SA1000")
        self.assertEqual(gosec[0]["tool"], "gosec")
        self.assertEqual(gosec[0]["file"], "internal/auth/jwt.go")

        changed_set = self.module._build_changed_set(
            f"{self.fixture_repo / 'internal' / 'auth' / 'jwt.go'}, internal/auth/jwt_test.go",
            project_dir,
        )
        self.assertEqual(changed_set, {"internal/auth/jwt.go", "internal/auth/jwt_test.go"})

        filtered = self.module._filter_findings(go_vet + staticcheck + gosec, {"internal/auth/jwt.go"})
        self.assertEqual(len(filtered), 3)
        self.assertTrue(all(item["file"] == "internal/auth/jwt.go" for item in filtered))

    def test_run_analysis_filters_by_changed_files_and_tracks_tool_availability(self) -> None:
        project_dir = str(self.fixture_repo)

        def fake_is_available(executable: str) -> bool:
            return executable != "gosec"

        def fake_run_tool(cmd: list[str], cwd: str) -> tuple[int, str, str]:
            self.assertEqual(cwd, project_dir)
            if cmd[0] == "go":
                return 1, "", "internal/auth/jwt.go:24:2: missing check\ninternal/other/skip.go:8:1: ignore\n"
            if cmd[0] == "staticcheck":
                return 1, "internal/auth/jwt.go:24:2: suspicious branch (SA1000)\ninternal/other/skip.go:9:1: ignore (SA2000)\n", ""
            raise AssertionError(f"unexpected command: {cmd}")

        with patch.object(self.module, "_is_available", side_effect=fake_is_available), patch.object(
            self.module,
            "_run_tool",
            side_effect=fake_run_tool,
        ):
            result = self.module.run_analysis(project_dir, "internal/auth/jwt.go")

        self.assertTrue(result["tools_available"]["go_vet"])
        self.assertTrue(result["tools_available"]["staticcheck"])
        self.assertFalse(result["tools_available"]["gosec"])
        self.assertEqual(len(result["go_vet"]), 1)
        self.assertEqual(len(result["staticcheck"]), 1)
        self.assertEqual(result["go_vet"][0]["file"], "internal/auth/jwt.go")
        self.assertEqual(result["staticcheck"][0]["code"], "SA1000")
        self.assertEqual(result["categories"]["logic"], result["go_vet"])
        self.assertEqual(result["categories"]["security"], result["gosec"])
        self.assertEqual(result["categories"]["all"], result["staticcheck"])

    def test_cli_dry_run_writes_output_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_file = Path(tmp) / "static_analysis.json"
            result = subprocess.run(
                [
                    "python3",
                    str(self.script),
                    "--project-dir",
                    str(self.fixture_repo),
                    "--dry-run",
                    "--output-file",
                    str(output_file),
                ],
                capture_output=True,
                text=True,
                check=True,
            )

            payload = json.loads(result.stdout)
            written = json.loads(output_file.read_text(encoding="utf-8"))
            self.assertEqual(payload["contract_version"], "ccr.static_analysis.v1")
            self.assertTrue(payload["tools_available"]["go_vet"])
            self.assertEqual(payload["categories"]["logic"], [])
            self.assertEqual(written, payload)
            self.assertIn("Output written to", result.stderr)


if __name__ == "__main__":
    unittest.main()
