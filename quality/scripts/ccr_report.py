#!/usr/bin/env python3
"""Human-readable CCR run summaries.

Reads run-scoped observability artifacts and prints a compact operational report
for a single CCR run.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ccr_runtime.common import display_path, format_milliseconds_short, load_json_file, utc_now

_HERE = Path(__file__).resolve()
_REPO_ROOT = _HERE.parents[2]
_REPORT_CONTRACT = "ccr.run_report.v1"
_PERSONA_ORDER = ("logic", "security", "concurrency", "performance", "requirements")
_PERSONA_LABELS = {
    "logic": "Logic",
    "security": "Security",
    "concurrency": "Concurrency",
    "performance": "Performance",
    "requirements": "Requirements",
}


def _safe_load(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.is_file():
        return None
    payload = load_json_file(path, default=None)
    return payload if isinstance(payload, dict) else None


def _resolve_summary_file(run_dir: Path) -> Path:
    return run_dir / "run_summary.json"


def _resolve_manifest_file(run_dir: Path) -> Path:
    return run_dir / "run_manifest.json"


def _artifact_path(payload: dict[str, Any] | None, key: str, *, default_run_dir: Path | None = None, default_name: str | None = None) -> Path | None:
    if isinstance(payload, dict):
        value = str(payload.get(key) or "").strip()
        if value:
            return Path(value).expanduser().resolve()
    if default_run_dir is not None and default_name:
        return (default_run_dir / default_name).resolve()
    return None


def _provider_breakdown_text(breakdown: dict[str, Any] | None) -> str | None:
    if not isinstance(breakdown, dict) or not breakdown:
        return None
    parts: list[str] = []
    for provider in sorted(breakdown):
        bucket = breakdown.get(provider)
        if not isinstance(bucket, dict):
            continue
        call_count = int(bucket.get("call_count") or 0)
        if call_count <= 0:
            continue
        parts.append(f"{provider} x{call_count}")
    return ", ".join(parts) if parts else None


def _persona_mix_text(pass_counts: dict[str, Any] | None) -> str | None:
    if not isinstance(pass_counts, dict) or not pass_counts:
        return None
    parts: list[str] = []
    seen: set[str] = set()
    for persona in _PERSONA_ORDER:
        count = int(pass_counts.get(persona) or 0)
        if count > 0:
            parts.append(f"{_PERSONA_LABELS.get(persona, persona.title())} x{count}")
            seen.add(persona)
    for persona in sorted(key for key in pass_counts if key not in seen):
        count = int(pass_counts.get(persona) or 0)
        if count > 0:
            parts.append(f"{_PERSONA_LABELS.get(persona, persona.title())} x{count}")
    return ", ".join(parts) if parts else None


def _build_anomalies(
    *,
    state: str,
    reviewers: dict[str, Any],
    verification: dict[str, Any],
    llm: dict[str, Any],
    posting_summary: dict[str, Any] | None,
) -> list[str]:
    anomalies: list[str] = []
    if state == "failed":
        anomalies.append("run failed")
    reviewer_failures = int(reviewers.get("failed_passes") or 0)
    if reviewer_failures > 0:
        anomalies.append(f"reviewer failures={reviewer_failures}")
    verifier_failures = int(verification.get("failed_batches") or 0)
    if verifier_failures > 0:
        anomalies.append(f"verification failures={verifier_failures}")
    timed_out_calls = int(llm.get("timed_out_calls") or 0)
    if timed_out_calls > 0:
        anomalies.append(f"llm timed out={timed_out_calls}")
    failed_calls = int(llm.get("failed_calls") or 0)
    if failed_calls > 0:
        anomalies.append(f"llm failed calls={failed_calls}")
    schema_retry_count = int(llm.get("schema_retry_count") or 0)
    if schema_retry_count > 0:
        anomalies.append(f"schema retries={schema_retry_count}")
    anchor_failure_rate = verification.get("anchor_failure_rate")
    if isinstance(anchor_failure_rate, (int, float)) and anchor_failure_rate >= 0.2:
        anomalies.append(f"anchor failure rate={anchor_failure_rate:.2f}")
    rejection_rate = verification.get("rejection_rate")
    ready_count = int(verification.get("ready_count") or 0)
    if isinstance(rejection_rate, (int, float)) and ready_count > 0 and rejection_rate >= 0.7:
        anomalies.append(f"high rejection rate={rejection_rate:.2f}")
    if isinstance(posting_summary, dict):
        failed_count = int(posting_summary.get("failed_count") or 0)
        missing_anchor_count = int(posting_summary.get("missing_anchor_count") or 0)
        if failed_count > 0:
            anomalies.append(f"posting failed={failed_count}")
        if missing_anchor_count > 0:
            anomalies.append(f"posting missing anchors={missing_anchor_count}")
    return anomalies


def _resolve_context(*, run_dir: Path | None, summary_file: Path | None, manifest_file: Path | None) -> dict[str, Any]:
    effective_run_dir = run_dir.resolve() if run_dir is not None else None

    summary = _safe_load(summary_file) if summary_file else None
    if summary is None and effective_run_dir is not None:
        summary = _safe_load(_resolve_summary_file(effective_run_dir))

    manifest = _safe_load(manifest_file) if manifest_file else None
    if manifest is None:
        manifest_from_summary = _artifact_path(summary, "manifest_file") if summary else None
        if manifest_from_summary is not None:
            manifest = _safe_load(manifest_from_summary)
    if manifest is None and effective_run_dir is not None:
        manifest = _safe_load(_resolve_manifest_file(effective_run_dir))

    if effective_run_dir is None:
        if isinstance(summary, dict):
            effective_run_dir = Path(str(summary.get("run_dir") or Path(summary.get("summary_file")).parent)).expanduser().resolve()
        elif isinstance(manifest, dict):
            effective_run_dir = Path(str(manifest.get("run_dir") or Path(manifest.get("manifest_file")).parent)).expanduser().resolve()
        else:
            raise ValueError("could not resolve run_dir; provide --run-dir, --summary-file, or --manifest-file")

    status = _safe_load(_artifact_path(summary, "status_file", default_run_dir=effective_run_dir, default_name="status.json"))
    route_plan = _safe_load(_artifact_path(manifest or summary, "route_plan_file", default_run_dir=effective_run_dir, default_name="route_plan.json"))
    run_metrics = _safe_load(_artifact_path(summary, "run_metrics_file", default_run_dir=effective_run_dir, default_name="run_metrics.json"))
    reviewers = _safe_load(_artifact_path(summary, "reviewers_file", default_run_dir=effective_run_dir, default_name="reviewers.json"))
    verification_prepare = _safe_load(_artifact_path(summary, "verification_prepare_file", default_run_dir=effective_run_dir, default_name="verification_prepare.json"))
    verified_findings = _safe_load(_artifact_path(summary, "verified_findings_file", default_run_dir=effective_run_dir, default_name="verified_findings.json"))
    posting_results = _safe_load(_artifact_path(summary, "posting_results_file", default_run_dir=effective_run_dir, default_name="posting_results.json"))
    posting_manifest = _safe_load(_artifact_path(summary, "posting_manifest_file", default_run_dir=effective_run_dir, default_name="posting_manifest.json"))

    return {
        "run_dir": effective_run_dir,
        "summary": summary or {},
        "manifest": manifest or {},
        "status": status or {},
        "route_plan": route_plan or {},
        "run_metrics": run_metrics or {},
        "reviewers": reviewers or {},
        "verification_prepare": verification_prepare or {},
        "verified_findings": verified_findings or {},
        "posting_results": posting_results or {},
        "posting_manifest": posting_manifest or {},
    }


def build_run_report(*, run_dir: Path | None = None, summary_file: Path | None = None, manifest_file: Path | None = None) -> dict[str, Any]:
    context = _resolve_context(run_dir=run_dir, summary_file=summary_file, manifest_file=manifest_file)
    effective_run_dir = context["run_dir"]
    summary = context["summary"]
    status = context["status"]
    route_plan = context["route_plan"]
    run_metrics = context["run_metrics"]
    reviewers_payload = context["reviewers"]
    verification_prepare = context["verification_prepare"]
    verified_findings_payload = context["verified_findings"]
    posting_results = context["posting_results"]

    reviewers_summary = reviewers_payload.get("summary") if isinstance(reviewers_payload.get("summary"), dict) else {}
    if not reviewers_summary and isinstance(run_metrics.get("reviewers"), dict):
        reviewers_summary = dict(run_metrics.get("reviewers") or {})
    verification_summary = run_metrics.get("verification") if isinstance(run_metrics.get("verification"), dict) else {}
    llm_summary = run_metrics.get("llm") if isinstance(run_metrics.get("llm"), dict) else {}
    route_summary = run_metrics.get("route") if isinstance(run_metrics.get("route"), dict) else {}
    posting_summary = posting_results.get("summary") if isinstance(posting_results.get("summary"), dict) else None

    state = str(status.get("state") or ("completed" if summary else "unknown"))
    current_stage = status.get("current_stage") if isinstance(status.get("current_stage"), dict) else None
    review_plan_summary = str(
        summary.get("review_plan_summary")
        or route_summary.get("summary")
        or route_plan.get("summary")
        or ""
    ).strip() or None
    reviewer_mix = _persona_mix_text(route_summary.get("pass_counts") if isinstance(route_summary.get("pass_counts"), dict) else route_plan.get("pass_counts"))
    provider_breakdown = _provider_breakdown_text(llm_summary.get("provider_breakdown") if isinstance(llm_summary.get("provider_breakdown"), dict) else reviewers_summary.get("provider_breakdown"))

    verified_findings = verified_findings_payload.get("verified_findings") if isinstance(verified_findings_payload.get("verified_findings"), list) else []
    verified_count = int(summary.get("verified_finding_count") or verification_summary.get("verified_count") or len(verified_findings))
    candidate_summary = verification_prepare.get("summary") if isinstance(verification_prepare.get("summary"), dict) else {}

    funnel = {
        "planned_reviewers": int(reviewers_summary.get("planned_passes") or 0),
        "completed_reviewers": int(reviewers_summary.get("completed_passes") or 0),
        "reviewer_findings": int(reviewers_summary.get("total_findings") or 0),
        "candidate_count": int(candidate_summary.get("candidate_count") or run_metrics.get("candidates", {}).get("candidate_count") or 0),
        "ready_count": int(candidate_summary.get("ready_count") or verification_summary.get("ready_count") or 0),
        "verified_count": verified_count,
        "posted_count": int((posting_summary or {}).get("posted_count") or 0),
        "failed_post_count": int((posting_summary or {}).get("failed_count") or 0),
    }

    anomalies = _build_anomalies(
        state=state,
        reviewers=reviewers_summary,
        verification=verification_summary,
        llm=llm_summary,
        posting_summary=posting_summary,
    )

    artifact_files = {
        "run_dir": str(effective_run_dir),
        "summary_file": str(_artifact_path(summary, "summary_file", default_run_dir=effective_run_dir, default_name="run_summary.json") or ""),
        "run_metrics_file": str(_artifact_path(summary, "run_metrics_file", default_run_dir=effective_run_dir, default_name="run_metrics.json") or ""),
        "reviewers_file": str(_artifact_path(summary, "reviewers_file", default_run_dir=effective_run_dir, default_name="reviewers.json") or ""),
        "verification_prepare_file": str(_artifact_path(summary, "verification_prepare_file", default_run_dir=effective_run_dir, default_name="verification_prepare.json") or ""),
        "verified_findings_file": str(_artifact_path(summary, "verified_findings_file", default_run_dir=effective_run_dir, default_name="verified_findings.json") or ""),
        "posting_results_file": str(_artifact_path(summary, "posting_results_file", default_run_dir=effective_run_dir, default_name="posting_results.json") or ""),
    }

    return {
        "contract_version": _REPORT_CONTRACT,
        "generated_at": utc_now(),
        "run_id": str(summary.get("run_id") or status.get("run_id") or context["manifest"].get("run_id") or effective_run_dir.name),
        "state": state,
        "current_stage": current_stage,
        "mode": summary.get("mode") or run_metrics.get("mode"),
        "target": summary.get("target") or run_metrics.get("target") or str(effective_run_dir),
        "duration_ms": summary.get("duration_ms") or status.get("duration_ms"),
        "review_plan_summary": review_plan_summary,
        "reviewer_mix": reviewer_mix,
        "provider_breakdown": provider_breakdown,
        "funnel": funnel,
        "reviewers": reviewers_summary,
        "verification": verification_summary,
        "llm": llm_summary,
        "posting": posting_summary or {
            "posting_supported": bool(run_metrics.get("posting", {}).get("posting_supported")),
            "posted_count": 0,
            "failed_count": 0,
        },
        "anomalies": anomalies,
        "artifacts": artifact_files,
    }


def render_report_text(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    run_id = str(payload.get("run_id") or "unknown")
    state = str(payload.get("state") or "unknown")
    mode = str(payload.get("mode") or "unknown")
    target = str(payload.get("target") or "unknown")
    lines.append(f"CCR {run_id} · {state} · {mode} · {target}")

    current_stage = payload.get("current_stage") if isinstance(payload.get("current_stage"), dict) else None
    if current_stage:
        stage_name = str(current_stage.get("name") or "unknown")
        stage_status = str(current_stage.get("status") or "unknown")
        lines.append(f"Stage: {stage_name} ({stage_status})")

    review_plan_summary = str(payload.get("review_plan_summary") or "").strip()
    if review_plan_summary:
        lines.append(f"Plan: {review_plan_summary}")

    reviewer_mix = str(payload.get("reviewer_mix") or "").strip()
    if reviewer_mix:
        lines.append(f"Reviewer mix: {reviewer_mix}")

    duration_value = payload.get("duration_ms")
    if isinstance(duration_value, int):
        lines.append(f"Duration: {format_milliseconds_short(duration_value)}")

    funnel = payload.get("funnel") if isinstance(payload.get("funnel"), dict) else {}
    lines.append(
        "Funnel: reviewers {completed}/{planned} · findings={findings} · candidates={candidates} · ready={ready} · verified={verified} · posted={posted}".format(
            completed=int(funnel.get("completed_reviewers") or 0),
            planned=int(funnel.get("planned_reviewers") or 0),
            findings=int(funnel.get("reviewer_findings") or 0),
            candidates=int(funnel.get("candidate_count") or 0),
            ready=int(funnel.get("ready_count") or 0),
            verified=int(funnel.get("verified_count") or 0),
            posted=int(funnel.get("posted_count") or 0),
        )
    )

    reviewers = payload.get("reviewers") if isinstance(payload.get("reviewers"), dict) else {}
    lines.append(
        "Reviewers: ok={ok}/{planned} · failed={failed} · tokens={tokens} · retries={retries}".format(
            ok=int(reviewers.get("succeeded_passes") or 0),
            planned=int(reviewers.get("planned_passes") or 0),
            failed=int(reviewers.get("failed_passes") or 0),
            tokens=int(reviewers.get("total_tokens") or 0),
            retries=int(reviewers.get("schema_retry_count") or 0),
        )
    )

    verification = payload.get("verification") if isinstance(payload.get("verification"), dict) else {}
    lines.append(
        "Verification: confirmed={confirmed} · uncertain={uncertain} · rejected={rejected} · batches={batches}".format(
            confirmed=int(verification.get("confirmed_count") or 0),
            uncertain=int(verification.get("uncertain_count") or 0),
            rejected=int(verification.get("rejected_count") or 0),
            batches=int(verification.get("batch_count") or 0),
        )
    )

    provider_breakdown = str(payload.get("provider_breakdown") or "").strip()
    if provider_breakdown:
        lines.append(f"Providers: {provider_breakdown}")

    posting = payload.get("posting") if isinstance(payload.get("posting"), dict) else {}
    if posting:
        posting_supported = bool(posting.get("posting_supported"))
        if posting_supported or int(posting.get("posted_count") or 0) > 0 or int(posting.get("failed_count") or 0) > 0:
            lines.append(
                "Posting: posted={posted} · already_posted={already_posted} · failed={failed} · missing_anchor={missing}".format(
                    posted=int(posting.get("posted_count") or 0),
                    already_posted=int(posting.get("already_posted_count") or 0),
                    failed=int(posting.get("failed_count") or 0),
                    missing=int(posting.get("missing_anchor_count") or 0),
                )
            )

    anomalies = payload.get("anomalies") if isinstance(payload.get("anomalies"), list) else []
    if anomalies:
        lines.append("Anomalies:")
        lines.extend(f"- {item}" for item in anomalies)
    else:
        lines.append("Anomalies: none")

    artifacts = payload.get("artifacts") if isinstance(payload.get("artifacts"), dict) else {}
    if artifacts:
        lines.append("Artifacts:")
        for key in (
            "summary_file",
            "run_metrics_file",
            "reviewers_file",
            "verification_prepare_file",
            "verified_findings_file",
            "posting_results_file",
        ):
            value = str(artifacts.get(key) or "").strip()
            if not value:
                continue
            lines.append(f"- {key}: {display_path(Path(value), relative_to=_REPO_ROOT)}")

    return "\n".join(lines) + "\n"


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ccr-report",
        description="Print a compact human-readable report for a CCR run.",
    )
    parser.add_argument("--run-dir", default=None, help="Path to a CCR run directory.")
    parser.add_argument("--summary-file", default=None, help="Optional path to run_summary.json.")
    parser.add_argument("--manifest-file", default=None, help="Optional path to run_manifest.json.")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    return parser


def main() -> None:
    args = _build_arg_parser().parse_args()
    try:
        payload = build_run_report(
            run_dir=Path(args.run_dir).expanduser().resolve() if args.run_dir else None,
            summary_file=Path(args.summary_file).expanduser().resolve() if args.summary_file else None,
            manifest_file=Path(args.manifest_file).expanduser().resolve() if args.manifest_file else None,
        )
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, indent=2), file=sys.stderr)
        sys.exit(1)

    if args.format == "json":
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        sys.stdout.write(render_report_text(payload))


if __name__ == "__main__":
    main()
