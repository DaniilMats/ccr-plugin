from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from util import REPO_ROOT


class TestCCREval(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.script = REPO_ROOT / "quality" / "scripts" / "ccr_eval.py"

    def test_eval_cli_runs_single_routing_case(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "eval-results"
            result = subprocess.run(
                [
                    "python3",
                    str(self.script),
                    "--suite",
                    "routing",
                    "--case",
                    "small",
                    "--output-dir",
                    str(output_dir),
                ],
                capture_output=True,
                text=True,
                check=True,
            )

            payload = json.loads(result.stdout)
            written = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["contract_version"], "ccr.eval_summary.v1")
            self.assertEqual(payload["suite"], "routing")
            self.assertEqual(payload["case_count"], 1)
            self.assertEqual(payload["passed_count"], 1)
            self.assertEqual(payload["failed_count"], 0)
            self.assertEqual(payload, written)
            self.assertEqual(payload["cases"][0]["case"], "small")
            self.assertEqual(payload["cases"][0]["status"], "passed")

    def test_eval_cli_runs_all_suites(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "eval-results"
            result = subprocess.run(
                [
                    "python3",
                    str(self.script),
                    "--suite",
                    "all",
                    "--output-dir",
                    str(output_dir),
                ],
                capture_output=True,
                text=True,
                check=True,
            )

            payload = json.loads(result.stdout)
            self.assertEqual(payload["suite"], "all")
            self.assertEqual(payload["case_count"], 5)
            self.assertEqual(payload["passed_count"], 5)
            self.assertEqual(payload["failed_count"], 0)
            suites = {item["suite"] for item in payload["cases"]}
            self.assertEqual(suites, {"routing", "consolidation", "verification_prepare", "posting"})
            self.assertTrue((output_dir / "routing" / "small" / "actual.json").is_file())
            self.assertTrue((output_dir / "posting" / "mixed_outcomes" / "actual.json").is_file())


if __name__ == "__main__":
    unittest.main()
