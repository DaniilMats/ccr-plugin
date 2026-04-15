from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from util import REPO_ROOT, read_fixture


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

    def test_eval_cli_scaffolds_cases_from_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            run_dir = tmp_path / "20260415T160000Z-1111-scaffold01"
            (run_dir / "reviewers").mkdir(parents=True)
            scaffold_root = tmp_path / "scaffolded-evals"

            (run_dir / "route_input.json").write_text(read_fixture("routing/route_input_small.json"), encoding="utf-8")
            (run_dir / "route_plan.json").write_text(read_fixture("routing/expected_route_plan_small.json"), encoding="utf-8")
            (run_dir / "static_analysis.json").write_text(
                json.dumps(
                    {
                        "contract_version": "ccr.static_analysis.v1",
                        "go_vet": [],
                        "staticcheck": [],
                        "gosec": [],
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (run_dir / "review_artifact.txt").write_text(read_fixture("go_repo/review_artifact.txt"), encoding="utf-8")
            (run_dir / "requirements.txt").write_text("ValidateToken must reject expired tokens.\n", encoding="utf-8")
            (run_dir / "verified_findings.json").write_text(
                json.dumps(
                    {
                        "contract_version": "ccr.verified_findings.v1",
                        "verified_findings": [
                            {
                                "finding_number": 1,
                                "candidate_id": "F1",
                                "persona": "security",
                                "severity": "bug",
                                "file": "internal/auth/jwt.go",
                                "line": 12,
                                "message": "ValidateToken skips expiry validation.",
                            }
                        ],
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (run_dir / "run_summary.json").write_text(
                json.dumps(
                    {
                        "contract_version": "ccr.run_summary.v1",
                        "run_id": run_dir.name,
                        "mode": "mr",
                        "target": "https://gitlab.com/group/project/-/merge_requests/42",
                        "run_dir": str(run_dir),
                        "project_dir": None,
                        "verified_finding_count": 1,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            reviewer_output = run_dir / "reviewers" / "security_p1.json"
            reviewer_output.write_text(
                json.dumps(
                    {
                        "contract_version": "ccr.reviewer_result.v1",
                        "summary": "security summary",
                        "findings": [
                            {
                                "severity": "bug",
                                "file": "internal/auth/jwt.go",
                                "line": 12,
                                "message": "ValidateToken skips expiry validation.",
                            }
                        ],
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (run_dir / "reviewers.json").write_text(
                json.dumps(
                    {
                        "contract_version": "ccr.reviewers_manifest.v1",
                        "passes": [
                            {
                                "pass_name": "security_p1",
                                "persona": "security",
                                "provider": "codex",
                                "output_file": str(reviewer_output),
                            }
                        ],
                        "summary": {
                            "planned_passes": 5,
                            "completed_passes": 5,
                            "succeeded_passes": 5,
                            "failed_passes": 0,
                            "total_findings": 1,
                        },
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    "python3",
                    str(self.script),
                    "--from-run",
                    str(run_dir),
                    "--suite",
                    "all",
                    "--case-name",
                    "scaffolded_auth",
                    "--scaffold-dir",
                    str(scaffold_root),
                ],
                capture_output=True,
                text=True,
                check=True,
            )

            payload = json.loads(result.stdout)
            self.assertEqual(payload["contract_version"], "ccr.eval_scaffold.v1")
            self.assertEqual(payload["case_count"], 4)
            self.assertTrue((scaffold_root / "routing_cases" / "scaffolded_auth" / "case.json").is_file())
            self.assertTrue((scaffold_root / "consolidation_cases" / "scaffolded_auth" / "reviewer_results.json").is_file())
            self.assertTrue((scaffold_root / "verification_prepare_cases" / "scaffolded_auth" / "expected.json").is_file())
            self.assertTrue((scaffold_root / "posting_cases" / "scaffolded_auth" / "case.json").is_file())


if __name__ == "__main__":
    unittest.main()
