from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ccr_runtime.common import utc_now

RUN_MANIFEST_VERSION = "ccr.run_manifest.v1"
DEFAULT_BASE_DIR = "/tmp/ccr"


def build_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


def build_manifest(base_dir: Path, run_id: str) -> dict[str, Any]:
    run_dir = base_dir / run_id
    logs_dir = run_dir / "logs"
    verify_batch_dir = run_dir / "verify_batches"
    reviewer_results_dir = run_dir / "reviewers"
    verifier_results_dir = run_dir / "verifier_results"
    comments_dir = run_dir / "comment_payloads"

    for path in (run_dir, logs_dir, verify_batch_dir, reviewer_results_dir, verifier_results_dir, comments_dir):
        path.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, Any] = {
        "contract_version": RUN_MANIFEST_VERSION,
        "run_id": run_id,
        "created_at": utc_now(),
        "base_dir": str(base_dir),
        "run_dir": str(run_dir),
        "logs_dir": str(logs_dir),
        "manifest_file": str(run_dir / "run_manifest.json"),
        "status_file": str(run_dir / "status.json"),
        "trace_file": str(run_dir / "trace.jsonl"),
        "summary_file": str(run_dir / "run_summary.json"),
        "run_metrics_file": str(run_dir / "run_metrics.json"),
        "watch_cursor_file": str(run_dir / "watch_cursor.json"),
        "harness_stdout_file": str(logs_dir / "harness.stdout.txt"),
        "harness_stderr_file": str(logs_dir / "harness.stderr.txt"),
        "diff_file": str(run_dir / "review_artifact.txt"),
        "requirements_file": str(run_dir / "requirements.txt"),
        "mr_metadata_file": str(run_dir / "mr_metadata.json"),
        "route_input_file": str(run_dir / "route_input.json"),
        "route_plan_file": str(run_dir / "route_plan.json"),
        "route_helper_err_file": str(logs_dir / "route_helper.stderr.txt"),
        "review_context_file": str(run_dir / "review_context.md"),
        "static_analysis_file": str(run_dir / "static_analysis.json"),
        "shuffled_diff_file": str(run_dir / "review_artifact.shuffled.txt"),
        "requirements_prompt_pass1_file": str(run_dir / "requirements_pass1.prompt.txt"),
        "requirements_prompt_pass2_file": str(run_dir / "requirements_pass2.prompt.txt"),
        "verify_batch_dir": str(verify_batch_dir),
        "reviewer_results_dir": str(reviewer_results_dir),
        "verifier_results_dir": str(verifier_results_dir),
        "reviewers_file": str(run_dir / "reviewers.json"),
        "candidates_file": str(run_dir / "candidates.json"),
        "verification_prepare_file": str(run_dir / "verification_prepare.json"),
        "verified_findings_file": str(run_dir / "verified_findings.json"),
        "posting_approval_file": str(run_dir / "posting_approval.json"),
        "posting_manifest_file": str(run_dir / "posting_manifest.json"),
        "posting_results_file": str(run_dir / "posting_results.json"),
        "comments_dir": str(comments_dir),
        "report_file": str(run_dir / "report.md"),
        "contract_versions": {
            "route_input": "ccr.route_input.v1",
            "route_plan": "ccr.route_plan.v1",
            "run_status": "ccr.run_status.v1",
            "run_summary": "ccr.run_summary.v1",
            "run_launch": "ccr.run_launch.v1",
            "watch_result": "ccr.watch_result.v1",
            "static_analysis": "ccr.static_analysis.v1",
            "llm_invocation": "ccr.llm_invocation.v1",
            "reviewer_result": "ccr.reviewer_result.v1",
            "reviewers_manifest": "ccr.reviewers_manifest.v1",
            "consolidated_candidate": "ccr.consolidated_candidate.v1",
            "candidates_manifest": "ccr.candidates_manifest.v1",
            "verification_prepare": "ccr.verification_prepare.v1",
            "verification_batch": "ccr.verification_batch.v1",
            "verification_result": "ccr.verification_result.v1",
            "verified_findings": "ccr.verified_findings.v1",
            "run_metrics": "ccr.run_metrics.v1",
            "posting_approval": "ccr.posting_approval.v1",
            "posting_manifest": "ccr.posting_manifest.v1",
            "posting_result": "ccr.posting_result.v1",
        },
    }
    return manifest
