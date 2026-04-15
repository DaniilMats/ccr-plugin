from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from util import REPO_ROOT, load_module


class TestCCRRunInit(unittest.TestCase):
    def setUp(self) -> None:
        self.script = REPO_ROOT / "quality" / "scripts" / "ccr_run_init.py"
        self.validator = load_module("validator_module", "quality/scripts/llm-proxy/validator.py")
        self.schema = REPO_ROOT / "quality" / "contracts" / "v1" / "run_manifest.schema.json"

    def _run_init(self, base_dir: Path) -> dict:
        result = subprocess.run(
            ["python3", str(self.script), "--base-dir", str(base_dir)],
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(result.stdout)

    def test_run_init_creates_manifest_and_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = self._run_init(Path(tmp))
            self.assertEqual(manifest["contract_version"], "ccr.run_manifest.v1")
            self.assertTrue(Path(manifest["run_dir"]).is_dir())
            self.assertTrue(Path(manifest["manifest_file"]).is_file())
            self.assertEqual(manifest["contract_versions"]["route_plan"], "ccr.route_plan.v1")
            self.assertEqual(manifest["contract_versions"]["static_analysis"], "ccr.static_analysis.v1")
            self.assertEqual(manifest["contract_versions"]["review_prepare"], "ccr.review_prepare.v1")
            self.assertEqual(manifest["contract_versions"]["llm_invocation"], "ccr.llm_invocation.v1")
            self.assertEqual(manifest["contract_versions"]["reviewers_manifest"], "ccr.reviewers_manifest.v1")
            self.assertEqual(manifest["contract_versions"]["consolidated_candidate"], "ccr.consolidated_candidate.v1")
            self.assertTrue(Path(manifest["logs_dir"]).is_dir())
            self.assertTrue(Path(manifest["reviewer_results_dir"]).is_dir())
            self.assertTrue(Path(manifest["verifier_results_dir"]).is_dir())
            self.assertTrue(str(manifest["status_file"]).endswith("status.json"))
            self.assertTrue(str(manifest["trace_file"]).endswith("trace.jsonl"))
            self.assertTrue(str(manifest["summary_file"]).endswith("run_summary.json"))
            self.assertTrue(str(manifest["run_metrics_file"]).endswith("run_metrics.json"))
            self.assertTrue(str(manifest["watch_cursor_file"]).endswith("watch_cursor.json"))
            self.assertTrue(str(manifest["harness_stdout_file"]).endswith("harness.stdout.txt"))
            self.assertTrue(str(manifest["harness_stderr_file"]).endswith("harness.stderr.txt"))
            self.assertTrue(str(manifest["review_prepare_file"]).endswith("review_prepare.json"))
            self.assertTrue(str(manifest["verification_prepare_file"]).endswith("verification_prepare.json"))
            self.assertTrue(str(manifest["posting_approval_file"]).endswith("posting_approval.json"))
            self.assertTrue(str(manifest["posting_manifest_file"]).endswith("posting_manifest.json"))
            self.assertTrue(str(manifest["posting_results_file"]).endswith("posting_results.json"))
            self.assertEqual(manifest["contract_versions"]["run_status"], "ccr.run_status.v1")
            self.assertEqual(manifest["contract_versions"]["run_summary"], "ccr.run_summary.v1")
            self.assertEqual(manifest["contract_versions"]["run_launch"], "ccr.run_launch.v1")
            self.assertEqual(manifest["contract_versions"]["watch_result"], "ccr.watch_result.v1")
            self.assertEqual(manifest["contract_versions"]["candidates_manifest"], "ccr.candidates_manifest.v1")
            self.assertEqual(manifest["contract_versions"]["verification_prepare"], "ccr.verification_prepare.v1")
            self.assertEqual(manifest["contract_versions"]["verified_findings"], "ccr.verified_findings.v1")
            self.assertEqual(manifest["contract_versions"]["run_metrics"], "ccr.run_metrics.v1")
            self.assertEqual(manifest["contract_versions"]["posting_approval"], "ccr.posting_approval.v1")
            self.assertEqual(manifest["contract_versions"]["posting_manifest"], "ccr.posting_manifest.v1")
            self.assertEqual(manifest["contract_versions"]["posting_result"], "ccr.posting_result.v1")

            is_valid, violations = self.validator.validate_response(
                json.dumps(manifest), str(self.schema)
            )
            self.assertTrue(is_valid, violations)

    def test_run_init_generates_unique_run_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = self._run_init(Path(tmp))
            second = self._run_init(Path(tmp))
            self.assertNotEqual(first["run_id"], second["run_id"])
            self.assertNotEqual(first["run_dir"], second["run_dir"])


if __name__ == "__main__":
    unittest.main()
