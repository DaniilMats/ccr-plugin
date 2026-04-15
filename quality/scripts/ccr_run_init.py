#!/usr/bin/env python3
"""Initialize an isolated CCR run workspace.

Creates a per-run directory, emits a run manifest with stable artifact paths,
and prints the manifest as JSON so the agent can reuse the paths across the
review workflow.

Examples:
    python3 ccr_run_init.py
    python3 ccr_run_init.py --base-dir /tmp/ccr --output-file /tmp/ccr-last-run.json
"""
from __future__ import annotations

import argparse
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path


RUN_MANIFEST_VERSION = "ccr.run_manifest.v1"
DEFAULT_BASE_DIR = "/tmp/ccr"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _build_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


def _build_manifest(base_dir: Path, run_id: str) -> dict:
    run_dir = base_dir / run_id
    logs_dir = run_dir / "logs"
    verify_batch_dir = run_dir / "verify_batches"
    reviewer_results_dir = run_dir / "reviewers"
    verifier_results_dir = run_dir / "verifier_results"
    comments_dir = run_dir / "comment_payloads"

    for path in (run_dir, logs_dir, verify_batch_dir, reviewer_results_dir, verifier_results_dir, comments_dir):
        path.mkdir(parents=True, exist_ok=True)

    manifest = {
        "contract_version": RUN_MANIFEST_VERSION,
        "run_id": run_id,
        "created_at": _utc_now(),
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


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    tmp_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp_path, path)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ccr-run-init",
        description="Create an isolated CCR run workspace and print its manifest as JSON.",
    )
    parser.add_argument(
        "--base-dir",
        default=DEFAULT_BASE_DIR,
        help=f"Base directory for run workspaces (default: {DEFAULT_BASE_DIR}).",
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="Optional explicit run_id. Normally auto-generated.",
    )
    parser.add_argument(
        "--output-file",
        default=None,
        help="Optional extra path to also write the manifest JSON.",
    )
    return parser


def main() -> None:
    args = _build_parser().parse_args()

    base_dir = Path(args.base_dir).expanduser().resolve()
    run_id = args.run_id or _build_run_id()
    manifest = _build_manifest(base_dir, run_id)

    manifest_path = Path(manifest["manifest_file"])
    _write_json(manifest_path, manifest)

    if args.output_file:
        _write_json(Path(args.output_file).expanduser().resolve(), manifest)

    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
