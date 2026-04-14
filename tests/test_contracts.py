from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from util import FIXTURES_DIR, REPO_ROOT, load_module, read_fixture


class TestContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.validator = load_module("validator_module_contracts", "quality/scripts/llm-proxy/validator.py")
        cls.run_init = load_module("ccr_run_init_module", "quality/scripts/ccr_run_init.py")
        cls.routing = load_module("ccr_routing_contracts", "quality/scripts/ccr_routing.py")
        cls.static_analysis = load_module("static_analysis_contracts", "quality/scripts/llm-proxy/static_analysis.py")
        cls.schemas_dir = REPO_ROOT / "quality" / "contracts" / "v1"

    def _assert_valid(self, payload: dict, schema_name: str) -> None:
        schema_path = self.schemas_dir / schema_name
        is_valid, violations = self.validator.validate_response(json.dumps(payload), str(schema_path))
        self.assertTrue(is_valid, f"{schema_name}: {violations}")

    def test_all_v1_schemas_exist(self) -> None:
        expected = {
            "run_manifest.schema.json",
            "route_input.schema.json",
            "route_plan.schema.json",
            "run_status.schema.json",
            "run_summary.schema.json",
            "run_launch.schema.json",
            "watch_result.schema.json",
            "static_analysis.schema.json",
            "reviewer_result.schema.json",
            "consolidated_candidate.schema.json",
            "verification_batch.schema.json",
            "verification_result.schema.json",
            "posting_manifest.schema.json",
        }
        existing = {path.name for path in self.schemas_dir.glob("*.json")}
        self.assertTrue(expected.issubset(existing))

    def test_run_manifest_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = self.run_init._build_manifest(Path(tmp), "test-run")
            self._assert_valid(manifest, "run_manifest.schema.json")

    def test_route_input_and_plan_contracts(self) -> None:
        route_input = json.loads(read_fixture("routing/route_input_small.json"))
        self._assert_valid(route_input, "route_input.schema.json")
        plan = self.routing.build_routing_plan(self.routing.RoutingInput.model_validate(route_input)).model_dump()
        self._assert_valid(plan, "route_plan.schema.json")

    def test_run_status_contract(self) -> None:
        payload = {
            "contract_version": "ccr.run_status.v1",
            "run_id": "20260414T220000Z-1234-abcd1234",
            "pid": 12345,
            "detached": True,
            "revision": 7,
            "event_seq": 5,
            "state": "running",
            "started_at": "2026-04-14T22:00:00Z",
            "updated_at": "2026-04-14T22:00:07Z",
            "heartbeat_at": "2026-04-14T22:00:07Z",
            "finished_at": None,
            "duration_ms": None,
            "current_stage": {
                "name": "reviewers",
                "status": "running",
                "message": "Running reviewer passes",
                "started_at": "2026-04-14T22:01:00Z",
                "ended_at": None,
                "duration_ms": None,
                "index": 7,
                "total": 10
            },
            "stages": {
                "routing": {
                    "name": "routing",
                    "status": "completed"
                }
            },
            "reviewers": {
                "planned": 12,
                "workers": 12,
                "timeout_sec": 600,
                "running": 8,
                "completed": 4,
                "succeeded": 4,
                "failed": 0,
                "estimated_max_duration_sec": 600,
                "passes": {
                    "logic_p1": {
                        "status": "completed"
                    }
                }
            },
            "verification": {
                "planned_batches": 0,
                "workers": 0,
                "timeout_sec": 300,
                "running_batches": 0,
                "completed_batches": 0,
                "succeeded_batches": 0,
                "failed_batches": 0,
                "estimated_max_duration_sec": 0,
                "batches": {}
            },
            "artifacts": {
                "run_dir": "/tmp/ccr/run",
                "trace_file": "/tmp/ccr/run/trace.jsonl",
                "status_file": "/tmp/ccr/run/status.json"
            },
            "summary": {},
            "last_event": None,
            "error": None
        }
        self._assert_valid(payload, "run_status.schema.json")

    def test_run_summary_contract(self) -> None:
        payload = {
            "contract_version": "ccr.run_summary.v1",
            "run_id": "20260414T220000Z-1234-abcd1234",
            "mode": "local",
            "target": "package:internal/auth",
            "project_dir": "/tmp/repo",
            "run_dir": "/tmp/ccr/run",
            "manifest_file": "/tmp/ccr/run/run_manifest.json",
            "status_file": "/tmp/ccr/run/status.json",
            "trace_file": "/tmp/ccr/run/trace.jsonl",
            "summary_file": "/tmp/ccr/run/run_summary.json",
            "harness_stdout_file": "/tmp/ccr/run/logs/harness.stdout.txt",
            "harness_stderr_file": "/tmp/ccr/run/logs/harness.stderr.txt",
            "pid": 12345,
            "detached": True,
            "report_file": "/tmp/ccr/run/report.md",
            "reviewers_file": "/tmp/ccr/run/reviewers.json",
            "candidates_file": "/tmp/ccr/run/candidates.json",
            "verified_findings_file": "/tmp/ccr/run/verified_findings.json",
            "review_plan_summary": "Review plan: medium-risk MR → Logic x3, Security x2",
            "reviewer_worker_count": 5,
            "verifier_worker_count": 1,
            "reviewer_timeout_sec": 600,
            "verifier_timeout_sec": 300,
            "duration_ms": 1234,
            "verified_finding_count": 0,
            "report_preview": ["Проверенных замечаний не найдено."]
        }
        self._assert_valid(payload, "run_summary.schema.json")

    def test_run_launch_contract(self) -> None:
        payload = {
            "contract_version": "ccr.run_launch.v1",
            "run_id": "20260414T220000Z-1234-abcd1234",
            "pid": 12345,
            "mode": "mr",
            "target": "https://gitlab.com/group/project/-/merge_requests/1",
            "project_dir": "/tmp/repo",
            "run_dir": "/tmp/ccr/run",
            "manifest_file": "/tmp/ccr/run/run_manifest.json",
            "status_file": "/tmp/ccr/run/status.json",
            "trace_file": "/tmp/ccr/run/trace.jsonl",
            "summary_file": "/tmp/ccr/run/run_summary.json",
            "report_file": "/tmp/ccr/run/report.md",
            "reviewers_file": "/tmp/ccr/run/reviewers.json",
            "candidates_file": "/tmp/ccr/run/candidates.json",
            "verified_findings_file": "/tmp/ccr/run/verified_findings.json",
            "harness_stdout_file": "/tmp/ccr/run/logs/harness.stdout.txt",
            "harness_stderr_file": "/tmp/ccr/run/logs/harness.stderr.txt",
            "state": "launched",
            "done": False,
            "launched_at": "2026-04-14T22:00:00Z"
        }
        self._assert_valid(payload, "run_launch.schema.json")

    def test_watch_result_contract(self) -> None:
        payload = {
            "contract_version": "ccr.watch_result.v1",
            "run_id": "20260414T220000Z-1234-abcd1234",
            "state": "running",
            "done": False,
            "changed": True,
            "pid": 12345,
            "revision": 4,
            "since_seq": 2,
            "last_seq": 4,
            "current_stage": {
                "name": "reviewers",
                "status": "running",
                "index": 7,
                "total": 10
            },
            "reviewers": {
                "planned": 14,
                "completed": 4
            },
            "verification": {},
            "summary": {},
            "artifacts": {
                "status_file": "/tmp/ccr/run/status.json"
            },
            "new_events": [
                {
                    "seq": 3,
                    "event": "reviewer_completed"
                }
            ],
            "display_lines": [
                "Run 20260414T220000Z-1234-abcd1234: state=running, stage=[7/10] reviewers"
            ],
            "next_poll_sec": 10
        }
        self._assert_valid(payload, "watch_result.schema.json")

    def test_static_analysis_contract(self) -> None:
        payload = self.static_analysis.empty_result()
        self._assert_valid(payload, "static_analysis.schema.json")

    def test_reviewer_result_contract(self) -> None:
        payload = {
            "contract_version": "ccr.reviewer_result.v1",
            "findings": [
                {
                    "severity": "warning",
                    "file": "internal/auth/jwt.go",
                    "line": 12,
                    "message": "Example reviewer message.",
                }
            ],
            "summary": "Example summary.",
            "raw_response": "{}",
        }
        self._assert_valid(payload, "reviewer_result.schema.json")

    def test_consolidated_candidate_contract(self) -> None:
        payload = {
            "contract_version": "ccr.consolidated_candidate.v1",
            "candidate_id": "F1",
            "persona": "security",
            "file": "internal/auth/jwt.go",
            "line": 12,
            "message": "Example candidate.",
            "severity": "bug",
            "reviewers": ["security_p1", "security_p2"],
            "consensus": "2/2",
            "evidence_sources": ["diff_hunk", "gosec"],
        }
        self._assert_valid(payload, "consolidated_candidate.schema.json")

    def test_verification_batch_contract(self) -> None:
        payload = {
            "contract_version": "ccr.verification_batch.v1",
            "file": "internal/auth/jwt.go",
            "diff_hunk": "@@ -1,1 +1,2 @@",
            "file_context": "package auth",
            "requirements": "",
            "candidates": [
                {
                    "candidate_id": "F1",
                    "file": "internal/auth/jwt.go",
                    "line": 12,
                    "message": "Example candidate.",
                }
            ],
        }
        self._assert_valid(payload, "verification_batch.schema.json")

    def test_verification_result_contract(self) -> None:
        payload = {
            "contract_version": "ccr.verification_result.v1",
            "verified_findings": [
                {
                    "candidate_id": "F1",
                    "verdict": "confirmed",
                    "file": "internal/auth/jwt.go",
                    "line": 12,
                    "revised_message": "Tightened message.",
                    "evidence": "Supported by the provided diff.",
                }
            ],
            "summary": "One finding confirmed.",
            "raw_response": "{}",
        }
        self._assert_valid(payload, "verification_result.schema.json")

    def test_posting_manifest_contract(self) -> None:
        payload = {
            "contract_version": "ccr.posting_manifest.v1",
            "project": "group/project",
            "mr_iid": 123,
            "approved_findings": [
                {
                    "finding_number": 1,
                    "file": "internal/auth/jwt.go",
                    "line": 12,
                    "message": "Comment body.",
                    "fingerprint": "abc123",
                }
            ],
        }
        self._assert_valid(payload, "posting_manifest.schema.json")


if __name__ == "__main__":
    unittest.main()
