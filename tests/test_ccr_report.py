from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from util import REPO_ROOT


class TestCCRReport(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.script = REPO_ROOT / "quality" / "scripts" / "ccr_report.py"

    def _write_json(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def test_text_report_highlights_plan_mix_and_anomalies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "20260415T150000Z-1234-abcd1234"
            run_dir.mkdir(parents=True)
            summary_file = run_dir / "run_summary.json"
            metrics_file = run_dir / "run_metrics.json"
            reviewers_file = run_dir / "reviewers.json"
            verification_prepare_file = run_dir / "verification_prepare.json"
            posting_results_file = run_dir / "posting_results.json"
            verified_findings_file = run_dir / "verified_findings.json"
            status_file = run_dir / "status.json"

            self._write_json(
                summary_file,
                {
                    "contract_version": "ccr.run_summary.v1",
                    "run_id": run_dir.name,
                    "mode": "mr",
                    "target": "https://gitlab.com/group/project/-/merge_requests/42",
                    "run_dir": str(run_dir),
                    "summary_file": str(summary_file),
                    "run_metrics_file": str(metrics_file),
                    "reviewers_file": str(reviewers_file),
                    "verification_prepare_file": str(verification_prepare_file),
                    "verified_findings_file": str(verified_findings_file),
                    "posting_results_file": str(posting_results_file),
                    "review_plan_summary": "Review plan: medium-risk MR → Logic x3, Security x2",
                    "verified_finding_count": 1,
                    "duration_ms": 123456,
                },
            )
            self._write_json(
                status_file,
                {
                    "contract_version": "ccr.run_status.v1",
                    "run_id": run_dir.name,
                    "state": "completed",
                    "duration_ms": 123456,
                    "current_stage": {
                        "name": "completed",
                        "status": "completed",
                    },
                },
            )
            self._write_json(
                metrics_file,
                {
                    "contract_version": "ccr.run_metrics.v1",
                    "run_id": run_dir.name,
                    "generated_at": "2026-04-15T15:00:00Z",
                    "mode": "mr",
                    "target": "https://gitlab.com/group/project/-/merge_requests/42",
                    "requirements": {"source": "mr_description", "has_requirements": True, "requirements_chars": 120},
                    "route": {
                        "summary": "Review plan: medium-risk MR → Logic x3, Security x2",
                        "total_passes": 5,
                        "full_matrix": False,
                        "pass_counts": {"logic": 3, "security": 2, "concurrency": 0, "performance": 0, "requirements": 0},
                    },
                    "reviewers": {
                        "planned_passes": 5,
                        "completed_passes": 5,
                        "succeeded_passes": 5,
                        "failed_passes": 0,
                        "total_findings": 4,
                        "total_tokens": 1500,
                        "schema_retry_count": 2,
                        "provider_breakdown": {
                            "gemini": {"call_count": 2},
                            "codex": {"call_count": 2},
                            "claude": {"call_count": 1},
                        },
                    },
                    "candidates": {
                        "candidate_count": 3,
                        "source_finding_count": 4,
                    },
                    "verification": {
                        "ready_count": 2,
                        "confirmed_count": 1,
                        "uncertain_count": 0,
                        "rejected_count": 1,
                        "verified_count": 1,
                        "batch_count": 1,
                        "successful_batches": 1,
                        "failed_batches": 0,
                        "rejection_rate": 0.5,
                        "anchor_failure_rate": 0.0,
                    },
                    "llm": {
                        "total_calls": 6,
                        "total_tokens": 1500,
                        "schema_retry_count": 2,
                        "timed_out_calls": 0,
                        "failed_calls": 1,
                        "provider_breakdown": {
                            "gemini": {"call_count": 2},
                            "codex": {"call_count": 3},
                            "claude": {"call_count": 1},
                        },
                    },
                    "posting": {
                        "posting_supported": True,
                    },
                },
            )
            self._write_json(
                reviewers_file,
                {
                    "contract_version": "ccr.reviewers_manifest.v1",
                    "passes": [],
                    "summary": {
                        "planned_passes": 5,
                        "completed_passes": 5,
                        "succeeded_passes": 5,
                        "failed_passes": 0,
                        "total_findings": 4,
                        "total_tokens": 1500,
                        "schema_retry_count": 2,
                    },
                },
            )
            self._write_json(
                verification_prepare_file,
                {
                    "contract_version": "ccr.verification_prepare.v1",
                    "summary": {
                        "candidate_count": 3,
                        "ready_count": 2,
                        "dropped_count": 1,
                        "batch_count": 1,
                    },
                },
            )
            self._write_json(
                posting_results_file,
                {
                    "contract_version": "ccr.posting_result.v1",
                    "summary": {
                        "posted_count": 1,
                        "already_posted_count": 0,
                        "failed_count": 1,
                        "missing_anchor_count": 1,
                    },
                },
            )
            self._write_json(
                verified_findings_file,
                {
                    "contract_version": "ccr.verified_findings.v1",
                    "verified_findings": [{"finding_number": 1}],
                },
            )

            result = subprocess.run(
                ["python3", str(self.script), "--run-dir", str(run_dir), "--format", "text"],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertIn("Plan: Review plan: medium-risk MR → Logic x3, Security x2", result.stdout)
            self.assertIn("Reviewer mix: Logic x3, Security x2", result.stdout)
            self.assertIn("Funnel: reviewers 5/5 · findings=4 · candidates=3 · ready=2 · verified=1 · posted=1", result.stdout)
            self.assertIn("Providers: claude x1, codex x3, gemini x2", result.stdout)
            self.assertIn("- schema retries=2", result.stdout)
            self.assertIn("- llm failed calls=1", result.stdout)
            self.assertIn("- posting failed=1", result.stdout)

    def test_json_report_works_from_summary_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            run_dir.mkdir(parents=True)
            summary_file = run_dir / "run_summary.json"
            metrics_file = run_dir / "run_metrics.json"

            self._write_json(
                summary_file,
                {
                    "contract_version": "ccr.run_summary.v1",
                    "run_id": "run-1",
                    "mode": "local",
                    "target": "package:internal/auth",
                    "run_dir": str(run_dir),
                    "summary_file": str(summary_file),
                    "run_metrics_file": str(metrics_file),
                    "verified_finding_count": 0,
                },
            )
            self._write_json(
                metrics_file,
                {
                    "contract_version": "ccr.run_metrics.v1",
                    "run_id": "run-1",
                    "generated_at": "2026-04-15T15:00:00Z",
                    "mode": "local",
                    "target": "package:internal/auth",
                    "requirements": {"source": "inline", "has_requirements": True, "requirements_chars": 20},
                    "route": {"summary": "Review plan: high-risk MR → Logic x3", "total_passes": 3, "full_matrix": False, "pass_counts": {"logic": 3}},
                    "reviewers": {"planned_passes": 3, "completed_passes": 3, "succeeded_passes": 3, "failed_passes": 0, "total_findings": 0},
                    "candidates": {"candidate_count": 0, "source_finding_count": 0},
                    "verification": {"verified_count": 0, "batch_count": 0, "successful_batches": 0, "failed_batches": 0},
                    "posting": {"posting_supported": False},
                },
            )

            result = subprocess.run(
                ["python3", str(self.script), "--summary-file", str(summary_file), "--format", "json"],
                capture_output=True,
                text=True,
                check=True,
            )
            payload = json.loads(result.stdout)
            self.assertEqual(payload["contract_version"], "ccr.run_report.v1")
            self.assertEqual(payload["run_id"], "run-1")
            self.assertEqual(payload["funnel"]["planned_reviewers"], 3)
            self.assertEqual(payload["funnel"]["verified_count"], 0)
            self.assertEqual(payload["reviewer_mix"], "Logic x3")


if __name__ == "__main__":
    unittest.main()
