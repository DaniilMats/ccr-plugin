from __future__ import annotations

import concurrent.futures
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ccr_runtime.common import (
    duration_ms,
    estimate_parallel_stage_duration,
    load_json_file,
    resolve_worker_count,
    run_command,
    utc_now,
    write_json,
    write_text,
)
from ccr_runtime.observer import RunObserver
from ccr_runtime.telemetry import aggregate_llm_metrics, collect_llm_invocations, llm_summary_fields, normalize_llm_invocation

_HERE = Path(__file__).resolve().parents[1]
_CODE_REVIEW_SCRIPT = _HERE / "llm-proxy" / "code_review.py"


@dataclass(frozen=True)
class ReviewerPassSpec:
    pass_name: str
    persona: str
    provider: str
    diff_kind: Literal["original", "shuffled"]


PASS_SPECS: dict[str, ReviewerPassSpec] = {
    "logic_p1": ReviewerPassSpec("logic_p1", "logic", "gemini", "original"),
    "logic_p2": ReviewerPassSpec("logic_p2", "logic", "codex", "shuffled"),
    "logic_p3": ReviewerPassSpec("logic_p3", "logic", "claude", "original"),
    "security_p1": ReviewerPassSpec("security_p1", "security", "gemini", "original"),
    "security_p2": ReviewerPassSpec("security_p2", "security", "codex", "shuffled"),
    "security_p3": ReviewerPassSpec("security_p3", "security", "claude", "original"),
    "concurrency_p1": ReviewerPassSpec("concurrency_p1", "concurrency", "gemini", "original"),
    "concurrency_p2": ReviewerPassSpec("concurrency_p2", "concurrency", "codex", "shuffled"),
    "concurrency_p3": ReviewerPassSpec("concurrency_p3", "concurrency", "claude", "original"),
    "performance_p1": ReviewerPassSpec("performance_p1", "performance", "gemini", "original"),
    "performance_p2": ReviewerPassSpec("performance_p2", "performance", "codex", "shuffled"),
    "performance_p3": ReviewerPassSpec("performance_p3", "performance", "claude", "original"),
    "requirements_p1": ReviewerPassSpec("requirements_p1", "requirements", "gemini", "original"),
    "requirements_p2": ReviewerPassSpec("requirements_p2", "requirements", "codex", "shuffled"),
}


def build_reviewer_command(
    spec: ReviewerPassSpec,
    *,
    manifest: dict[str, Any],
    requirements_available: bool,
    dry_run: bool,
    timeout_sec: int,
) -> tuple[list[str], str]:
    diff_path = manifest["diff_file"] if spec.diff_kind == "original" else manifest["shuffled_diff_file"]
    output_path = str(Path(manifest["reviewer_results_dir"]) / f"{spec.pass_name}.json")

    cmd = [
        sys.executable,
        str(_CODE_REVIEW_SCRIPT),
        "--diff-file",
        diff_path,
        "--provider",
        spec.provider,
        "--persona",
        spec.persona,
        "--static-analysis",
        manifest["static_analysis_file"],
        "--review-context-file",
        manifest["review_context_file"],
        "--review-prepare-file",
        manifest["review_prepare_file"],
        "--output-file",
        output_path,
        "--timeout",
        str(timeout_sec),
    ]
    if requirements_available:
        cmd.extend(["--requirements-file", manifest["requirements_file"]])
    if dry_run:
        cmd.append("--dry-run")
    return cmd, output_path


def run_reviewer_pass(
    spec: ReviewerPassSpec,
    *,
    manifest: dict[str, Any],
    project_dir: Path | None,
    requirements_available: bool,
    dry_run: bool,
    timeout_sec: int,
) -> dict[str, Any]:
    cmd, output_path = build_reviewer_command(
        spec,
        manifest=manifest,
        requirements_available=requirements_available,
        dry_run=dry_run,
        timeout_sec=timeout_sec,
    )
    stderr_path = Path(manifest["logs_dir"]) / f"reviewer.{spec.pass_name}.stderr.txt"
    started_at = utc_now()
    started_mono = time.monotonic()
    timed_out = False

    cwd = project_dir if project_dir and project_dir.is_dir() else Path.cwd()
    try:
        result = run_command(cmd, cwd=cwd, timeout=timeout_sec + 30)
        stderr = result.stderr
        exit_code = result.returncode
    except subprocess.TimeoutExpired:
        stderr = f"timed out after {timeout_sec + 30} seconds\n"
        exit_code = -1
        timed_out = True

    finished_at = utc_now()
    elapsed = duration_ms(started_mono)

    write_text(stderr_path, stderr)
    output_payload = load_json_file(Path(output_path), default={})
    if not isinstance(output_payload, dict):
        output_payload = {}

    summary = str(output_payload.get("summary") or "Reviewer did not produce structured output.")
    findings = output_payload.get("findings") if isinstance(output_payload.get("findings"), list) else []
    status = "succeeded" if exit_code == 0 else "failed"
    llm_invocation = normalize_llm_invocation(
        output_payload.get("llm_invocation"),
        provider=spec.provider,
        duration_ms=elapsed,
        exit_code=exit_code,
        timed_out=timed_out,
        error=(stderr.strip() or summary) if exit_code != 0 or timed_out else None,
    )

    return {
        "pass_name": spec.pass_name,
        "persona": spec.persona,
        "provider": spec.provider,
        "diff_kind": spec.diff_kind,
        "status": status,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": elapsed,
        "output_file": output_path,
        "stderr_file": str(stderr_path),
        "finding_count": len(findings),
        "summary": summary,
        "llm_invocation": llm_invocation,
        "result": output_payload,
    }


def run_reviewers(
    manifest: dict[str, Any],
    route_plan: dict[str, Any],
    *,
    observer: RunObserver,
    project_dir: Path | None,
    requirements_available: bool,
    dry_run: bool,
    reviewer_timeout_sec: int,
    max_reviewer_workers: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    passes = route_plan.get("passes") or []
    specs = [PASS_SPECS[pass_name] for pass_name in passes]
    worker_count = resolve_worker_count(max_reviewer_workers, len(specs), auto_cap=14)
    estimated_max_duration_sec = estimate_parallel_stage_duration(len(specs), worker_count, reviewer_timeout_sec)
    observer.configure_reviewers(
        passes=passes,
        workers=worker_count,
        timeout_sec=reviewer_timeout_sec,
        estimated_max_duration_sec=estimated_max_duration_sec,
    )

    results: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, worker_count)) as pool:
        future_map = {}
        for spec in specs:
            observer.reviewer_started(spec)
            future = pool.submit(
                run_reviewer_pass,
                spec,
                manifest=manifest,
                project_dir=project_dir,
                requirements_available=requirements_available,
                dry_run=dry_run,
                timeout_sec=reviewer_timeout_sec,
            )
            future_map[future] = spec.pass_name
        for future in concurrent.futures.as_completed(future_map):
            result = future.result()
            results.append(result)
            observer.reviewer_finished(result)

    results.sort(key=lambda item: passes.index(item["pass_name"]))
    reviewer_llm_metrics = aggregate_llm_metrics(collect_llm_invocations(results))
    reviewers_summary = {
        "planned_passes": len(passes),
        "worker_count": worker_count,
        "timeout_sec": reviewer_timeout_sec,
        "estimated_max_duration_sec": estimated_max_duration_sec,
        "completed_passes": len(results),
        "succeeded_passes": sum(1 for item in results if item["status"] == "succeeded"),
        "failed_passes": sum(1 for item in results if item["status"] != "succeeded"),
        "total_findings": sum(item["finding_count"] for item in results),
        **llm_summary_fields(reviewer_llm_metrics),
    }
    reviewers_payload = {
        "contract_version": "ccr.reviewers_manifest.v1",
        "passes": [
            {
                key: value
                for key, value in item.items()
                if key != "result"
            }
            for item in results
        ],
        "summary": reviewers_summary,
    }
    write_json(Path(manifest["reviewers_file"]), reviewers_payload)
    return results, reviewers_summary
