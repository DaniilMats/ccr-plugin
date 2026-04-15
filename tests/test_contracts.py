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
            "llm_invocation.schema.json",
            "reviewer_result.schema.json",
            "reviewers_manifest.schema.json",
            "review_prepare.schema.json",
            "consolidated_candidate.schema.json",
            "candidates_manifest.schema.json",
            "verification_prepare.schema.json",
            "verification_batch.schema.json",
            "verification_result.schema.json",
            "verified_findings.schema.json",
            "run_metrics.schema.json",
            "posting_approval.schema.json",
            "posting_manifest.schema.json",
            "posting_result.schema.json",
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
                "index": 8,
                "total": 11
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
            "run_metrics_file": "/tmp/ccr/run/run_metrics.json",
            "watch_cursor_file": "/tmp/ccr/run/watch_cursor.json",
            "harness_stdout_file": "/tmp/ccr/run/logs/harness.stdout.txt",
            "harness_stderr_file": "/tmp/ccr/run/logs/harness.stderr.txt",
            "pid": 12345,
            "detached": True,
            "report_file": "/tmp/ccr/run/report.md",
            "reviewers_file": "/tmp/ccr/run/reviewers.json",
            "review_prepare_file": "/tmp/ccr/run/review_prepare.json",
            "candidates_file": "/tmp/ccr/run/candidates.json",
            "verification_prepare_file": "/tmp/ccr/run/verification_prepare.json",
            "verified_findings_file": "/tmp/ccr/run/verified_findings.json",
            "posting_approval_file": "/tmp/ccr/run/posting_approval.json",
            "posting_manifest_file": "/tmp/ccr/run/posting_manifest.json",
            "posting_results_file": "/tmp/ccr/run/posting_results.json",
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
            "run_metrics_file": "/tmp/ccr/run/run_metrics.json",
            "watch_cursor_file": "/tmp/ccr/run/watch_cursor.json",
            "report_file": "/tmp/ccr/run/report.md",
            "reviewers_file": "/tmp/ccr/run/reviewers.json",
            "review_prepare_file": "/tmp/ccr/run/review_prepare.json",
            "candidates_file": "/tmp/ccr/run/candidates.json",
            "verification_prepare_file": "/tmp/ccr/run/verification_prepare.json",
            "verified_findings_file": "/tmp/ccr/run/verified_findings.json",
            "posting_approval_file": "/tmp/ccr/run/posting_approval.json",
            "posting_manifest_file": "/tmp/ccr/run/posting_manifest.json",
            "posting_results_file": "/tmp/ccr/run/posting_results.json",
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
                "index": 8,
                "total": 11
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
                "Run 20260414T220000Z-1234-abcd1234: state=running, stage=[8/11] reviewers"
            ],
            "next_poll_sec": 10
        }
        self._assert_valid(payload, "watch_result.schema.json")

    def test_static_analysis_contract(self) -> None:
        payload = self.static_analysis.empty_result()
        self._assert_valid(payload, "static_analysis.schema.json")

    def test_llm_invocation_contract(self) -> None:
        payload = {
            "provider": "codex",
            "thread_id": "thread-123",
            "tokens": 321,
            "duration_ms": 1234,
            "exit_code": 0,
            "error": None,
            "timed_out": False,
            "schema_valid": True,
            "schema_retries": 1,
            "schema_violations": [],
        }
        self._assert_valid(payload, "llm_invocation.schema.json")

    def test_review_prepare_contract(self) -> None:
        payload = {
            "contract_version": "ccr.review_prepare.v1",
            "summary": {
                "changed_file_count": 1,
                "requirement_clause_count": 2,
                "conditional_clause_count": 1,
                "dimension_count": 3,
                "case_count": 4,
                "question_count": 3,
            },
            "requirements": {
                "has_requirements": True,
                "clauses": [
                    {
                        "id": "R1",
                        "text": "omitOnEmpty hides the widget only when history is empty",
                        "kind": "visibility",
                        "conditional": True,
                    }
                ],
            },
            "changed": {
                "files": ["internal/layout/widgetmanager/builder/widgets/shortwidget/widget.go"],
                "symbols": ["omitOnEmpty", "hasTransactions"],
                "state_terms": ["untrusted", "history", "transactions"],
                "conditionals": [
                    {
                        "change": "added",
                        "text": "show := !omitOnEmpty",
                    }
                ],
            },
            "related_context": {
                "snippets": [
                    {
                        "type": "test",
                        "text": "TestShortWidgetBuild_UntrustedDevice_OmitOnEmpty_WithTransactions_ShowsUntrusted",
                    }
                ],
            },
            "scenario_matrix": {
                "dimensions": [
                    {
                        "name": "omitOnEmpty",
                        "type": "boolean",
                        "values": [True, False],
                        "source": "symbol",
                    }
                ],
                "cases": [
                    {
                        "id": "C1",
                        "inputs": {"omitOnEmpty": True},
                        "check": "Compare this combination against requirement clauses.",
                        "requirement_ids": ["R1"],
                    }
                ],
            },
            "invariants": ["Visibility semantics mention both the flag and data presence."],
            "questions_to_verify": ["Does every branch preserve both operands?"],
            "route_context": {
                "triggered_personas": ["security", "requirements"],
                "highest_risk_personas": ["security", "requirements"],
                "review_plan_summary": "Review plan: high-risk MR → Logic x3, Security x3, Requirements x2",
            },
            "summary_text": "Prepared 2 requirement clauses, 3 scenario dimensions, and 4 scenario cases for downstream reviewers.",
        }
        self._assert_valid(payload, "review_prepare.schema.json")

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
            "llm_invocation": {
                "provider": "codex",
                "thread_id": "thread-123",
                "tokens": 321,
                "duration_ms": 1234,
                "exit_code": 0,
                "error": None,
                "timed_out": False,
                "schema_valid": True,
                "schema_retries": 1,
                "schema_violations": [],
            },
        }
        self._assert_valid(payload, "reviewer_result.schema.json")

    def test_reviewers_manifest_contract(self) -> None:
        payload = {
            "contract_version": "ccr.reviewers_manifest.v1",
            "passes": [
                {
                    "pass_name": "security_p1",
                    "persona": "security",
                    "provider": "codex",
                    "diff_kind": "original",
                    "status": "succeeded",
                    "exit_code": 0,
                    "timed_out": False,
                    "started_at": "2026-04-15T00:00:00Z",
                    "finished_at": "2026-04-15T00:00:05Z",
                    "duration_ms": 5000,
                    "output_file": "/tmp/ccr/run/reviewers/security_p1.json",
                    "stderr_file": "/tmp/ccr/run/logs/reviewer.security_p1.stderr.txt",
                    "finding_count": 1,
                    "summary": "One finding.",
                    "tokens": 321,
                    "thread_id": "thread-reviewer-1",
                    "schema_valid": True,
                    "schema_retries": 1,
                    "schema_violations": [],
                    "llm_invocation": {
                        "provider": "codex",
                        "thread_id": "thread-reviewer-1",
                        "tokens": 321,
                        "duration_ms": 1234,
                        "exit_code": 0,
                        "error": None,
                        "timed_out": False,
                        "schema_valid": True,
                        "schema_retries": 1,
                        "schema_violations": [],
                    },
                }
            ],
            "summary": {
                "planned_passes": 14,
                "worker_count": 14,
                "timeout_sec": 600,
                "estimated_max_duration_sec": 600,
                "completed_passes": 14,
                "succeeded_passes": 13,
                "failed_passes": 1,
                "total_findings": 3,
                "llm_call_count": 14,
                "total_tokens": 4567,
                "llm_total_duration_ms": 120000,
                "schema_retry_count": 2,
                "schema_retry_rate": 0.1429,
                "schema_violation_count": 1,
                "timed_out_calls": 0,
                "failed_calls": 1,
                "provider_breakdown": {
                    "codex": {
                        "call_count": 5,
                        "total_tokens": 1500,
                        "total_duration_ms": 40000,
                        "schema_retry_count": 1,
                        "schema_violation_count": 1,
                        "timed_out_calls": 0,
                        "failed_calls": 1,
                    }
                },
            },
        }
        self._assert_valid(payload, "reviewers_manifest.schema.json")

    def test_consolidated_candidate_contract(self) -> None:
        payload = {
            "contract_version": "ccr.consolidated_candidate.v1",
            "candidate_id": "F1",
            "persona": "security",
            "supporting_personas": ["logic"],
            "file": "internal/auth/jwt.go",
            "line": 12,
            "message": "Example candidate.",
            "severity": "bug",
            "reviewers": ["security_p1", "security_p2"],
            "consensus": "2/2",
            "support_count": 2,
            "available_pass_count": 2,
            "symbol": "ValidateToken",
            "normalized_category": "jwt-validation-missing-expiry-check",
            "anchor_status": "diff",
            "evidence_sources": ["diff_hunk", "gosec"],
            "source_findings": [
                {
                    "pass_name": "security_p1",
                    "provider": "gemini",
                    "persona": "security",
                    "file": "internal/auth/jwt.go",
                    "line": 12,
                    "severity": "bug",
                    "message": "Example candidate.",
                }
            ],
            "prefilter": {
                "ready_for_verification": True,
                "drop_reasons": [],
            },
            "evidence_bundle": {
                "diff_hunk": "@@ -1,1 +1,2 @@",
                "file_context": "package auth",
                "requirements_excerpt": "",
                "static_analysis": [],
            },
        }
        self._assert_valid(payload, "consolidated_candidate.schema.json")

    def test_candidates_manifest_contract(self) -> None:
        payload = {
            "contract_version": "ccr.candidates_manifest.v1",
            "candidates": [
                {
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
            ],
            "summary": {
                "candidate_count": 1,
                "source_finding_count": 2,
            },
        }
        self._assert_valid(payload, "candidates_manifest.schema.json")

    def test_verification_prepare_contract(self) -> None:
        payload = {
            "contract_version": "ccr.verification_prepare.v1",
            "prepared_at": "2026-04-15T00:00:00Z",
            "ready_candidates": [
                {
                    "candidate_id": "F1",
                    "file": "internal/auth/jwt.go",
                    "line": 12,
                    "message": "Example candidate.",
                    "ready_for_verification": True,
                    "drop_reasons": [],
                    "persona": "security",
                }
            ],
            "dropped_candidates": [
                {
                    "candidate_id": "F2",
                    "file": "internal/auth/jwt.go",
                    "line": 30,
                    "message": "Dropped candidate.",
                    "ready_for_verification": False,
                    "drop_reasons": ["missing_anchor"],
                }
            ],
            "batches": [
                {
                    "batch_id": "B1",
                    "batch_file": "/tmp/ccr/run/verify_batches/verify_batch_001.json",
                    "file": "internal/auth/jwt.go",
                    "candidate_ids": ["F1"],
                    "candidate_count": 1,
                }
            ],
            "summary": {
                "candidate_count": 2,
                "ready_count": 1,
                "dropped_count": 1,
                "batch_count": 1,
            },
        }
        self._assert_valid(payload, "verification_prepare.schema.json")

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
                    "persona": "security",
                    "severity": "bug",
                    "reviewers": ["security_p1", "security_p2"],
                    "consensus": "2/2",
                    "symbol": "ValidateToken",
                    "anchor_status": "diff",
                    "evidence_sources": ["diff_hunk", "gosec"],
                    "source_findings": [],
                    "evidence_bundle": {"diff_hunk": "@@ -1,1 +1,2 @@"},
                    "prefilter": {"ready_for_verification": True, "drop_reasons": []},
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
                    "title": "Negative days rendered",
                    "problem": "CalendarDaysUntil can return a negative value on this path.",
                    "impact": "Users can see text like 'Due in -2 days'.",
                    "suggested_fixes": [
                        "Change case days == 0 to case days <= 0.",
                        "Clamp negative days to zero before the switch.",
                    ],
                    "evidence": "Supported by the provided diff.",
                    "anchor_status": "diff",
                    "evidence_sources": ["diff_hunk", "gosec"],
                    "support_count": 2,
                    "available_pass_count": 2,
                    "prefilter_status": "ready",
                    "evidence_bundle": {"diff_hunk": "@@ -1,1 +1,2 @@"},
                }
            ],
            "summary": "One finding confirmed.",
            "raw_response": "{}",
            "llm_invocation": {
                "provider": "codex",
                "thread_id": "thread-456",
                "tokens": 222,
                "duration_ms": 987,
                "exit_code": 0,
                "error": None,
                "timed_out": False,
                "schema_valid": True,
                "schema_retries": 1,
                "schema_violations": [],
            },
        }
        self._assert_valid(payload, "verification_result.schema.json")

    def test_verified_findings_contract(self) -> None:
        payload = {
            "contract_version": "ccr.verified_findings.v1",
            "verified_findings": [
                {
                    "candidate_id": "F1",
                    "persona": "security",
                    "severity": "bug",
                    "file": "internal/auth/jwt.go",
                    "line": 12,
                    "message": "Tightened message.",
                    "title": "Negative days rendered",
                    "problem": "CalendarDaysUntil can return a negative value on this path.",
                    "impact": "Users can see text like 'Due in -2 days'.",
                    "suggested_fixes": [
                        "Change case days == 0 to case days <= 0.",
                        "Clamp negative days to zero before the switch.",
                    ],
                    "evidence": "Supported by the provided diff.",
                    "verdict": "confirmed",
                    "reviewers": ["security_p1", "security_p2"],
                    "consensus": "2/2",
                    "evidence_sources": ["diff_hunk", "gosec"],
                    "tentative": False,
                    "finding_number": 1,
                    "support_count": 2,
                    "available_pass_count": 2,
                    "anchor_status": "diff",
                    "evidence_bundle": {"diff_hunk": "@@ -1,1 +1,2 @@"},
                    "prefilter_status": "ready",
                }
            ],
            "verification_batches": [
                {
                    "batch_id": "B1",
                    "status": "succeeded",
                    "batch_file": "/tmp/ccr/run/verify_batches/verify_batch_001.json",
                }
            ],
            "summary": {
                "verified_count": 1,
                "batch_count": 1,
                "successful_batches": 1,
                "failed_batches": 0,
                "worker_count": 1,
                "timeout_sec": 300,
                "estimated_max_duration_sec": 300,
            },
        }
        self._assert_valid(payload, "verified_findings.schema.json")

    def test_run_metrics_contract(self) -> None:
        payload = {
            "contract_version": "ccr.run_metrics.v1",
            "run_id": "20260414T220000Z-1234-abcd1234",
            "generated_at": "2026-04-14T22:00:00Z",
            "mode": "local",
            "target": "package:internal/auth",
            "requirements": {
                "source": "inline",
                "has_requirements": True,
                "requirements_chars": 42,
            },
            "route": {
                "summary": "Review plan: high-risk MR → Logic x3, Security x3",
                "total_passes": 14,
                "full_matrix": True,
                "changed_file_count": 2,
                "changed_lines": 52,
            },
            "reviewers": {
                "planned_passes": 14,
                "completed_passes": 14,
                "succeeded_passes": 13,
                "failed_passes": 1,
                "total_findings": 3,
                "availability_rate": 0.9286,
            },
            "candidates": {
                "candidate_count": 2,
                "source_finding_count": 3,
                "skipped_invalid_finding_count": 0,
                "duplicate_merge_count": 1,
                "duplicate_merge_rate": 0.3333,
            },
            "verification": {
                "candidate_count": 2,
                "ready_count": 1,
                "dropped_count": 1,
                "anchor_failure_count": 1,
                "anchor_failure_rate": 0.5,
                "drop_reason_counts": {"missing_anchor": 1},
                "confirmed_count": 1,
                "uncertain_count": 0,
                "rejected_count": 0,
                "rejection_rate": 0.0,
                "verified_count": 1,
                "batch_count": 1,
                "successful_batches": 1,
                "failed_batches": 0,
                "llm_call_count": 1,
                "total_tokens": 222,
                "llm_total_duration_ms": 987,
                "schema_retry_count": 1,
                "schema_retry_rate": 1.0,
                "schema_violation_count": 0,
                "timed_out_calls": 0,
                "failed_calls": 0,
                "provider_breakdown": {
                    "codex": {
                        "call_count": 1,
                        "total_tokens": 222,
                        "total_duration_ms": 987,
                        "schema_retry_count": 1,
                        "schema_violation_count": 0,
                        "timed_out_calls": 0,
                        "failed_calls": 0,
                    }
                },
            },
            "llm": {
                "total_calls": 15,
                "reviewer_calls": 14,
                "verifier_calls": 1,
                "total_tokens": 4789,
                "total_duration_ms": 120987,
                "schema_retry_count": 3,
                "schema_retry_rate": 0.2,
                "schema_violation_count": 1,
                "timed_out_calls": 0,
                "failed_calls": 1,
                "provider_breakdown": {
                    "codex": {
                        "call_count": 6,
                        "total_tokens": 1722,
                        "total_duration_ms": 40987,
                        "schema_retry_count": 2,
                        "schema_violation_count": 1,
                        "timed_out_calls": 0,
                        "failed_calls": 1,
                    }
                },
            },
            "posting": {
                "posting_supported": False,
                "posting_approval_file": "/tmp/ccr/run/posting_approval.json",
                "posting_manifest_file": "/tmp/ccr/run/posting_manifest.json",
                "posting_results_file": "/tmp/ccr/run/posting_results.json",
            },
        }
        self._assert_valid(payload, "run_metrics.schema.json")

    def test_posting_approval_contract(self) -> None:
        payload = {
            "contract_version": "ccr.posting_approval.v1",
            "run_id": "20260414T220000Z-1234-abcd1234",
            "project": "group/project",
            "mr_iid": 123,
            "approved_finding_numbers": [1, 2],
            "approved_all": False,
            "approved_at": "2026-04-14T22:00:00Z",
            "source": "user_selection",
        }
        self._assert_valid(payload, "posting_approval.schema.json")

    def test_posting_manifest_contract(self) -> None:
        payload = {
            "contract_version": "ccr.posting_manifest.v1",
            "run_id": "20260414T220000Z-1234-abcd1234",
            "project": "group/project",
            "mr_iid": 123,
            "prepared_at": "2026-04-14T22:01:00Z",
            "approved_all": False,
            "approved_finding_numbers": [1],
            "invalid_finding_numbers": [],
            "diff_refs": {
                "base_sha": "aaa",
                "start_sha": "bbb",
                "head_sha": "ccc"
            },
            "approved_findings": [
                {
                    "finding_number": 1,
                    "candidate_id": "F1",
                    "persona": "security",
                    "severity": "bug",
                    "file": "internal/auth/jwt.go",
                    "line": 12,
                    "message": "Comment body.",
                    "fingerprint": "abc123",
                    "status": "ready",
                    "payload_file": "/tmp/ccr/run/comment_payloads/001-F1.request.json",
                    "anchor": {
                        "position_type": "text",
                        "base_sha": "aaa",
                        "start_sha": "bbb",
                        "head_sha": "ccc",
                        "old_path": "internal/auth/jwt.go",
                        "new_path": "internal/auth/jwt.go",
                        "new_line": 12,
                        "line_kind": "new"
                    }
                }
            ],
            "summary": {
                "approved_count": 1,
                "ready_count": 1,
                "missing_anchor_count": 0,
                "invalid_count": 0,
                "status_counts": {
                    "ready": 1,
                    "missing_anchor": 0
                },
                "persona_breakdown": {
                    "security": {
                        "approved_count": 1,
                        "ready_count": 1,
                        "missing_anchor_count": 0,
                        "posted_count": 0,
                        "already_posted_count": 0,
                        "skipped_count": 0,
                        "skipped_missing_anchor_count": 0,
                        "skipped_invalid_selection_count": 0,
                        "failed_count": 0,
                        "invalid_response_count": 0
                    }
                },
                "severity_breakdown": {
                    "bug": {
                        "approved_count": 1,
                        "ready_count": 1,
                        "missing_anchor_count": 0,
                        "posted_count": 0,
                        "already_posted_count": 0,
                        "skipped_count": 0,
                        "skipped_missing_anchor_count": 0,
                        "skipped_invalid_selection_count": 0,
                        "failed_count": 0,
                        "invalid_response_count": 0
                    }
                }
            }
        }
        self._assert_valid(payload, "posting_manifest.schema.json")

    def test_posting_result_contract(self) -> None:
        payload = {
            "contract_version": "ccr.posting_result.v1",
            "run_id": "20260414T220000Z-1234-abcd1234",
            "project": "group/project",
            "mr_iid": 123,
            "approved_all": False,
            "approved_finding_numbers": [1],
            "invalid_finding_numbers": [],
            "started_at": "2026-04-14T22:01:00Z",
            "finished_at": "2026-04-14T22:01:03Z",
            "duration_ms": 3000,
            "posted_count": 1,
            "already_posted_count": 0,
            "skipped_count": 0,
            "failed_count": 0,
            "results": [
                {
                    "finding_number": 1,
                    "candidate_id": "F1",
                    "persona": "security",
                    "severity": "bug",
                    "file": "internal/auth/jwt.go",
                    "line": 12,
                    "message": "Comment body.",
                    "prepared_status": "ready",
                    "fingerprint": "abc123",
                    "status": "posted",
                    "payload_file": "/tmp/ccr/run/comment_payloads/001-F1.request.json",
                    "response_file": "/tmp/ccr/run/comment_payloads/001-F1.response.json",
                    "discussion_id": "discussion-1",
                    "note_id": 42,
                    "error": None,
                    "attempts": 1
                }
            ],
            "summary": {
                "approved_all": False,
                "approved_count": 1,
                "ready_count": 1,
                "missing_anchor_count": 0,
                "invalid_count": 0,
                "ready_resolved_count": 1,
                "ready_resolution_rate": 1.0,
                "posted_count": 1,
                "already_posted_count": 0,
                "skipped_count": 0,
                "failed_count": 0,
                "total_attempts": 1,
                "status_counts": {
                    "posted": 1,
                    "already_posted": 0,
                    "skipped_missing_anchor": 0,
                    "skipped_invalid_selection": 0,
                    "failed": 0,
                    "invalid_response": 0
                },
                "persona_breakdown": {
                    "security": {
                        "approved_count": 1,
                        "ready_count": 1,
                        "missing_anchor_count": 0,
                        "posted_count": 1,
                        "already_posted_count": 0,
                        "skipped_count": 0,
                        "skipped_missing_anchor_count": 0,
                        "skipped_invalid_selection_count": 0,
                        "failed_count": 0,
                        "invalid_response_count": 0
                    }
                },
                "severity_breakdown": {
                    "bug": {
                        "approved_count": 1,
                        "ready_count": 1,
                        "missing_anchor_count": 0,
                        "posted_count": 1,
                        "already_posted_count": 0,
                        "skipped_count": 0,
                        "skipped_missing_anchor_count": 0,
                        "skipped_invalid_selection_count": 0,
                        "failed_count": 0,
                        "invalid_response_count": 0
                    }
                }
            }
        }
        self._assert_valid(payload, "posting_result.schema.json")


if __name__ == "__main__":
    unittest.main()
