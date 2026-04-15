from __future__ import annotations

import concurrent.futures
import re
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from ccr_consolidate import CandidateRecord
from ccr_verify_prepare import prepare_verification_artifacts
from ccr_runtime.common import (
    duration_ms,
    estimate_parallel_stage_duration,
    load_json_file,
    ratio,
    resolve_worker_count,
    run_command,
    utc_now,
    write_json,
    write_text,
)
from ccr_runtime.observer import RunObserver
from ccr_runtime.reporting import REPORT_PERSONA_ORDER, severity_rank
from ccr_runtime.telemetry import (
    aggregate_llm_metrics,
    collect_llm_invocations,
    empty_llm_metrics,
    llm_summary_fields,
    normalize_llm_invocation,
)

_HERE = Path(__file__).resolve().parents[1]
_CODE_REVIEW_VERIFY_SCRIPT = _HERE / "llm-proxy" / "code_review_verify.py"


def verification_verdict_counts(results: list[dict[str, Any]]) -> dict[str, int]:
    counts = {
        "confirmed_count": 0,
        "uncertain_count": 0,
        "rejected_count": 0,
    }
    for item in results:
        payload = item.get("result") if isinstance(item.get("result"), dict) else {}
        findings = payload.get("verified_findings") if isinstance(payload.get("verified_findings"), list) else []
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            verdict = str(finding.get("verdict") or "")
            if verdict == "confirmed":
                counts["confirmed_count"] += 1
            elif verdict == "uncertain":
                counts["uncertain_count"] += 1
            elif verdict == "rejected":
                counts["rejected_count"] += 1
    return counts


def verification_prepare_metrics(payload: dict[str, Any]) -> dict[str, Any]:
    ready_candidates = payload.get("ready_candidates") if isinstance(payload.get("ready_candidates"), list) else []
    dropped_candidates = payload.get("dropped_candidates") if isinstance(payload.get("dropped_candidates"), list) else []
    candidate_count = len(ready_candidates) + len(dropped_candidates)
    anchor_failure_count = 0
    drop_reason_counts: dict[str, int] = defaultdict(int)
    for item in [*ready_candidates, *dropped_candidates]:
        if not isinstance(item, dict):
            continue
        drop_reasons = [str(entry) for entry in (item.get("drop_reasons") or [])]
        for reason in drop_reasons:
            drop_reason_counts[reason] += 1
        anchor_status = str(item.get("anchor_status") or "")
        if anchor_status == "missing" or "missing_anchor" in drop_reasons:
            anchor_failure_count += 1
    return {
        "candidate_count": candidate_count,
        "ready_count": len(ready_candidates),
        "dropped_count": len(dropped_candidates),
        "anchor_failure_count": anchor_failure_count,
        "anchor_failure_rate": ratio(anchor_failure_count, candidate_count),
        "drop_reason_counts": {key: drop_reason_counts[key] for key in sorted(drop_reason_counts)},
    }


def run_single_verification_batch(
    batch: dict[str, Any],
    *,
    manifest: dict[str, Any],
    project_dir: Path | None,
    dry_run: bool,
    verifier_timeout_sec: int,
) -> dict[str, Any]:
    batch_path = Path(batch["batch_file"])
    output_path = Path(manifest["verifier_results_dir"]) / f"{batch_path.stem}.result.json"
    stderr_path = Path(manifest["logs_dir"]) / f"verifier.{batch_path.stem}.stderr.txt"
    providers = ["codex"] if dry_run else ["codex", "gemini"]
    used_provider = providers[0]
    attempted_providers: list[str] = []
    last_exit_code = 1
    last_stderr = ""
    started_at = utc_now()
    started_mono = time.monotonic()
    timed_out = False

    cwd = project_dir if project_dir and project_dir.is_dir() else Path.cwd()

    for provider in providers:
        used_provider = provider
        attempted_providers.append(provider)
        cmd = [
            sys.executable,
            str(_CODE_REVIEW_VERIFY_SCRIPT),
            "--input-file",
            str(batch_path),
            "--provider",
            provider,
            "--output-file",
            str(output_path),
        ]
        if dry_run:
            cmd.append("--dry-run")
        try:
            result = run_command(cmd, cwd=cwd, timeout=verifier_timeout_sec + 30)
            last_exit_code = result.returncode
            last_stderr = result.stderr
        except subprocess.TimeoutExpired:
            last_exit_code = -1
            last_stderr = f"timed out after {verifier_timeout_sec + 30} seconds\n"
            timed_out = True
        if output_path.is_file() and last_exit_code == 0:
            break

    finished_at = utc_now()
    elapsed = duration_ms(started_mono)

    write_text(stderr_path, last_stderr)
    payload = load_json_file(output_path, default={})
    if not isinstance(payload, dict):
        payload = {}
    llm_invocation = normalize_llm_invocation(
        payload.get("llm_invocation"),
        provider=used_provider,
        duration_ms=elapsed,
        exit_code=last_exit_code,
        timed_out=timed_out,
        error=(last_stderr.strip() or payload.get("summary")) if last_exit_code != 0 or timed_out else None,
    )

    return {
        "batch_id": batch["batch_id"],
        "batch_file": str(batch_path),
        "candidate_count": int(batch.get("candidate_count") or 0),
        "output_file": str(output_path),
        "stderr_file": str(stderr_path),
        "provider": used_provider,
        "attempted_providers": attempted_providers,
        "exit_code": last_exit_code,
        "status": "succeeded" if last_exit_code == 0 else "failed",
        "timed_out": timed_out,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": elapsed,
        "llm_invocation": llm_invocation,
        "result": payload,
    }


def parse_consensus_support(consensus: str) -> tuple[int, int]:
    match = re.match(r"^(\d+)/(\d+)$", consensus)
    if not match:
        return 0, 0
    return int(match.group(1)), int(match.group(2))


def merge_verified_findings(
    manifest: dict[str, Any],
    *,
    candidates: list[CandidateRecord],
    verification_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    candidate_map = {candidate.candidate_id: candidate for candidate in candidates}
    merged: list[dict[str, Any]] = []
    for batch in verification_results:
        payload = batch.get("result") if isinstance(batch.get("result"), dict) else {}
        findings = payload.get("verified_findings") if isinstance(payload.get("verified_findings"), list) else []
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            candidate_id = str(finding.get("candidate_id") or "")
            candidate = candidate_map.get(candidate_id)
            if candidate is None:
                continue
            verdict = str(finding.get("verdict") or "rejected")
            if verdict not in {"confirmed", "uncertain", "rejected"}:
                verdict = "rejected"
            if verdict == "rejected":
                continue
            support_count, _ = parse_consensus_support(candidate.consensus)
            support_count = int(candidate.support_count or support_count)
            if not bool(candidate.prefilter.get("ready_for_verification", True)):
                continue
            if verdict == "uncertain" and (support_count < 2 or candidate.anchor_status == "missing"):
                continue
            merged.append(
                {
                    "candidate_id": candidate.candidate_id,
                    "persona": candidate.persona,
                    "severity": candidate.severity,
                    "file": str(finding.get("file") or candidate.file),
                    "line": int(finding.get("line") or candidate.line),
                    "message": str(finding.get("revised_message") or candidate.message),
                    "evidence": str(finding.get("evidence") or ""),
                    "verdict": verdict,
                    "reviewers": list(candidate.reviewers),
                    "consensus": candidate.consensus,
                    "evidence_sources": list(candidate.evidence_sources),
                    "tentative": verdict == "uncertain",
                    "support_count": candidate.support_count,
                    "available_pass_count": candidate.available_pass_count,
                    "anchor_status": str(finding.get("anchor_status") or candidate.anchor_status),
                    "evidence_bundle": {
                        "diff_hunk": candidate.evidence_bundle.get("diff_hunk"),
                        "file_context": candidate.evidence_bundle.get("file_context"),
                        "requirements_excerpt": candidate.evidence_bundle.get("requirements_excerpt"),
                        "static_analysis": [dict(item) for item in candidate.evidence_bundle.get("static_analysis", [])],
                    },
                    "prefilter_status": "ready" if bool(candidate.prefilter.get("ready_for_verification", True)) else "dropped",
                }
            )

    merged.sort(
        key=lambda item: (
            REPORT_PERSONA_ORDER.index(item["persona"]),
            severity_rank(item["severity"]),
            item["file"],
            item["line"],
            item["candidate_id"],
        )
    )
    for finding_number, item in enumerate(merged, start=1):
        item["finding_number"] = finding_number
    payload = {
        "contract_version": "ccr.verified_findings.v1",
        "verified_findings": merged,
        "verification_batches": [
            {
                key: value
                for key, value in batch.items()
                if key != "result"
            }
            for batch in verification_results
        ],
        "summary": {
            "verified_count": len(merged),
            "batch_count": len(verification_results),
            "successful_batches": sum(1 for batch in verification_results if batch["status"] == "succeeded"),
        },
    }
    write_json(Path(manifest["verified_findings_file"]), payload)
    return merged


def run_verification(
    manifest: dict[str, Any],
    *,
    observer: RunObserver,
    candidates: list[CandidateRecord],
    artifact_text: str,
    project_dir: Path | None,
    requirements_text: str,
    dry_run: bool,
    verifier_timeout_sec: int,
    max_verifier_workers: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    prepared = prepare_verification_artifacts(
        candidates,
        artifact_text=artifact_text,
        project_dir=project_dir,
        requirements_text=requirements_text,
        verify_batch_dir=Path(manifest["verify_batch_dir"]),
        output_file=Path(manifest["verification_prepare_file"]),
    )
    prepared_candidates = prepared["prepared_candidates"]
    ready_candidates = prepared["ready_candidates"]
    batches = prepared["batches"]
    prepare_payload = prepared.get("payload") if isinstance(prepared.get("payload"), dict) else {}
    prepare_metrics = verification_prepare_metrics(prepare_payload)

    candidates_payload = load_json_file(Path(manifest["candidates_file"]), default={})
    if isinstance(candidates_payload, dict):
        candidates_payload["candidates"] = [candidate.to_contract_dict() for candidate in prepared_candidates]
        write_json(Path(manifest["candidates_file"]), candidates_payload)

    if not ready_candidates:
        summary = {
            "candidate_count": prepare_metrics["candidate_count"],
            "ready_count": prepare_metrics["ready_count"],
            "dropped_count": prepare_metrics["dropped_count"],
            "anchor_failure_count": prepare_metrics["anchor_failure_count"],
            "anchor_failure_rate": prepare_metrics["anchor_failure_rate"],
            "drop_reason_counts": dict(prepare_metrics["drop_reason_counts"]),
            "confirmed_count": 0,
            "uncertain_count": 0,
            "rejected_count": 0,
            "rejection_rate": None,
            "verified_count": 0,
            "batch_count": 0,
            "successful_batches": 0,
            "failed_batches": 0,
            "worker_count": 0,
            "timeout_sec": verifier_timeout_sec,
            "estimated_max_duration_sec": 0,
            **llm_summary_fields(empty_llm_metrics()),
        }
        payload = {
            "contract_version": "ccr.verified_findings.v1",
            "verified_findings": [],
            "verification_batches": [],
            "summary": summary,
        }
        write_json(Path(manifest["verified_findings_file"]), payload)
        return [], summary

    worker_count = resolve_worker_count(max_verifier_workers, len(batches), auto_cap=8)
    estimated_max_duration_sec = estimate_parallel_stage_duration(len(batches), worker_count, verifier_timeout_sec)
    observer.configure_verification(
        batch_ids=[batch["batch_id"] for batch in batches],
        workers=worker_count,
        timeout_sec=verifier_timeout_sec,
        estimated_max_duration_sec=estimated_max_duration_sec,
    )

    results: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, worker_count)) as pool:
        future_map = {}
        for batch in batches:
            observer.verification_batch_started(batch["batch_id"], batch["batch_file"])
            future = pool.submit(
                run_single_verification_batch,
                batch,
                manifest=manifest,
                project_dir=project_dir,
                dry_run=dry_run,
                verifier_timeout_sec=verifier_timeout_sec,
            )
            future_map[future] = batch["batch_id"]
        for future in concurrent.futures.as_completed(future_map):
            result = future.result()
            results.append(result)
            observer.verification_batch_finished(result)

    results.sort(key=lambda item: item["batch_id"])
    verified_findings = merge_verified_findings(manifest, candidates=prepared_candidates, verification_results=results)
    verification_llm_metrics = aggregate_llm_metrics(collect_llm_invocations(results))
    verdict_counts = verification_verdict_counts(results)
    verification_summary = {
        "candidate_count": prepare_metrics["candidate_count"],
        "ready_count": prepare_metrics["ready_count"],
        "dropped_count": prepare_metrics["dropped_count"],
        "anchor_failure_count": prepare_metrics["anchor_failure_count"],
        "anchor_failure_rate": prepare_metrics["anchor_failure_rate"],
        "drop_reason_counts": dict(prepare_metrics["drop_reason_counts"]),
        **verdict_counts,
        "rejection_rate": ratio(verdict_counts["rejected_count"], len(ready_candidates)),
        "verified_count": len(verified_findings),
        "batch_count": len(results),
        "successful_batches": sum(1 for batch in results if batch["status"] == "succeeded"),
        "failed_batches": sum(1 for batch in results if batch["status"] != "succeeded"),
        "worker_count": worker_count,
        "timeout_sec": verifier_timeout_sec,
        "estimated_max_duration_sec": estimated_max_duration_sec,
        **llm_summary_fields(verification_llm_metrics),
    }
    verified_payload = load_json_file(Path(manifest["verified_findings_file"]), default={})
    if isinstance(verified_payload, dict):
        verified_payload["summary"] = verification_summary
        write_json(Path(manifest["verified_findings_file"]), verified_payload)
    return verified_findings, verification_summary
