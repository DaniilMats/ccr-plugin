from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Protocol

from ccr_runtime.common import duration_ms, format_milliseconds_short, format_seconds_short, utc_now, write_json
from ccr_runtime.telemetry import invocation_event_fields, normalize_llm_invocation

STAGE_SEQUENCE: tuple[str, ...] = (
    "artifact_preparation",
    "requirements",
    "routing",
    "review_context",
    "static_analysis",
    "shuffle_diff",
    "reviewers",
    "candidates",
    "verification",
    "report",
)
STAGE_INDEX = {name: index + 1 for index, name in enumerate(STAGE_SEQUENCE)}
STAGE_TOTAL = len(STAGE_SEQUENCE)


class ReviewerPassLike(Protocol):
    pass_name: str
    persona: str
    provider: str
    diff_kind: str


class RunObserver:
    def __init__(self, manifest: dict[str, Any]) -> None:
        self.run_id = manifest["run_id"]
        self.trace_file = Path(manifest["trace_file"])
        self.status_file = Path(manifest["status_file"])
        self.summary_file = Path(manifest["summary_file"])
        self._lock = threading.Lock()
        self._run_started_monotonic = time.monotonic()
        self._stage_started_monotonic: dict[str, float] = {}
        self._event_seq = 0
        self._revision = 0
        self._status: dict[str, Any] = {
            "contract_version": "ccr.run_status.v1",
            "run_id": self.run_id,
            "pid": os.getpid(),
            "detached": False,
            "revision": 0,
            "event_seq": 0,
            "state": "running",
            "started_at": utc_now(),
            "updated_at": None,
            "heartbeat_at": None,
            "finished_at": None,
            "duration_ms": None,
            "current_stage": None,
            "target": {},
            "route_plan": {},
            "stages": {},
            "reviewers": {
                "planned": 0,
                "workers": 0,
                "timeout_sec": None,
                "running": 0,
                "completed": 0,
                "succeeded": 0,
                "failed": 0,
                "estimated_max_duration_sec": None,
                "passes": {},
            },
            "verification": {
                "planned_batches": 0,
                "workers": 0,
                "timeout_sec": None,
                "running_batches": 0,
                "completed_batches": 0,
                "succeeded_batches": 0,
                "failed_batches": 0,
                "estimated_max_duration_sec": None,
                "batches": {},
            },
            "artifacts": {
                "run_dir": manifest["run_dir"],
                "manifest_file": manifest["manifest_file"],
                "status_file": manifest["status_file"],
                "trace_file": manifest["trace_file"],
                "summary_file": manifest["summary_file"],
                "run_metrics_file": manifest["run_metrics_file"],
                "watch_cursor_file": manifest["watch_cursor_file"],
                "report_file": manifest["report_file"],
                "reviewers_file": manifest["reviewers_file"],
                "candidates_file": manifest["candidates_file"],
                "verification_prepare_file": manifest["verification_prepare_file"],
                "verified_findings_file": manifest["verified_findings_file"],
                "posting_approval_file": manifest["posting_approval_file"],
                "posting_manifest_file": manifest["posting_manifest_file"],
                "posting_results_file": manifest["posting_results_file"],
                "harness_stdout_file": manifest["harness_stdout_file"],
                "harness_stderr_file": manifest["harness_stderr_file"],
            },
            "summary": {},
            "last_event": None,
            "error": None,
        }
        self.trace_file.parent.mkdir(parents=True, exist_ok=True)
        self.status_file.parent.mkdir(parents=True, exist_ok=True)
        self.summary_file.parent.mkdir(parents=True, exist_ok=True)
        self.trace_file.write_text("", encoding="utf-8")
        self._write_status_locked()

    def _stage_meta(self, stage: str | None) -> dict[str, Any]:
        if stage is None or stage not in STAGE_INDEX:
            return {}
        return {
            "index": STAGE_INDEX[stage],
            "total": STAGE_TOTAL,
        }

    def _stage_label(self, stage: str | None) -> str:
        if stage is None:
            return "run"
        if stage not in STAGE_INDEX:
            return stage
        meta = self._stage_meta(stage)
        return f"{meta['index']}/{meta['total']} {stage}"

    def _write_status_locked(self) -> None:
        now = utc_now()
        self._revision += 1
        self._status["revision"] = self._revision
        self._status["updated_at"] = now
        self._status["heartbeat_at"] = now
        self._status["event_seq"] = self._event_seq
        write_json(self.status_file, self._status)

    def _format_brief_data(self, data: dict[str, Any]) -> str:
        if not data:
            return ""
        preferred_keys = (
            "mode",
            "target",
            "project_dir",
            "summary",
            "source",
            "has_requirements",
            "requirements_chars",
            "changed_file_count",
            "changed_lines",
            "planned",
            "workers",
            "running",
            "completed",
            "succeeded",
            "failed",
            "finding_count",
            "candidate_count",
            "verified_count",
            "batch_count",
            "total_findings",
            "go_vet",
            "staticcheck",
            "gosec",
            "context_status",
            "pass_name",
            "batch_id",
            "provider",
            "status",
            "full_matrix",
            "estimated_max_duration_sec",
            "duration_ms",
            "run_dir",
            "report_file",
        )
        parts: list[str] = []
        for key in preferred_keys:
            if key not in data:
                continue
            value = data[key]
            if value in (None, "", [], {}):
                continue
            display_value = value
            if key.endswith("_duration_sec") or key == "timeout_sec":
                display_value = format_seconds_short(int(value))
            elif key == "estimated_max_duration_sec":
                display_value = format_seconds_short(int(value))
            elif key == "duration_ms":
                display_value = format_milliseconds_short(int(value))
            parts.append(f"{key}={display_value}")
        return " | " + ", ".join(parts) if parts else ""

    def set_process_info(self, *, pid: int, detached: bool) -> None:
        with self._lock:
            self._status["pid"] = pid
            self._status["detached"] = detached
            self._write_status_locked()

    def event(self, event: str, message: str, *, stage: str | None = None, level: str = "info", **data: Any) -> None:
        payload = {
            "seq": self._event_seq + 1,
            "ts": utc_now(),
            "level": level,
            "event": event,
            "stage": stage,
            "message": message,
            "data": data,
        }
        with self._lock:
            self._event_seq += 1
            payload["seq"] = self._event_seq
            with self.trace_file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self._status["last_event"] = payload
            self._write_status_locked()
        stage_label = self._stage_label(stage)
        print(f"[CCR][{payload['ts']}][{stage_label}] {message}{self._format_brief_data(data)}", file=sys.stderr, flush=True)

    def set_target(self, *, mode: str, target: str, project_dir: str | None) -> None:
        with self._lock:
            self._status["target"] = {
                "mode": mode,
                "target": target,
                "project_dir": project_dir,
            }
            self._write_status_locked()
        self.event(
            "target_ready",
            "Resolved review target",
            stage="bootstrap",
            mode=mode,
            target=target,
            project_dir=project_dir,
        )

    def start_stage(self, stage: str, message: str, **data: Any) -> None:
        started_at = utc_now()
        stage_meta = self._stage_meta(stage)
        with self._lock:
            self._stage_started_monotonic[stage] = time.monotonic()
            stage_payload = self._status["stages"].get(stage, {})
            stage_payload.update(
                {
                    "name": stage,
                    "status": "running",
                    "message": message,
                    "started_at": started_at,
                    "ended_at": None,
                    "duration_ms": None,
                    **stage_meta,
                }
            )
            if data:
                stage_payload.update(data)
            self._status["stages"][stage] = stage_payload
            self._status["current_stage"] = {
                "name": stage,
                "status": "running",
                "message": message,
                "started_at": started_at,
                "ended_at": None,
                "duration_ms": None,
                **stage_meta,
            }
            self._write_status_locked()
        self.event("stage_started", message, stage=stage, **data)

    def complete_stage(self, stage: str, message: str, **data: Any) -> None:
        finished_at = utc_now()
        stage_meta = self._stage_meta(stage)
        with self._lock:
            started_mono = self._stage_started_monotonic.get(stage)
            elapsed = duration_ms(started_mono) if started_mono is not None else None
            stage_payload = self._status["stages"].get(stage, {"name": stage, **stage_meta})
            stage_payload.update(
                {
                    "status": "completed",
                    "message": message,
                    "ended_at": finished_at,
                    "duration_ms": elapsed,
                    **stage_meta,
                }
            )
            if data:
                stage_payload.update(data)
            self._status["stages"][stage] = stage_payload
            self._status["current_stage"] = {
                "name": stage,
                "status": "completed",
                "message": message,
                "started_at": stage_payload.get("started_at"),
                "ended_at": finished_at,
                "duration_ms": elapsed,
                **stage_meta,
            }
            self._write_status_locked()
        payload = dict(data)
        if elapsed is not None:
            payload.setdefault("duration_ms", elapsed)
        self.event("stage_completed", message, stage=stage, **payload)

    def fail_stage(self, stage: str, message: str, **data: Any) -> None:
        finished_at = utc_now()
        stage_meta = self._stage_meta(stage)
        with self._lock:
            started_mono = self._stage_started_monotonic.get(stage)
            elapsed = duration_ms(started_mono) if started_mono is not None else None
            stage_payload = self._status["stages"].get(stage, {"name": stage, **stage_meta})
            stage_payload.update(
                {
                    "status": "failed",
                    "message": message,
                    "ended_at": finished_at,
                    "duration_ms": elapsed,
                    **stage_meta,
                }
            )
            if data:
                stage_payload.update(data)
            self._status["stages"][stage] = stage_payload
            self._status["current_stage"] = {
                "name": stage,
                "status": "failed",
                "message": message,
                "started_at": stage_payload.get("started_at"),
                "ended_at": finished_at,
                "duration_ms": elapsed,
                **stage_meta,
            }
            self._write_status_locked()
        payload = dict(data)
        if elapsed is not None:
            payload.setdefault("duration_ms", elapsed)
        self.event("stage_failed", message, stage=stage, level="error", **payload)

    def set_route_plan(self, route_input: dict[str, Any], route_plan: dict[str, Any]) -> None:
        with self._lock:
            self._status["route_plan"] = {
                "summary": route_plan.get("summary"),
                "total_passes": route_plan.get("total_passes"),
                "full_matrix": route_plan.get("full_matrix"),
                "pass_counts": route_plan.get("pass_counts"),
                "triggered_personas": route_input.get("triggered_personas"),
                "highest_risk_personas": route_input.get("highest_risk_personas"),
                "critical_surfaces": route_input.get("critical_surfaces"),
                "changed_file_count": route_input.get("changed_file_count"),
                "changed_lines": route_input.get("changed_lines"),
            }
            self._write_status_locked()
        self.event(
            "route_plan_ready",
            "Adaptive routing plan ready",
            stage="routing",
            summary=route_plan.get("summary"),
            planned=route_plan.get("total_passes"),
            triggered_personas=route_input.get("triggered_personas"),
            critical_surfaces=route_input.get("critical_surfaces"),
        )

    def configure_reviewers(
        self,
        *,
        passes: list[str],
        workers: int,
        timeout_sec: int,
        estimated_max_duration_sec: int,
    ) -> None:
        with self._lock:
            self._status["reviewers"] = {
                "planned": len(passes),
                "workers": workers,
                "timeout_sec": timeout_sec,
                "running": 0,
                "completed": 0,
                "succeeded": 0,
                "failed": 0,
                "estimated_max_duration_sec": estimated_max_duration_sec,
                "passes": {
                    pass_name: {
                        "status": "pending",
                    }
                    for pass_name in passes
                },
            }
            self._write_status_locked()
        self.event(
            "reviewers_started",
            "Launching reviewer passes",
            stage="reviewers",
            planned=len(passes),
            workers=workers,
            estimated_max_duration_sec=estimated_max_duration_sec,
        )

    def reviewer_started(self, spec: ReviewerPassLike) -> None:
        started_at = utc_now()
        with self._lock:
            reviewers = self._status["reviewers"]
            reviewers["running"] = reviewers.get("running", 0) + 1
            pass_status = reviewers["passes"].setdefault(spec.pass_name, {})
            pass_status.update(
                {
                    "persona": spec.persona,
                    "provider": spec.provider,
                    "diff_kind": spec.diff_kind,
                    "status": "running",
                    "started_at": started_at,
                }
            )
            self._write_status_locked()
        self.event(
            "reviewer_started",
            f"Reviewer started: {spec.pass_name}",
            stage="reviewers",
            pass_name=spec.pass_name,
            persona=spec.persona,
            provider=spec.provider,
            diff_kind=spec.diff_kind,
        )

    def reviewer_finished(self, result: dict[str, Any]) -> None:
        pass_name = result["pass_name"]
        llm_invocation = normalize_llm_invocation(
            result.get("llm_invocation"),
            provider=result.get("provider"),
            duration_ms=result.get("duration_ms"),
            exit_code=result.get("exit_code"),
            timed_out=result.get("timed_out"),
        )
        with self._lock:
            reviewers = self._status["reviewers"]
            pass_status = reviewers["passes"].setdefault(pass_name, {})
            pass_status.update(
                {
                    "persona": result["persona"],
                    "provider": result["provider"],
                    "diff_kind": result["diff_kind"],
                    "status": result["status"],
                    "exit_code": result["exit_code"],
                    "finding_count": result["finding_count"],
                    "summary": result["summary"],
                    "timed_out": result.get("timed_out", False),
                    "started_at": result.get("started_at"),
                    "finished_at": result.get("finished_at"),
                    "duration_ms": result.get("duration_ms"),
                    "output_file": result.get("output_file"),
                    "stderr_file": result.get("stderr_file"),
                    "llm_invocation": llm_invocation,
                    "tokens": llm_invocation.get("tokens"),
                    "thread_id": llm_invocation.get("thread_id"),
                    "schema_valid": llm_invocation.get("schema_valid"),
                    "schema_retries": llm_invocation.get("schema_retries"),
                    "schema_violations": llm_invocation.get("schema_violations"),
                }
            )
            reviewers["running"] = max(0, reviewers.get("running", 0) - 1)
            reviewers["completed"] += 1
            if result["status"] == "succeeded":
                reviewers["succeeded"] += 1
            else:
                reviewers["failed"] += 1
            completed = reviewers["completed"]
            planned = reviewers["planned"]
            running = reviewers["running"]
            self._write_status_locked()
        self.event(
            "reviewer_completed",
            f"Reviewer {completed}/{planned} finished: {pass_name}",
            stage="reviewers",
            completed=completed,
            planned=planned,
            running=running,
            pass_name=pass_name,
            persona=result["persona"],
            provider=result["provider"],
            finding_count=result["finding_count"],
            status=result["status"],
            duration_ms=result.get("duration_ms"),
            **invocation_event_fields(llm_invocation),
        )

    def configure_verification(
        self,
        *,
        batch_ids: list[str],
        workers: int,
        timeout_sec: int,
        estimated_max_duration_sec: int,
    ) -> None:
        with self._lock:
            self._status["verification"] = {
                "planned_batches": len(batch_ids),
                "workers": workers,
                "timeout_sec": timeout_sec,
                "running_batches": 0,
                "completed_batches": 0,
                "succeeded_batches": 0,
                "failed_batches": 0,
                "estimated_max_duration_sec": estimated_max_duration_sec,
                "batches": {
                    batch_id: {
                        "status": "pending",
                    }
                    for batch_id in batch_ids
                },
            }
            self._write_status_locked()
        self.event(
            "verification_started",
            "Launching verification batches",
            stage="verification",
            planned=len(batch_ids),
            workers=workers,
            estimated_max_duration_sec=estimated_max_duration_sec,
        )

    def verification_batch_started(self, batch_id: str, batch_file: str) -> None:
        started_at = utc_now()
        with self._lock:
            verification = self._status["verification"]
            verification["running_batches"] = verification.get("running_batches", 0) + 1
            batch_status = verification["batches"].setdefault(batch_id, {})
            batch_status.update(
                {
                    "status": "running",
                    "batch_file": batch_file,
                    "started_at": started_at,
                }
            )
            self._write_status_locked()
        self.event(
            "verification_batch_started",
            f"Verification batch started: {batch_id}",
            stage="verification",
            batch_id=batch_id,
        )

    def verification_batch_finished(self, result: dict[str, Any]) -> None:
        batch_id = result["batch_id"]
        llm_invocation = normalize_llm_invocation(
            result.get("llm_invocation"),
            provider=result.get("provider"),
            duration_ms=result.get("duration_ms"),
            exit_code=result.get("exit_code"),
            timed_out=result.get("timed_out"),
        )
        with self._lock:
            verification = self._status["verification"]
            batch_status = verification["batches"].setdefault(batch_id, {})
            payload = result.get("result") if isinstance(result.get("result"), dict) else {}
            verified_findings = payload.get("verified_findings") if isinstance(payload.get("verified_findings"), list) else []
            confirmed_count = 0
            uncertain_count = 0
            rejected_count = 0
            for finding in verified_findings:
                if not isinstance(finding, dict):
                    continue
                verdict = str(finding.get("verdict") or "")
                if verdict == "confirmed":
                    confirmed_count += 1
                elif verdict == "uncertain":
                    uncertain_count += 1
                elif verdict == "rejected":
                    rejected_count += 1
            batch_status.update(
                {
                    "status": result["status"],
                    "provider": result["provider"],
                    "exit_code": result["exit_code"],
                    "started_at": result.get("started_at"),
                    "finished_at": result.get("finished_at"),
                    "duration_ms": result.get("duration_ms"),
                    "batch_file": result.get("batch_file"),
                    "output_file": result.get("output_file"),
                    "stderr_file": result.get("stderr_file"),
                    "attempted_providers": result.get("attempted_providers"),
                    "candidate_count": result.get("candidate_count"),
                    "verified_findings": len(verified_findings),
                    "confirmed_count": confirmed_count,
                    "uncertain_count": uncertain_count,
                    "rejected_count": rejected_count,
                    "timed_out": result.get("timed_out", False),
                    "llm_invocation": llm_invocation,
                    "tokens": llm_invocation.get("tokens"),
                    "thread_id": llm_invocation.get("thread_id"),
                    "schema_valid": llm_invocation.get("schema_valid"),
                    "schema_retries": llm_invocation.get("schema_retries"),
                    "schema_violations": llm_invocation.get("schema_violations"),
                }
            )
            verification["running_batches"] = max(0, verification.get("running_batches", 0) - 1)
            verification["completed_batches"] += 1
            if result["status"] == "succeeded":
                verification["succeeded_batches"] += 1
            else:
                verification["failed_batches"] += 1
            completed = verification["completed_batches"]
            planned = verification["planned_batches"]
            running = verification["running_batches"]
            self._write_status_locked()
        self.event(
            "verification_batch_completed",
            f"Verification {completed}/{planned} finished: {batch_id}",
            stage="verification",
            batch_id=batch_id,
            completed=completed,
            planned=planned,
            running=running,
            provider=result["provider"],
            status=result["status"],
            duration_ms=result.get("duration_ms"),
            candidate_count=result.get("candidate_count"),
            confirmed_count=confirmed_count,
            uncertain_count=uncertain_count,
            rejected_count=rejected_count,
            **invocation_event_fields(llm_invocation),
        )

    def current_duration_ms(self) -> int:
        return duration_ms(self._run_started_monotonic)

    def complete_run(self, summary: dict[str, Any]) -> None:
        finished_at = utc_now()
        with self._lock:
            self._status["state"] = "completed"
            self._status["finished_at"] = finished_at
            self._status["duration_ms"] = duration_ms(self._run_started_monotonic)
            summary["duration_ms"] = self._status["duration_ms"]
            self._status["summary"] = summary
            self._status["current_stage"] = {
                "name": "completed",
                "status": "completed",
                "message": "CCR run completed",
                "started_at": self._status["started_at"],
                "ended_at": finished_at,
                "duration_ms": self._status["duration_ms"],
            }
            self._write_status_locked()
            write_json(self.summary_file, summary)
        self.event(
            "run_completed",
            "CCR run completed",
            stage="completed",
            verified_count=summary.get("verified_finding_count"),
            duration_ms=summary.get("duration_ms"),
            report_file=summary.get("report_file"),
        )

    def fail_run(self, exc: Exception) -> None:
        finished_at = utc_now()
        error_payload = {
            "type": type(exc).__name__,
            "message": str(exc),
        }
        with self._lock:
            self._status["state"] = "failed"
            self._status["finished_at"] = finished_at
            self._status["duration_ms"] = duration_ms(self._run_started_monotonic)
            self._status["error"] = error_payload
            self._status["current_stage"] = {
                "name": "failed",
                "status": "failed",
                "message": str(exc),
                "started_at": self._status["started_at"],
                "ended_at": finished_at,
                "duration_ms": self._status["duration_ms"],
            }
            self._write_status_locked()
        self.event("run_failed", f"CCR run failed: {exc}", stage="failed", level="error")
