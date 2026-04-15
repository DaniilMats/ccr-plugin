from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from util import FIXTURES_DIR, REPO_ROOT, load_module, read_fixture


class TestCCRRun(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_module("ccr_run_module", "quality/scripts/ccr_run.py")
        cls.fixture_repo = FIXTURES_DIR / "go_repo"
        cls.script = REPO_ROOT / "quality" / "scripts" / "ccr_run.py"
        cls.watch_script = REPO_ROOT / "quality" / "scripts" / "ccr_watch.py"

    def test_detect_review_target_normalizes_raw_go_file(self) -> None:
        file_path = self.fixture_repo / "internal" / "auth" / "jwt.go"
        target = self.module.detect_review_target(str(file_path))
        self.assertEqual(target.mode, "local")
        self.assertEqual(target.scope, f"file:{file_path}")

        relative_target = self.module.detect_review_target("internal/auth/jwt.go", cwd=self.fixture_repo)
        self.assertEqual(relative_target.mode, "local")
        self.assertEqual(relative_target.scope, f"file:{file_path}")

    def test_build_route_input_flags_auth_as_security_surface(self) -> None:
        payload = self.module.build_route_input(
            read_fixture("go_repo/review_artifact.txt"),
            requirements_text="",
            requirements_from_mr_description=False,
            user_requested_exhaustive=False,
            behavior_change_ambiguous=False,
        )
        self.assertEqual(payload["changed_files"], ["internal/auth/jwt.go"])
        self.assertEqual(payload["triggered_personas"], ["security"])
        self.assertEqual(payload["highest_risk_personas"], ["security"])
        self.assertEqual(payload["critical_surfaces"], ["auth"])

    def test_dry_run_end_to_end_writes_expected_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [
                    "python3",
                    str(self.script),
                    "package:internal/auth",
                    "--project-dir",
                    str(self.fixture_repo),
                    "--dry-run",
                    "--base-dir",
                    tmp,
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            summary = json.loads(result.stdout)
            self.assertEqual(summary["contract_version"], "ccr.run_summary.v1")
            self.assertEqual(summary["mode"], "local")
            self.assertEqual(summary["verified_finding_count"], 0)
            self.assertIn("Adaptive routing plan ready", result.stderr)
            self.assertIn("Launching reviewer passes", result.stderr)
            self.assertIn("CCR run completed", result.stderr)

            manifest = json.loads(Path(summary["manifest_file"]).read_text(encoding="utf-8"))
            self.assertTrue(Path(manifest["reviewers_file"]).is_file())
            self.assertTrue(Path(manifest["candidates_file"]).is_file())
            self.assertTrue(Path(manifest["verified_findings_file"]).is_file())
            self.assertTrue(Path(manifest["status_file"]).is_file())
            self.assertTrue(Path(manifest["trace_file"]).is_file())
            self.assertTrue(Path(manifest["summary_file"]).is_file())
            self.assertTrue(str(manifest["watch_cursor_file"]).endswith("watch_cursor.json"))
            self.assertTrue(str(manifest["harness_stdout_file"]).endswith("harness.stdout.txt"))
            self.assertTrue(str(manifest["harness_stderr_file"]).endswith("harness.stderr.txt"))
            self.assertTrue(str(manifest["verification_prepare_file"]).endswith("verification_prepare.json"))
            self.assertTrue(str(manifest["posting_approval_file"]).endswith("posting_approval.json"))
            self.assertTrue(str(manifest["posting_manifest_file"]).endswith("posting_manifest.json"))
            self.assertTrue(str(manifest["posting_results_file"]).endswith("posting_results.json"))
            self.assertEqual(
                Path(manifest["report_file"]).read_text(encoding="utf-8").strip(),
                "Проверенных замечаний не найдено.",
            )

            route_input = json.loads(Path(manifest["route_input_file"]).read_text(encoding="utf-8"))
            route_plan = json.loads(Path(manifest["route_plan_file"]).read_text(encoding="utf-8"))
            reviewers = json.loads(Path(manifest["reviewers_file"]).read_text(encoding="utf-8"))
            verification_prepare = json.loads(Path(manifest["verification_prepare_file"]).read_text(encoding="utf-8"))
            verified = json.loads(Path(manifest["verified_findings_file"]).read_text(encoding="utf-8"))
            status = json.loads(Path(manifest["status_file"]).read_text(encoding="utf-8"))
            written_summary = json.loads(Path(manifest["summary_file"]).read_text(encoding="utf-8"))
            trace_lines = [json.loads(line) for line in Path(manifest["trace_file"]).read_text(encoding="utf-8").splitlines() if line.strip()]
            trace_events = {entry["event"] for entry in trace_lines}

            self.assertEqual(route_input["triggered_personas"], ["security"])
            self.assertTrue(route_plan["full_matrix"])
            self.assertEqual(route_plan["total_passes"], 12)
            self.assertEqual(reviewers["summary"]["planned_passes"], 12)
            self.assertEqual(reviewers["summary"]["worker_count"], 12)
            self.assertEqual(reviewers["summary"]["failed_passes"], 0)
            self.assertEqual(verification_prepare["contract_version"], "ccr.verification_prepare.v1")
            self.assertEqual(verification_prepare["summary"]["candidate_count"], 0)
            self.assertEqual(verification_prepare["summary"]["ready_count"], 0)
            self.assertEqual(verification_prepare["summary"]["dropped_count"], 0)
            self.assertEqual(verification_prepare["summary"]["batch_count"], 0)
            self.assertEqual(verified["summary"]["verified_count"], 0)
            self.assertEqual(status["state"], "completed")
            self.assertGreaterEqual(status["revision"], 1)
            self.assertGreaterEqual(status["event_seq"], 1)
            self.assertEqual(status["summary"]["verified_finding_count"], 0)
            self.assertEqual(status["reviewers"]["planned"], 12)
            self.assertEqual(status["reviewers"]["workers"], 12)
            self.assertEqual(status["reviewers"]["completed"], 12)
            self.assertEqual(status["verification"]["planned_batches"], 0)
            self.assertEqual(written_summary["run_id"], summary["run_id"])
            self.assertEqual(written_summary["duration_ms"], summary["duration_ms"])
            self.assertTrue({"run_initialized", "route_plan_ready", "reviewers_started", "run_completed"}.issubset(trace_events))
            self.assertEqual(summary["verification_prepare_file"], manifest["verification_prepare_file"])
            self.assertEqual(summary["posting_approval_file"], manifest["posting_approval_file"])
            self.assertEqual(summary["posting_manifest_file"], manifest["posting_manifest_file"])
            self.assertEqual(summary["posting_results_file"], manifest["posting_results_file"])

    def test_merge_verified_findings_assigns_finding_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = self.module._build_manifest(Path(tmp), "test-run")
            candidates = [
                self.module.CandidateRecord(
                    candidate_id="F2",
                    persona="logic",
                    severity="warning",
                    file="internal/auth/jwt.go",
                    line=22,
                    message="Second finding.",
                    reviewers=["logic_p1", "logic_p2"],
                    consensus="2/2",
                    evidence_sources=["diff_hunk"],
                ),
                self.module.CandidateRecord(
                    candidate_id="F1",
                    persona="security",
                    severity="bug",
                    file="internal/auth/jwt.go",
                    line=12,
                    message="First finding.",
                    reviewers=["security_p1", "security_p2"],
                    consensus="2/2",
                    evidence_sources=["diff_hunk"],
                ),
            ]
            verification_results = [
                {
                    "batch_id": "batch-1",
                    "status": "succeeded",
                    "result": {
                        "verified_findings": [
                            {
                                "candidate_id": "F2",
                                "verdict": "confirmed",
                                "file": "internal/auth/jwt.go",
                                "line": 22,
                                "revised_message": "Second finding.",
                                "evidence": "Supported.",
                            },
                            {
                                "candidate_id": "F1",
                                "verdict": "confirmed",
                                "file": "internal/auth/jwt.go",
                                "line": 12,
                                "revised_message": "First finding.",
                                "evidence": "Supported.",
                            },
                        ]
                    },
                }
            ]

            merged = self.module._merge_verified_findings(
                manifest,
                candidates=candidates,
                verification_results=verification_results,
            )
            self.assertEqual([item["finding_number"] for item in merged], [1, 2])
            self.assertEqual([item["candidate_id"] for item in merged], ["F2", "F1"])
            written = json.loads(Path(manifest["verified_findings_file"]).read_text(encoding="utf-8"))
            self.assertEqual(written["verified_findings"][0]["finding_number"], 1)
            self.assertEqual(written["verified_findings"][1]["finding_number"], 2)
            report = self.module._format_report(merged)
            self.assertIn("1. [WARNING] internal/auth/jwt.go:22", report)
            self.assertIn("2. [BUG] internal/auth/jwt.go:12", report)

    def test_detach_launch_and_watch_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            launch_result = subprocess.run(
                [
                    "python3",
                    str(self.script),
                    "package:internal/auth",
                    "--project-dir",
                    str(self.fixture_repo),
                    "--dry-run",
                    "--base-dir",
                    tmp,
                    "--detach",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            launch = json.loads(launch_result.stdout)
            self.assertEqual(launch["contract_version"], "ccr.run_launch.v1")
            self.assertEqual(launch["mode"], "local")
            self.assertFalse(launch["done"])
            self.assertTrue(Path(launch["manifest_file"]).is_file())
            self.assertTrue(str(launch["verification_prepare_file"]).endswith("verification_prepare.json"))
            self.assertTrue(Path(launch["harness_stdout_file"]).is_file())
            self.assertTrue(Path(launch["harness_stderr_file"]).is_file())

            cursor_file = Path(launch["watch_cursor_file"])
            watch_payload = None
            all_display_lines: list[str] = []
            for _ in range(10):
                watch_result = subprocess.run(
                    [
                        "python3",
                        str(self.watch_script),
                        "--status-file",
                        launch["status_file"],
                        "--trace-file",
                        launch["trace_file"],
                        "--pid",
                        str(launch["pid"]),
                        "--cursor-file",
                        str(cursor_file),
                        "--wait-seconds",
                        "2",
                        "--emit-heartbeat",
                    ],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                watch_payload = json.loads(watch_result.stdout)
                self.assertEqual(watch_payload["contract_version"], "ccr.watch_result.v1")
                all_display_lines.extend(watch_payload["display_lines"])
                if watch_payload["done"]:
                    break
            self.assertIsNotNone(watch_payload)
            self.assertTrue(watch_payload["done"], watch_payload)
            self.assertEqual(watch_payload["state"], "completed")
            self.assertTrue(any("▶ Reviewers" in line for line in all_display_lines))

            final_watch = subprocess.run(
                [
                    "python3",
                    str(self.watch_script),
                    "--status-file",
                    launch["status_file"],
                    "--trace-file",
                    launch["trace_file"],
                    "--cursor-file",
                    str(cursor_file),
                    "--quiet-unchanged",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(final_watch.stdout.strip(), "")

            follow_text = subprocess.run(
                [
                    "python3",
                    str(self.watch_script),
                    "--status-file",
                    launch["status_file"],
                    "--trace-file",
                    launch["trace_file"],
                    "--pid",
                    str(launch["pid"]),
                    "--cursor-file",
                    str(cursor_file),
                    "--format",
                    "text",
                    "--follow",
                    "--wait-seconds",
                    "1",
                    "--quiet-unchanged",
                    "--emit-heartbeat",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(follow_text.stdout.strip(), "")

            summary = json.loads(Path(launch["summary_file"]).read_text(encoding="utf-8"))
            status = json.loads(Path(launch["status_file"]).read_text(encoding="utf-8"))
            self.assertEqual(summary["contract_version"], "ccr.run_summary.v1")
            self.assertEqual(summary["run_id"], launch["run_id"])
            self.assertTrue(summary["detached"])
            self.assertEqual(status["state"], "completed")
            self.assertTrue(status["detached"])


if __name__ == "__main__":
    unittest.main()
