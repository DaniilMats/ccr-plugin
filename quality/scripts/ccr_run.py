#!/usr/bin/env python3
"""Deterministic CCR orchestration harness.

Runs CCR end-to-end for local scopes, artifact replays, and GitLab merge requests:
- initializes an isolated run workspace
- materializes the review artifact
- computes deterministic routing input + route plan
- builds review context and static-analysis artifacts
- runs reviewer subprocesses in parallel
- consolidates reviewer findings into candidate findings
- prepares/runs verifier batches
- writes verified findings + final report artifacts

Examples:
    python3 ccr_run.py uncommitted --dry-run
    python3 ccr_run.py package:internal/service --project-dir ~/src/my-repo --dry-run
    python3 ccr_run.py https://gitlab.com/group/project/-/merge_requests/1234 --detach
    python3 ccr_run.py --artifact-file /tmp/review_artifact.txt --project-dir ~/src/my-repo --dry-run
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ccr_run_init import DEFAULT_BASE_DIR, _build_manifest, _build_run_id, _write_json
from ccr_routing import RoutingInput, build_routing_plan


_HERE = Path(__file__).resolve().parent
_LLM_PROXY_DIR = _HERE / "llm-proxy"
_CODE_REVIEW_SCRIPT = _LLM_PROXY_DIR / "code_review.py"
_CODE_REVIEW_VERIFY_SCRIPT = _LLM_PROXY_DIR / "code_review_verify.py"
_REVIEW_CONTEXT_SCRIPT = _LLM_PROXY_DIR / "review_context.py"
_STATIC_ANALYSIS_SCRIPT = _LLM_PROXY_DIR / "static_analysis.py"
_SHUFFLE_DIFF_SCRIPT = _LLM_PROXY_DIR / "shuffle_diff.py"

_LOCAL_SCOPE_RE = re.compile(r"^(?:uncommitted|commit:.+|branch:.+|file:.+|package:.+)$")
_MR_URL_RE = re.compile(r"^https?://(?P<host>[^/]+)/(?P<project>.+?)/-/merge_requests/(?P<iid>\d+)(?:[/?#].*)?$", re.IGNORECASE)
_DIFF_HEADER_RE = re.compile(r"^diff --git a/(.+?) b/(.+)$")
_HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<count>\d+))? @@")

_PERSONA_ORDER = ("security", "concurrency", "performance", "requirements")
_SEVERITY_ORDER = {"bug": 0, "warning": 1, "info": 2}
_REPORT_PERSONA_ORDER = ("requirements", "logic", "security", "concurrency", "performance")
_REPORT_LABELS = {
    "logic": "LOGIC",
    "security": "SECURITY",
    "concurrency": "CONCURRENCY",
    "performance": "PERFORMANCE",
    "requirements": "REQUIREMENTS",
}

_CRITICAL_SURFACE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("auth", ("auth", "jwt", "token", "oauth", "rbac", "permission", "session")),
    ("payments", ("payment", "billing", "invoice", "ledger", "checkout")),
    ("migrations", ("migration", "migrations/", ".sql", "schema/")),
    ("public-api", ("api/", "http/", "handler", "router", "grpc", "proto", "openapi")),
    ("shared-library", ("pkg/", "/pkg/", "shared", "common", "lib/")),
    ("infra/security-sensitive", ("terraform", "helm", "k8s", "iam", "policy", "secret", "vault", ".github/")),
)

_SECURITY_PATH_HINTS = (
    "auth",
    "jwt",
    "token",
    "secret",
    "oauth",
    "permission",
    "rbac",
    "login",
    "session",
    "crypto",
    "cert",
    "tls",
    "sql",
    "query",
    "filesystem",
    "upload",
)
_SECURITY_CONTENT_PATTERNS = (
    r"\bauthorization\b",
    r"\bbearer\b",
    r"\bpassword\b",
    r"\bsecret\b",
    r"\btoken\b",
    r"\bjwt\b",
    r"\boauth\b",
    r"\bcrypto\b",
    r"\btls\b",
    r"\bsql\b",
    r"exec\(",
    r"filepath\.",
    r"os\.(?:open|create|remove)",
    r"json\.unmarshal",
    r"yaml\.unmarshal",
)

_CONCURRENCY_PATH_HINTS = ("worker", "queue", "pool", "async", "parallel", "scheduler", "stream")
_CONCURRENCY_CONTENT_PATTERNS = (
    r"\bgo\s+func\b",
    r"\bgo\s+[A-Za-z_]",
    r"\bchan\b",
    r"<-",
    r"\bsync\.",
    r"\batomic\.",
    r"\bwaitgroup\b",
    r"\bmutex\b",
    r"\brwmutex\b",
    r"select\s*\{",
    r"context\.with(?:cancel|timeout|deadline)",
)

_PERFORMANCE_PATH_HINTS = ("cache", "batch", "stream", "handler", "search", "index", "query", "api", "http")
_PERFORMANCE_CONTENT_PATTERNS = (
    r"\bfor\b",
    r"\brange\b",
    r"json\.(?:marshal|unmarshal)",
    r"strings\.builder",
    r"bytes\.buffer",
    r"sort\.",
    r"append\(",
    r"make\(\[\]",
    r"http\.",
    r"sql\.",
)


@dataclass(frozen=True)
class ReviewTarget:
    mode: Literal["local", "mr", "artifact"]
    raw_target: str
    display_target: str
    scope: str | None = None
    artifact_file: Path | None = None
    mr_url: str | None = None
    mr_host: str | None = None
    mr_project: str | None = None
    mr_iid: int | None = None


@dataclass(frozen=True)
class ReviewerPassSpec:
    pass_name: str
    persona: str
    provider: str
    diff_kind: Literal["original", "shuffled"]


@dataclass(frozen=True)
class CandidateRecord:
    candidate_id: str
    persona: str
    severity: str
    file: str
    line: int
    message: str
    reviewers: list[str]
    consensus: str
    evidence_sources: list[str]

    def to_contract_dict(self) -> dict[str, Any]:
        return {
            "contract_version": "ccr.consolidated_candidate.v1",
            "candidate_id": self.candidate_id,
            "persona": self.persona,
            "severity": self.severity,
            "file": self.file,
            "line": self.line,
            "message": self.message,
            "reviewers": self.reviewers,
            "consensus": self.consensus,
            "evidence_sources": self.evidence_sources,
        }


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _duration_ms(started_at: float) -> int:
    return int((time.monotonic() - started_at) * 1000)


def _estimate_parallel_stage_duration(total_items: int, worker_count: int, timeout_sec: int) -> int:
    if total_items <= 0 or worker_count <= 0:
        return 0
    waves = (total_items + worker_count - 1) // worker_count
    return waves * max(timeout_sec, 0)


def _format_seconds_short(total_seconds: int | None) -> str:
    if total_seconds is None:
        return "n/a"
    if total_seconds < 60:
        return f"{total_seconds}s"
    minutes, seconds = divmod(total_seconds, 60)
    if minutes < 60:
        return f"{minutes}m" if seconds == 0 else f"{minutes}m{seconds}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h" if minutes == 0 and seconds == 0 else f"{hours}h{minutes}m{seconds}s"


def _format_milliseconds_short(total_ms: int | None) -> str:
    if total_ms is None:
        return "n/a"
    if total_ms < 1000:
        return f"{total_ms}ms"
    seconds = total_ms / 1000.0
    if seconds < 60:
        return f"{seconds:.1f}s"
    return _format_seconds_short(int(round(seconds)))


_STAGE_SEQUENCE: tuple[str, ...] = (
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
_STAGE_INDEX = {name: index + 1 for index, name in enumerate(_STAGE_SEQUENCE)}
_STAGE_TOTAL = len(_STAGE_SEQUENCE)


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
            "started_at": _utc_now(),
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
                "watch_cursor_file": manifest["watch_cursor_file"],
                "report_file": manifest["report_file"],
                "reviewers_file": manifest["reviewers_file"],
                "candidates_file": manifest["candidates_file"],
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
        if stage is None or stage not in _STAGE_INDEX:
            return {}
        return {
            "index": _STAGE_INDEX[stage],
            "total": _STAGE_TOTAL,
        }

    def _stage_label(self, stage: str | None) -> str:
        if stage is None:
            return "run"
        if stage not in _STAGE_INDEX:
            return stage
        meta = self._stage_meta(stage)
        return f"{meta['index']}/{meta['total']} {stage}"

    def _write_status_locked(self) -> None:
        now = _utc_now()
        self._revision += 1
        self._status["revision"] = self._revision
        self._status["updated_at"] = now
        self._status["heartbeat_at"] = now
        self._status["event_seq"] = self._event_seq
        _write_json(self.status_file, self._status)

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
                display_value = _format_seconds_short(int(value))
            elif key == "estimated_max_duration_sec":
                display_value = _format_seconds_short(int(value))
            elif key == "duration_ms":
                display_value = _format_milliseconds_short(int(value))
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
            "ts": _utc_now(),
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
        started_at = _utc_now()
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
        finished_at = _utc_now()
        stage_meta = self._stage_meta(stage)
        with self._lock:
            started_mono = self._stage_started_monotonic.get(stage)
            duration_ms = _duration_ms(started_mono) if started_mono is not None else None
            stage_payload = self._status["stages"].get(stage, {"name": stage, **stage_meta})
            stage_payload.update(
                {
                    "status": "completed",
                    "message": message,
                    "ended_at": finished_at,
                    "duration_ms": duration_ms,
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
                "duration_ms": duration_ms,
                **stage_meta,
            }
            self._write_status_locked()
        payload = dict(data)
        if duration_ms is not None:
            payload.setdefault("duration_ms", duration_ms)
        self.event("stage_completed", message, stage=stage, **payload)

    def fail_stage(self, stage: str, message: str, **data: Any) -> None:
        finished_at = _utc_now()
        stage_meta = self._stage_meta(stage)
        with self._lock:
            started_mono = self._stage_started_monotonic.get(stage)
            duration_ms = _duration_ms(started_mono) if started_mono is not None else None
            stage_payload = self._status["stages"].get(stage, {"name": stage, **stage_meta})
            stage_payload.update(
                {
                    "status": "failed",
                    "message": message,
                    "ended_at": finished_at,
                    "duration_ms": duration_ms,
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
                "duration_ms": duration_ms,
                **stage_meta,
            }
            self._write_status_locked()
        payload = dict(data)
        if duration_ms is not None:
            payload.setdefault("duration_ms", duration_ms)
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

    def reviewer_started(self, spec: ReviewerPassSpec) -> None:
        started_at = _utc_now()
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
        started_at = _utc_now()
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
        with self._lock:
            verification = self._status["verification"]
            batch_status = verification["batches"].setdefault(batch_id, {})
            payload = result.get("result") if isinstance(result.get("result"), dict) else {}
            verified_findings = payload.get("verified_findings") if isinstance(payload.get("verified_findings"), list) else []
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
                    "verified_findings": len(verified_findings),
                    "timed_out": result.get("timed_out", False),
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
        )

    def current_duration_ms(self) -> int:
        return _duration_ms(self._run_started_monotonic)

    def complete_run(self, summary: dict[str, Any]) -> None:
        finished_at = _utc_now()
        with self._lock:
            self._status["state"] = "completed"
            self._status["finished_at"] = finished_at
            self._status["duration_ms"] = _duration_ms(self._run_started_monotonic)
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
            _write_json(self.summary_file, summary)
        self.event(
            "run_completed",
            "CCR run completed",
            stage="completed",
            verified_count=summary.get("verified_finding_count"),
            duration_ms=summary.get("duration_ms"),
            report_file=summary.get("report_file"),
        )

    def fail_run(self, exc: Exception) -> None:
        finished_at = _utc_now()
        error_payload = {
            "type": type(exc).__name__,
            "message": str(exc),
        }
        with self._lock:
            self._status["state"] = "failed"
            self._status["finished_at"] = finished_at
            self._status["duration_ms"] = _duration_ms(self._run_started_monotonic)
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


_PASS_SPECS: dict[str, ReviewerPassSpec] = {
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


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _empty_static_analysis_result(reason: str | None = None) -> dict[str, Any]:
    return {
        "contract_version": "ccr.static_analysis.v1",
        "go_vet": [],
        "staticcheck": [],
        "gosec": [],
        "tools_available": {
            "go_vet": False,
            "staticcheck": False,
            "gosec": False,
        },
        "categories": {
            "logic": [],
            "security": [],
            "all": [],
        },
        "error": reason,
    }


def _build_review_context_placeholder(project_dir: Path | None, focus_files: list[str], reason: str) -> str:
    lines = [
        "## Review Target",
        f"- Repo root: {str(project_dir) if project_dir else '(unavailable)'}",
        f"- Repository/package context unavailable: {reason}",
    ]
    if focus_files:
        lines.append("- Focus files:")
        for path in focus_files:
            lines.append(f"  - {path}")
    return "\n".join(lines) + "\n"


def _find_project_root_for_path(path: Path) -> Path:
    cursor = path.resolve()
    if cursor.is_file():
        cursor = cursor.parent
    for candidate in (cursor, *cursor.parents):
        if (candidate / ".git").exists() or (candidate / "go.mod").exists():
            return candidate
    return cursor


def _normalize_scope_path(scope: str, base_dir: Path) -> str:
    if scope.startswith("file:"):
        raw = scope[len("file:") :].strip()
        expanded = Path(raw).expanduser()
        resolved = expanded if expanded.is_absolute() else (base_dir / expanded).resolve()
        return f"file:{resolved}"
    if scope.startswith("package:"):
        raw = scope[len("package:") :].strip()
        expanded = Path(raw).expanduser()
        resolved = expanded if expanded.is_absolute() else (base_dir / expanded).resolve()
        return f"package:{resolved}"
    return scope


def detect_review_target(raw_target: str | None, *, artifact_file: str | None = None, cwd: Path | None = None) -> ReviewTarget:
    current_dir = (cwd or Path.cwd()).resolve()

    if artifact_file:
        artifact_path = Path(artifact_file).expanduser().resolve()
        if not artifact_path.is_file():
            raise ValueError(f"artifact file does not exist: {artifact_path}")
        display = raw_target or f"artifact:{artifact_path}"
        return ReviewTarget(
            mode="artifact",
            raw_target=display,
            display_target=display,
            artifact_file=artifact_path,
        )

    if not raw_target:
        raise ValueError("a review target is required unless --artifact-file is provided")

    raw_target = raw_target.strip()
    if not raw_target:
        raise ValueError("review target must not be empty")

    mr_match = _MR_URL_RE.match(raw_target)
    if mr_match:
        return ReviewTarget(
            mode="mr",
            raw_target=raw_target,
            display_target=raw_target,
            mr_url=raw_target,
            mr_host=mr_match.group("host"),
            mr_project=mr_match.group("project"),
            mr_iid=int(mr_match.group("iid")),
        )

    if _LOCAL_SCOPE_RE.match(raw_target):
        return ReviewTarget(
            mode="local",
            raw_target=raw_target,
            display_target=raw_target,
            scope=raw_target,
        )

    raw_path = Path(raw_target).expanduser()
    candidate = raw_path if raw_path.is_absolute() else (current_dir / raw_path)
    candidate = candidate.resolve()
    if candidate.is_file() and candidate.suffix == ".go":
        return ReviewTarget(
            mode="local",
            raw_target=raw_target,
            display_target=f"file:{candidate}",
            scope=f"file:{candidate}",
        )
    if candidate.is_dir() and any(path.is_file() and path.suffix == ".go" for path in candidate.iterdir()):
        return ReviewTarget(
            mode="local",
            raw_target=raw_target,
            display_target=f"package:{candidate}",
            scope=f"package:{candidate}",
        )

    raise ValueError(
        "unsupported target. Expected MR URL, uncommitted, commit:<SHA>, branch:<BASE>, file:<PATH>, package:<PATH>, or a local Go file/package path"
    )


def _resolve_project_dir(target: ReviewTarget, explicit_project_dir: str | None, *, cwd: Path | None = None) -> Path | None:
    current_dir = (cwd or Path.cwd()).resolve()

    if explicit_project_dir:
        project_dir = Path(explicit_project_dir).expanduser().resolve()
        if not project_dir.is_dir():
            raise ValueError(f"project directory does not exist: {project_dir}")
        return project_dir

    if target.mode == "local":
        assert target.scope is not None
        if target.scope.startswith(("file:", "package:")):
            raw_path = Path(target.scope.split(":", 1)[1])
            return _find_project_root_for_path(raw_path)
        return current_dir

    if target.mode == "artifact":
        return current_dir if current_dir.is_dir() else None

    if target.mode != "mr":
        return None

    if (current_dir / ".git").is_dir() and _git_remote_matches_project(current_dir, target.mr_project or ""):
        return current_dir

    project_name = (target.mr_project or "").split("/")[-1]
    host = target.mr_host or ""
    candidate_dirs = [
        Path.home() / "GolandProjects" / project_name,
        Path.home() / "projects" / project_name,
        Path.home() / "Projects" / project_name,
        Path.home() / project_name,
    ]
    if target.mr_project:
        candidate_dirs.append(Path.home() / "go" / "src" / host / target.mr_project)

    for candidate in candidate_dirs:
        if candidate.is_dir() and (candidate / ".git").is_dir():
            return candidate.resolve()
    return None


def _git_remote_matches_project(project_dir: Path, project_path: str) -> bool:
    if not project_path:
        return False
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return False
    if result.returncode != 0:
        return False
    normalized_remote = result.stdout.strip().lower().removesuffix(".git")
    normalized_project = project_path.lower().strip("/")
    return normalized_project in normalized_remote


def _prepare_run_manifest(base_dir: Path, run_id: str | None, output_file: str | None) -> dict[str, Any]:
    manifest = _build_manifest(base_dir, run_id or _build_run_id())
    manifest_path = Path(manifest["manifest_file"])
    _write_json(manifest_path, manifest)
    if output_file:
        _write_json(Path(output_file).expanduser().resolve(), manifest)
    return manifest


def _load_json_file(path: Path, *, default: Any = None) -> Any:
    try:
        return json.loads(_read_text(path))
    except (OSError, json.JSONDecodeError):
        return default


def _run_command(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def _materialize_requirements(
    manifest: dict[str, Any],
    *,
    requirements_text: str | None,
    requirements_file: str | None,
    requirements_stdin: bool,
    use_mr_description: bool,
    mr_metadata: dict[str, Any] | None,
) -> str:
    text = ""
    chosen_sources = sum(
        1
        for enabled in (bool(requirements_text), bool(requirements_file), bool(requirements_stdin))
        if enabled
    )
    if chosen_sources > 1:
        raise ValueError("use only one of --requirements-text, --requirements-file, or --requirements-stdin")

    if requirements_text:
        text = requirements_text.strip()
    elif requirements_file:
        req_path = Path(requirements_file).expanduser().resolve()
        if not req_path.is_file():
            raise ValueError(f"requirements file does not exist: {req_path}")
        text = _read_text(req_path).strip()
    elif requirements_stdin:
        text = sys.stdin.read().strip()
    elif use_mr_description:
        text = str((mr_metadata or {}).get("description") or "").strip()

    requirements_path = Path(manifest["requirements_file"])
    if text:
        _write_text(requirements_path, text + "\n")
    return text


def _gitlab_api(project: str, path_suffix: str) -> Any:
    encoded_project = quote(project, safe="")
    api_path = f"projects/{encoded_project}/{path_suffix.lstrip('/')}"
    result = _run_command(["glab", "api", api_path])
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"glab api failed for {api_path}: {stderr}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"glab api returned invalid JSON for {api_path}: {exc}") from exc


def _render_gitlab_change(change: dict[str, Any]) -> str:
    old_path = change.get("old_path") or change.get("new_path") or "unknown"
    new_path = change.get("new_path") or change.get("old_path") or old_path
    diff_body = str(change.get("diff") or "").strip("\n")

    if diff_body.startswith("diff --git "):
        return diff_body

    header = [f"diff --git a/{old_path} b/{new_path}"]
    has_old_header = re.search(r"(?m)^---\s", diff_body) is not None
    has_new_header = re.search(r"(?m)^\+\+\+\s", diff_body) is not None

    if change.get("new_file") and not has_old_header and not has_new_header:
        header.extend([
            "new file mode 100644",
            "index 0000000..1111111",
            "--- /dev/null",
            f"+++ b/{new_path}",
        ])
    elif change.get("deleted_file") and not has_old_header and not has_new_header:
        header.extend([
            "deleted file mode 100644",
            "index 1111111..0000000",
            f"--- a/{old_path}",
            "+++ /dev/null",
        ])
    else:
        if not has_old_header:
            header.append(f"--- a/{old_path}")
        if not has_new_header:
            header.append(f"+++ b/{new_path}")

    if diff_body:
        header.append(diff_body)
    return "\n".join(header)


def _fetch_mr_artifact(target: ReviewTarget) -> tuple[dict[str, Any], str]:
    assert target.mr_project is not None
    assert target.mr_iid is not None
    metadata = _gitlab_api(target.mr_project, f"merge_requests/{target.mr_iid}")
    changes_payload = _gitlab_api(target.mr_project, f"merge_requests/{target.mr_iid}/changes")
    changes = changes_payload.get("changes") or []
    diff_blocks = [_render_gitlab_change(change) for change in changes]
    diff_text = "\n\n".join(block for block in diff_blocks if block.strip())
    return metadata, diff_text or "(no changes)"


def _materialize_review_artifact(
    manifest: dict[str, Any],
    target: ReviewTarget,
    *,
    project_dir: Path | None,
    mr_metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    diff_file = Path(manifest["diff_file"])
    mr_metadata_file = Path(manifest["mr_metadata_file"])
    artifact_log = Path(manifest["logs_dir"]) / "artifact.stderr.txt"

    if target.mode == "artifact":
        assert target.artifact_file is not None
        shutil.copyfile(target.artifact_file, diff_file)
        artifact_log.write_text("", encoding="utf-8")
        return mr_metadata or {}

    if target.mode == "mr":
        metadata, diff_text = _fetch_mr_artifact(target)
        _write_text(diff_file, diff_text.rstrip() + "\n")
        _write_json(mr_metadata_file, metadata)
        artifact_log.write_text("", encoding="utf-8")
        return metadata

    assert target.scope is not None
    if project_dir is None:
        raise ValueError("local scopes require a project directory")
    normalized_scope = _normalize_scope_path(target.scope, project_dir)
    cmd = [
        sys.executable,
        str(_CODE_REVIEW_SCRIPT),
        "--scope",
        normalized_scope,
        "--artifact-output",
        str(diff_file),
        "--artifact-only",
    ]
    result = _run_command(cmd, cwd=project_dir)
    _write_text(artifact_log, result.stderr)
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"failed to generate review artifact: {stderr}")
    if not diff_file.is_file():
        raise RuntimeError("artifact generation completed without producing the diff file")
    return mr_metadata or {}


def _extract_changed_files(diff_text: str) -> list[str]:
    files: list[str] = []
    for raw_line in diff_text.splitlines():
        match = _DIFF_HEADER_RE.match(raw_line.strip())
        if not match:
            continue
        a_path, b_path = match.groups()
        candidate = b_path if b_path != "/dev/null" else a_path
        if candidate == "/dev/null":
            continue
        files.append(candidate)
    return _dedupe_preserve_order(files)


def _count_changed_lines(diff_text: str) -> int:
    count = 0
    for raw_line in diff_text.splitlines():
        if raw_line.startswith("+++") or raw_line.startswith("---"):
            continue
        if raw_line.startswith("+") or raw_line.startswith("-"):
            count += 1
    return count


def _extract_diff_content_text(diff_text: str) -> str:
    content_lines: list[str] = []
    for raw_line in diff_text.splitlines():
        if raw_line.startswith(("+++", "---", "@@", "diff --git ", "index ", "new file mode", "deleted file mode")):
            continue
        if raw_line.startswith("+") or raw_line.startswith("-"):
            content_lines.append(raw_line[1:])
    return "\n".join(content_lines)


def _match_pattern_score(text: str, patterns: tuple[str, ...], *, cap: int = 4) -> int:
    score = 0
    for pattern in patterns:
        if re.search(pattern, text):
            score += 1
            if score >= cap:
                return cap
    return score


def _path_hint_score(paths: list[str], hints: tuple[str, ...], *, weight: int = 2) -> int:
    score = 0
    for path in paths:
        lowered = path.lower()
        if any(hint in lowered for hint in hints):
            score += weight
    return score


def _detect_critical_surfaces(changed_files: list[str]) -> list[str]:
    surfaces: list[str] = []
    lowered_paths = [path.lower() for path in changed_files]
    for label, hints in _CRITICAL_SURFACE_RULES:
        if any(any(hint in path for hint in hints) for path in lowered_paths):
            surfaces.append(label)
    return surfaces


def build_route_input(
    artifact_text: str,
    *,
    requirements_text: str,
    requirements_from_mr_description: bool,
    user_requested_exhaustive: bool,
    behavior_change_ambiguous: bool,
) -> dict[str, Any]:
    changed_files = _extract_changed_files(artifact_text)
    changed_lines = _count_changed_lines(artifact_text)
    lowered_artifact = _extract_diff_content_text(artifact_text).lower()
    has_requirements = bool(requirements_text.strip())

    scores = {
        "security": 0,
        "concurrency": 0,
        "performance": 0,
        "requirements": 2 if has_requirements or requirements_from_mr_description else 0,
    }

    scores["security"] += _path_hint_score(changed_files, _SECURITY_PATH_HINTS)
    scores["security"] += _match_pattern_score(lowered_artifact, _SECURITY_CONTENT_PATTERNS)

    scores["concurrency"] += _path_hint_score(changed_files, _CONCURRENCY_PATH_HINTS)
    scores["concurrency"] += _match_pattern_score(lowered_artifact, _CONCURRENCY_CONTENT_PATTERNS)

    perf_path_score = _path_hint_score(changed_files, _PERFORMANCE_PATH_HINTS, weight=1)
    perf_content_score = _match_pattern_score(lowered_artifact, _PERFORMANCE_CONTENT_PATTERNS)
    if perf_path_score >= 2 or perf_content_score >= 2 or changed_lines >= 120:
        scores["performance"] += max(perf_path_score, 1)
        scores["performance"] += max(perf_content_score, 1)

    critical_surfaces = _detect_critical_surfaces(changed_files)
    if "public-api" in critical_surfaces and changed_lines >= 40:
        scores["performance"] += 1
    if "auth" in critical_surfaces:
        scores["security"] += 2

    triggered_personas = [persona for persona in _PERSONA_ORDER if scores[persona] > 0]
    highest_risk_personas = sorted(
        triggered_personas,
        key=lambda persona: (-scores[persona], _PERSONA_ORDER.index(persona)),
    )[:2]

    return {
        "contract_version": "ccr.route_input.v1",
        "changed_files": changed_files,
        "changed_file_count": len(changed_files),
        "changed_lines": changed_lines,
        "has_requirements": has_requirements,
        "requirements_from_mr_description": requirements_from_mr_description,
        "user_requested_exhaustive": user_requested_exhaustive,
        "behavior_change_ambiguous": behavior_change_ambiguous,
        "triggered_personas": triggered_personas,
        "highest_risk_personas": highest_risk_personas,
        "critical_surfaces": critical_surfaces,
    }


def _plan_route(manifest: dict[str, Any], route_input_payload: dict[str, Any]) -> dict[str, Any]:
    route_input_file = Path(manifest["route_input_file"])
    route_plan_file = Path(manifest["route_plan_file"])
    route_err_file = Path(manifest["route_helper_err_file"])

    _write_json(route_input_file, route_input_payload)

    try:
        request = RoutingInput.model_validate(route_input_payload)
        plan = build_routing_plan(request).model_dump()
        _write_json(route_plan_file, plan)
        _write_text(route_err_file, "")
        return plan
    except Exception as exc:  # noqa: BLE001
        _write_text(route_err_file, f"{type(exc).__name__}: {exc}\n")
        raise


def _build_review_context_artifact(manifest: dict[str, Any], project_dir: Path | None, artifact_text: str) -> None:
    review_context_file = Path(manifest["review_context_file"])
    context_log = Path(manifest["logs_dir"]) / "review_context.stderr.txt"
    focus_files = _extract_changed_files(artifact_text)

    if project_dir is None or not project_dir.is_dir():
        _write_text(review_context_file, _build_review_context_placeholder(project_dir, focus_files, "project directory unavailable"))
        _write_text(context_log, "project directory unavailable\n")
        return

    cmd = [
        sys.executable,
        str(_REVIEW_CONTEXT_SCRIPT),
        "--project-dir",
        str(project_dir),
        "--artifact-file",
        manifest["diff_file"],
        "--output-file",
        manifest["review_context_file"],
    ]
    result = _run_command(cmd, cwd=project_dir)
    _write_text(context_log, result.stderr)
    if result.returncode != 0 or not review_context_file.is_file():
        _write_text(
            review_context_file,
            _build_review_context_placeholder(project_dir, focus_files, result.stderr.strip() or "review_context helper failed"),
        )


def _write_static_analysis_artifact(
    manifest: dict[str, Any],
    project_dir: Path | None,
    changed_files: list[str],
    *,
    dry_run: bool,
) -> dict[str, Any]:
    output_path = Path(manifest["static_analysis_file"])
    log_path = Path(manifest["logs_dir"]) / "static_analysis.stderr.txt"

    if dry_run:
        payload = _empty_static_analysis_result("dry-run: static analysis skipped")
        _write_json(output_path, payload)
        _write_text(log_path, "dry-run: static analysis skipped\n")
        return payload

    if project_dir is None or not project_dir.is_dir():
        payload = _empty_static_analysis_result("project directory unavailable")
        _write_json(output_path, payload)
        _write_text(log_path, "project directory unavailable\n")
        return payload

    if not (project_dir / "go.mod").is_file():
        payload = _empty_static_analysis_result("go.mod not found")
        _write_json(output_path, payload)
        _write_text(log_path, "go.mod not found\n")
        return payload

    cmd = [
        sys.executable,
        str(_STATIC_ANALYSIS_SCRIPT),
        "--project-dir",
        str(project_dir),
        "--output-file",
        manifest["static_analysis_file"],
    ]
    if changed_files:
        cmd.extend(["--changed-files", ",".join(changed_files)])
    result = _run_command(cmd, cwd=project_dir)
    _write_text(log_path, result.stderr)

    payload = _load_json_file(output_path)
    if result.returncode != 0 or not isinstance(payload, dict):
        payload = _empty_static_analysis_result(result.stderr.strip() or "static analysis helper failed")
        _write_json(output_path, payload)
    return payload


def _build_shuffled_diff(manifest: dict[str, Any], artifact_text: str) -> None:
    diff_hash = hashlib.sha256(artifact_text.encode("utf-8")).hexdigest()
    seed = int(diff_hash[:8], 16)
    cmd = [
        sys.executable,
        str(_SHUFFLE_DIFF_SCRIPT),
        "--input-file",
        manifest["diff_file"],
        "--output-file",
        manifest["shuffled_diff_file"],
        "--seed",
        str(seed),
    ]
    result = _run_command(cmd)
    log_path = Path(manifest["logs_dir"]) / "shuffle_diff.stderr.txt"
    _write_text(log_path, result.stderr)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "shuffle_diff.py failed")


def _resolve_worker_count(requested_workers: int, total_items: int, *, auto_cap: int) -> int:
    if total_items <= 0:
        return 0
    if requested_workers > 0:
        return max(1, min(total_items, requested_workers))
    return max(1, min(total_items, auto_cap))


def _build_reviewer_command(
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


def _run_reviewer_pass(
    spec: ReviewerPassSpec,
    *,
    manifest: dict[str, Any],
    project_dir: Path | None,
    requirements_available: bool,
    dry_run: bool,
    timeout_sec: int,
) -> dict[str, Any]:
    cmd, output_path = _build_reviewer_command(
        spec,
        manifest=manifest,
        requirements_available=requirements_available,
        dry_run=dry_run,
        timeout_sec=timeout_sec,
    )
    stderr_path = Path(manifest["logs_dir"]) / f"reviewer.{spec.pass_name}.stderr.txt"
    started_at = _utc_now()
    started_mono = time.monotonic()
    timed_out = False

    cwd = project_dir if project_dir and project_dir.is_dir() else Path.cwd()
    try:
        result = _run_command(cmd, cwd=cwd, timeout=timeout_sec + 30)
        stderr = result.stderr
        exit_code = result.returncode
    except subprocess.TimeoutExpired:
        stderr = f"timed out after {timeout_sec + 30} seconds\n"
        exit_code = -1
        timed_out = True

    finished_at = _utc_now()
    duration_ms = _duration_ms(started_mono)

    _write_text(stderr_path, stderr)
    output_payload = _load_json_file(Path(output_path), default={})
    if not isinstance(output_payload, dict):
        output_payload = {}

    summary = str(output_payload.get("summary") or "Reviewer did not produce structured output.")
    findings = output_payload.get("findings") if isinstance(output_payload.get("findings"), list) else []
    status = "succeeded" if exit_code == 0 else "failed"

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
        "duration_ms": duration_ms,
        "output_file": output_path,
        "stderr_file": str(stderr_path),
        "finding_count": len(findings),
        "summary": summary,
        "result": output_payload,
    }


def _run_reviewers(
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
    specs = [_PASS_SPECS[pass_name] for pass_name in passes]
    worker_count = _resolve_worker_count(max_reviewer_workers, len(specs), auto_cap=14)
    estimated_max_duration_sec = _estimate_parallel_stage_duration(len(specs), worker_count, reviewer_timeout_sec)
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
                _run_reviewer_pass,
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
    reviewers_summary = {
        "planned_passes": len(passes),
        "worker_count": worker_count,
        "timeout_sec": reviewer_timeout_sec,
        "estimated_max_duration_sec": estimated_max_duration_sec,
        "completed_passes": len(results),
        "succeeded_passes": sum(1 for item in results if item["status"] == "succeeded"),
        "failed_passes": sum(1 for item in results if item["status"] != "succeeded"),
        "total_findings": sum(item["finding_count"] for item in results),
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
    _write_json(Path(manifest["reviewers_file"]), reviewers_payload)
    return results, reviewers_summary


def _severity_rank(severity: str) -> int:
    return _SEVERITY_ORDER.get(severity, 99)


def _message_key(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _combine_messages(messages: list[str]) -> str:
    unique: list[str] = []
    seen: set[str] = set()
    for message in messages:
        normalized = _message_key(message)
        if normalized in seen:
            continue
        seen.add(normalized)
        unique.append(message.strip())
    if not unique:
        return "Reviewer reported an issue but did not provide details."
    if len(unique) == 1:
        return unique[0]
    head = unique[0]
    tail = "\nAdditional reviewer notes:\n" + "\n".join(f"- {item}" for item in unique[1:])
    return head + tail


def _find_static_analysis_evidence(static_analysis_payload: dict[str, Any], file_path: str, line: int) -> list[str]:
    evidence: list[str] = ["diff_hunk"]
    for tool_key in ("go_vet", "staticcheck", "gosec"):
        findings = static_analysis_payload.get(tool_key)
        if not isinstance(findings, list):
            continue
        if any(
            isinstance(finding, dict)
            and finding.get("file") == file_path
            and abs(int(finding.get("line", 0) or 0) - line) <= 3
            for finding in findings
        ):
            evidence.append(tool_key)
    return _dedupe_preserve_order(evidence)


def _cluster_persona_findings(items: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    clusters: list[list[dict[str, Any]]] = []
    for item in sorted(items, key=lambda value: (value["file"], int(value["line"]), _severity_rank(value["severity"]), value["pass_name"])):
        assigned = False
        for cluster in clusters:
            head = cluster[0]
            if item["file"] == head["file"] and abs(int(item["line"]) - int(head["line"])) <= 3:
                cluster.append(item)
                assigned = True
                break
        if not assigned:
            clusters.append([item])
    return clusters


def _build_candidates(
    reviewer_results: list[dict[str, Any]],
    *,
    manifest: dict[str, Any],
    route_plan: dict[str, Any],
    static_analysis_payload: dict[str, Any],
) -> tuple[list[CandidateRecord], dict[str, Any]]:
    pass_counts = route_plan.get("pass_counts") if isinstance(route_plan.get("pass_counts"), dict) else {}
    flattened: list[dict[str, Any]] = []
    for review in reviewer_results:
        output = review.get("result") if isinstance(review.get("result"), dict) else {}
        findings = output.get("findings") if isinstance(output.get("findings"), list) else []
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            file_path = str(finding.get("file") or "").strip()
            line = int(finding.get("line") or 0)
            message = str(finding.get("message") or "").strip()
            severity = str(finding.get("severity") or "info").strip().lower()
            if not file_path or line < 1 or not message:
                continue
            if severity not in _SEVERITY_ORDER:
                severity = "info"
            flattened.append(
                {
                    "pass_name": review["pass_name"],
                    "persona": review["persona"],
                    "provider": review["provider"],
                    "file": file_path,
                    "line": line,
                    "message": message,
                    "severity": severity,
                }
            )

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for finding in flattened:
        grouped[(finding["persona"], finding["file"])] .append(finding)

    candidates: list[CandidateRecord] = []
    next_id = 1
    for (persona, _file_path), findings in sorted(grouped.items(), key=lambda item: item[0]):
        for cluster in _cluster_persona_findings(findings):
            messages = [item["message"] for item in cluster]
            reviewers = sorted({item["pass_name"] for item in cluster})
            severity = min((item["severity"] for item in cluster), key=_severity_rank)
            line = min(int(item["line"]) for item in cluster)
            file_path = cluster[0]["file"]
            available_persona_passes = int(pass_counts.get(persona, 0) or 0) or len(reviewers)
            consensus = f"{len(reviewers)}/{available_persona_passes}"
            candidate = CandidateRecord(
                candidate_id=f"F{next_id}",
                persona=persona,
                severity=severity,
                file=file_path,
                line=line,
                message=_combine_messages(messages),
                reviewers=reviewers,
                consensus=consensus,
                evidence_sources=_find_static_analysis_evidence(static_analysis_payload, file_path, line),
            )
            candidates.append(candidate)
            next_id += 1

    candidates_summary = {
        "candidate_count": len(candidates),
        "source_finding_count": len(flattened),
    }
    candidates_payload = {
        "contract_version": "ccr.candidates_manifest.v1",
        "candidates": [candidate.to_contract_dict() for candidate in candidates],
        "summary": candidates_summary,
    }
    _write_json(Path(manifest["candidates_file"]), candidates_payload)
    return candidates, candidates_summary


def _extract_diff_blocks(diff_text: str) -> dict[str, str]:
    blocks: dict[str, str] = {}
    current_file: str | None = None
    current_lines: list[str] = []
    for raw_line in diff_text.splitlines(keepends=True):
        header_match = _DIFF_HEADER_RE.match(raw_line.rstrip("\n"))
        if header_match:
            if current_file is not None:
                blocks[current_file] = "".join(current_lines).strip()
            a_path, b_path = header_match.groups()
            current_file = b_path if b_path != "/dev/null" else a_path
            current_lines = [raw_line]
            continue
        current_lines.append(raw_line)
    if current_file is not None:
        blocks[current_file] = "".join(current_lines).strip()
    return blocks


def _extract_file_context(project_dir: Path | None, rel_path: str, target_lines: list[int], radius: int = 20) -> str:
    if project_dir is None:
        return "Local checkout unavailable."
    path = project_dir / rel_path
    if not path.is_file():
        return "Local file unavailable in this checkout."
    lines = _read_text(path).splitlines()
    if not lines:
        return "(empty file)"
    start = max(1, min(target_lines) - radius)
    end = min(len(lines), max(target_lines) + radius)
    snippet = []
    for line_no in range(start, end + 1):
        snippet.append(f"{line_no:4d}: {lines[line_no - 1]}")
    return "\n".join(snippet)


def _write_verification_batches(
    manifest: dict[str, Any],
    *,
    candidates: list[CandidateRecord],
    artifact_text: str,
    project_dir: Path | None,
    requirements_text: str,
) -> list[dict[str, Any]]:
    diff_blocks = _extract_diff_blocks(artifact_text)
    grouped_by_file: dict[str, list[CandidateRecord]] = defaultdict(list)
    for candidate in candidates:
        grouped_by_file[candidate.file].append(candidate)

    batches: list[dict[str, Any]] = []
    batch_index = 1
    for file_path in sorted(grouped_by_file):
        file_candidates = grouped_by_file[file_path]
        for offset in range(0, len(file_candidates), 5):
            chunk = file_candidates[offset : offset + 5]
            batch_payload = {
                "contract_version": "ccr.verification_batch.v1",
                "file": file_path,
                "diff_hunk": diff_blocks.get(file_path, ""),
                "file_context": _extract_file_context(project_dir, file_path, [candidate.line for candidate in chunk]),
                "requirements": requirements_text,
                "candidates": [
                    {
                        "candidate_id": candidate.candidate_id,
                        "file": candidate.file,
                        "line": candidate.line,
                        "message": candidate.message,
                    }
                    for candidate in chunk
                ],
            }
            batch_path = Path(manifest["verify_batch_dir"]) / f"verify_batch_{batch_index:03d}.json"
            _write_json(batch_path, batch_payload)
            batches.append(
                {
                    "batch_id": f"B{batch_index}",
                    "batch_file": str(batch_path),
                    "payload": batch_payload,
                }
            )
            batch_index += 1
    return batches


def _run_single_verification_batch(
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
    started_at = _utc_now()
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
            result = _run_command(cmd, cwd=cwd, timeout=verifier_timeout_sec + 30)
            last_exit_code = result.returncode
            last_stderr = result.stderr
        except subprocess.TimeoutExpired:
            last_exit_code = -1
            last_stderr = f"timed out after {verifier_timeout_sec + 30} seconds\n"
            timed_out = True
        if output_path.is_file() and last_exit_code == 0:
            break

    finished_at = _utc_now()
    duration_ms = _duration_ms(started_mono)

    _write_text(stderr_path, last_stderr)
    payload = _load_json_file(output_path, default={})
    if not isinstance(payload, dict):
        payload = {}

    return {
        "batch_id": batch["batch_id"],
        "batch_file": str(batch_path),
        "output_file": str(output_path),
        "stderr_file": str(stderr_path),
        "provider": used_provider,
        "attempted_providers": attempted_providers,
        "exit_code": last_exit_code,
        "status": "succeeded" if last_exit_code == 0 else "failed",
        "timed_out": timed_out,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": duration_ms,
        "result": payload,
    }


def _parse_consensus_support(consensus: str) -> tuple[int, int]:
    match = re.match(r"^(\d+)/(\d+)$", consensus)
    if not match:
        return 0, 0
    return int(match.group(1)), int(match.group(2))


def _merge_verified_findings(
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
            support_count, _ = _parse_consensus_support(candidate.consensus)
            if verdict == "uncertain" and support_count < 2:
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
                    "reviewers": candidate.reviewers,
                    "consensus": candidate.consensus,
                    "evidence_sources": candidate.evidence_sources,
                    "tentative": verdict == "uncertain",
                }
            )

    merged.sort(key=lambda item: (_REPORT_PERSONA_ORDER.index(item["persona"]), _severity_rank(item["severity"]), item["file"], item["line"], item["candidate_id"]))
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
    _write_json(Path(manifest["verified_findings_file"]), payload)
    return merged


def _run_verification(
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
    if not candidates:
        summary = {
            "verified_count": 0,
            "batch_count": 0,
            "successful_batches": 0,
            "failed_batches": 0,
            "worker_count": 0,
            "timeout_sec": verifier_timeout_sec,
            "estimated_max_duration_sec": 0,
        }
        payload = {
            "contract_version": "ccr.verified_findings.v1",
            "verified_findings": [],
            "verification_batches": [],
            "summary": summary,
        }
        _write_json(Path(manifest["verified_findings_file"]), payload)
        return [], summary

    batches = _write_verification_batches(
        manifest,
        candidates=candidates,
        artifact_text=artifact_text,
        project_dir=project_dir,
        requirements_text=requirements_text,
    )

    worker_count = _resolve_worker_count(max_verifier_workers, len(batches), auto_cap=8)
    estimated_max_duration_sec = _estimate_parallel_stage_duration(len(batches), worker_count, verifier_timeout_sec)
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
                _run_single_verification_batch,
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
    verified_findings = _merge_verified_findings(manifest, candidates=candidates, verification_results=results)
    verification_summary = {
        "verified_count": len(verified_findings),
        "batch_count": len(results),
        "successful_batches": sum(1 for batch in results if batch["status"] == "succeeded"),
        "failed_batches": sum(1 for batch in results if batch["status"] != "succeeded"),
        "worker_count": worker_count,
        "timeout_sec": verifier_timeout_sec,
        "estimated_max_duration_sec": estimated_max_duration_sec,
    }
    verified_payload = _load_json_file(Path(manifest["verified_findings_file"]), default={})
    if isinstance(verified_payload, dict):
        verified_payload["summary"] = verification_summary
        _write_json(Path(manifest["verified_findings_file"]), verified_payload)
    return verified_findings, verification_summary


def _format_report(verified_findings: list[dict[str, Any]]) -> str:
    if not verified_findings:
        return "Проверенных замечаний не найдено.\n"

    lines: list[str] = []
    fallback_finding_number = 1
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for finding in verified_findings:
        grouped[finding["persona"]].append(finding)

    for persona in _REPORT_PERSONA_ORDER:
        items = grouped.get(persona)
        if not items:
            continue
        lines.append(f"## [{_REPORT_LABELS[persona]}]")
        for item in sorted(items, key=lambda entry: (_severity_rank(entry["severity"]), entry["file"], entry["line"], entry["candidate_id"])):
            confidence = item["consensus"]
            if item.get("tentative"):
                confidence = f"{confidence} — tentative"
            finding_number = int(item.get("finding_number") or 0)
            if finding_number <= 0:
                finding_number = fallback_finding_number
            lines.append(
                f"{finding_number}. [{item['severity'].upper()}] {item['file']}:{item['line']} — {confidence} — {item['message']}"
            )
            if item.get("evidence"):
                lines.append(f"   Evidence: {item['evidence']}")
            fallback_finding_number = max(fallback_finding_number, finding_number + 1)
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _write_report(manifest: dict[str, Any], verified_findings: list[dict[str, Any]]) -> str:
    report_text = _format_report(verified_findings)
    _write_text(Path(manifest["report_file"]), report_text)
    return report_text


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ccr-run",
        description="Deterministic CCR review harness.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s uncommitted --dry-run\n"
            "  %(prog)s package:internal/auth --project-dir tests/fixtures/go_repo --dry-run\n"
            "  %(prog)s https://gitlab.com/group/project/-/merge_requests/1234\n"
            "  %(prog)s --artifact-file /tmp/review_artifact.txt --project-dir ~/src/repo --dry-run\n"
        ),
    )
    parser.add_argument(
        "target",
        nargs="?",
        help="MR URL, local diff scope, file:/package: scope, or raw Go file/package path.",
    )
    parser.add_argument(
        "--artifact-file",
        default=None,
        help="Replay mode: use an existing review artifact instead of generating/fetching one.",
    )
    parser.add_argument(
        "--project-dir",
        default=None,
        help="Optional local checkout path used for git diff generation, review_context, and static analysis.",
    )
    parser.add_argument(
        "--requirements-text",
        default=None,
        help="Inline requirements/spec text to inject into reviewer prompts.",
    )
    parser.add_argument(
        "--requirements-file",
        default=None,
        help="Path to requirements/spec text to inject into reviewer prompts.",
    )
    parser.add_argument(
        "--requirements-stdin",
        action="store_true",
        help="Read requirements/spec text from stdin and persist it into the run workspace.",
    )
    parser.add_argument(
        "--use-mr-description-as-requirements",
        action="store_true",
        help="For MR mode, use the MR description as requirements/spec text.",
    )
    parser.add_argument(
        "--user-requested-exhaustive",
        action="store_true",
        help="Force exhaustive routing when building the route plan.",
    )
    parser.add_argument(
        "--behavior-change-ambiguous",
        action="store_true",
        help="Flag the change as behavior-changing but ambiguous to the router.",
    )
    parser.add_argument(
        "--reviewer-timeout",
        type=int,
        default=600,
        help="Per-reviewer timeout in seconds (default: 600).",
    )
    parser.add_argument(
        "--max-reviewer-workers",
        type=int,
        default=0,
        help="Maximum parallel reviewer workers. 0 means auto (default: run all planned passes in parallel).",
    )
    parser.add_argument(
        "--verifier-timeout",
        type=int,
        default=300,
        help="Per-verifier timeout in seconds (default: 300).",
    )
    parser.add_argument(
        "--max-verifier-workers",
        type=int,
        default=0,
        help="Maximum parallel verifier workers. 0 means auto (default: up to 8 batches in parallel).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run reviewers/verifiers in deterministic dry-run mode and skip static analysis.",
    )
    parser.add_argument(
        "--base-dir",
        default=DEFAULT_BASE_DIR,
        help=f"Base directory for CCR runs (default: {DEFAULT_BASE_DIR}).",
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="Optional explicit run id. Normally auto-generated.",
    )
    parser.add_argument(
        "--manifest-output",
        default=None,
        help="Optional extra path to also write the run manifest JSON.",
    )
    parser.add_argument(
        "--output-file",
        default=None,
        help="Optional path to also write the final run summary JSON.",
    )
    parser.add_argument(
        "--detach",
        action="store_true",
        help="Launch the harness in the background and return a run launch payload immediately.",
    )
    parser.add_argument(
        "--detached-child",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    return parser


def _build_detached_child_args(
    raw_args: list[str],
    *,
    run_id: str,
    requirements_file_override: str | None,
) -> list[str]:
    child_args: list[str] = []
    run_id_present = False
    for token in raw_args:
        if token in {"--detach", "--detached-child", "--requirements-stdin"}:
            continue
        child_args.append(token)
        if token == "--run-id":
            run_id_present = True
    if not run_id_present:
        child_args.extend(["--run-id", run_id])
    if requirements_file_override:
        child_args.extend(["--requirements-file", requirements_file_override])
    child_args.append("--detached-child")
    return child_args


def launch_ccr_detached(args: argparse.Namespace, raw_args: list[str]) -> dict[str, Any]:
    detection_cwd = Path(args.project_dir).expanduser().resolve() if args.project_dir else None
    target = detect_review_target(args.target, artifact_file=args.artifact_file, cwd=detection_cwd)
    project_dir = _resolve_project_dir(target, args.project_dir)

    run_id = args.run_id or _build_run_id()
    manifest = _prepare_run_manifest(
        Path(args.base_dir).expanduser().resolve(),
        run_id,
        args.manifest_output,
    )

    requirements_file_override: str | None = None
    if args.requirements_stdin:
        requirements_text = sys.stdin.read()
        _write_text(Path(manifest["requirements_file"]), requirements_text)
        requirements_file_override = manifest["requirements_file"]

    child_args = _build_detached_child_args(
        raw_args,
        run_id=run_id,
        requirements_file_override=requirements_file_override,
    )
    cmd = [sys.executable, str(Path(__file__).resolve()), *child_args]

    stdout_handle = open(manifest["harness_stdout_file"], "w", encoding="utf-8")
    stderr_handle = open(manifest["harness_stderr_file"], "w", encoding="utf-8")
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(Path.cwd()),
            stdin=subprocess.DEVNULL,
            stdout=stdout_handle,
            stderr=stderr_handle,
            start_new_session=True,
            text=True,
        )
    finally:
        stdout_handle.close()
        stderr_handle.close()

    launch_payload = {
        "contract_version": "ccr.run_launch.v1",
        "run_id": manifest["run_id"],
        "pid": proc.pid,
        "mode": target.mode,
        "target": target.display_target,
        "project_dir": str(project_dir) if project_dir else None,
        "run_dir": manifest["run_dir"],
        "manifest_file": manifest["manifest_file"],
        "status_file": manifest["status_file"],
        "trace_file": manifest["trace_file"],
        "summary_file": manifest["summary_file"],
        "watch_cursor_file": manifest["watch_cursor_file"],
        "report_file": manifest["report_file"],
        "reviewers_file": manifest["reviewers_file"],
        "candidates_file": manifest["candidates_file"],
        "verified_findings_file": manifest["verified_findings_file"],
        "posting_approval_file": manifest["posting_approval_file"],
        "posting_manifest_file": manifest["posting_manifest_file"],
        "posting_results_file": manifest["posting_results_file"],
        "harness_stdout_file": manifest["harness_stdout_file"],
        "harness_stderr_file": manifest["harness_stderr_file"],
        "state": "launched",
        "done": False,
        "launched_at": _utc_now(),
    }
    return launch_payload


def run_ccr(args: argparse.Namespace) -> dict[str, Any]:
    detection_cwd = Path(args.project_dir).expanduser().resolve() if args.project_dir else None
    target = detect_review_target(args.target, artifact_file=args.artifact_file, cwd=detection_cwd)
    project_dir = _resolve_project_dir(target, args.project_dir)

    manifest = _prepare_run_manifest(
        Path(args.base_dir).expanduser().resolve(),
        args.run_id,
        args.manifest_output,
    )
    observer = RunObserver(manifest)
    observer.set_process_info(pid=os.getpid(), detached=bool(args.detached_child))
    observer.event(
        "run_initialized",
        "Initialized isolated CCR run workspace",
        stage="bootstrap",
        run_dir=manifest["run_dir"],
        status_file=manifest["status_file"],
        trace_file=manifest["trace_file"],
        summary_file=manifest["summary_file"],
    )
    observer.set_target(
        mode=target.mode,
        target=target.display_target,
        project_dir=str(project_dir) if project_dir else None,
    )

    current_stage: str | None = None
    try:
        mr_metadata = _load_json_file(Path(manifest["mr_metadata_file"]), default={})

        current_stage = "artifact_preparation"
        observer.start_stage(
            current_stage,
            "Preparing review artifact",
            mode=target.mode,
            target=target.display_target,
        )
        if target.mode == "mr":
            observer.event(
                "mr_fetch_started",
                "Fetching MR metadata and diff from GitLab",
                stage=current_stage,
                target=target.display_target,
            )
        elif target.mode == "local":
            observer.event(
                "local_artifact_generation_started",
                "Generating local review artifact",
                stage=current_stage,
                target=target.display_target,
            )
        elif target.mode == "artifact":
            observer.event(
                "artifact_replay_started",
                "Replaying existing review artifact",
                stage=current_stage,
                target=target.display_target,
            )
        mr_metadata = _materialize_review_artifact(
            manifest,
            target,
            project_dir=project_dir,
            mr_metadata=mr_metadata,
        )
        artifact_text = _read_text(Path(manifest["diff_file"]))
        changed_files = _extract_changed_files(artifact_text)
        changed_lines = _count_changed_lines(artifact_text)
        if target.mode != "mr" and not Path(manifest["mr_metadata_file"]).is_file():
            _write_json(Path(manifest["mr_metadata_file"]), mr_metadata or {})
        observer.complete_stage(
            current_stage,
            "Review artifact ready",
            changed_file_count=len(changed_files),
            changed_lines=changed_lines,
            diff_file=manifest["diff_file"],
        )
        current_stage = None

        current_stage = "requirements"
        observer.start_stage(current_stage, "Persisting requirements/spec input")
        requirements_text = _materialize_requirements(
            manifest,
            requirements_text=args.requirements_text,
            requirements_file=args.requirements_file,
            requirements_stdin=args.requirements_stdin,
            use_mr_description=args.use_mr_description_as_requirements,
            mr_metadata=mr_metadata,
        )
        requirements_source = "none"
        if args.requirements_text:
            requirements_source = "inline"
        elif args.requirements_file:
            requirements_source = "file"
        elif args.requirements_stdin:
            requirements_source = "stdin"
        elif args.use_mr_description_as_requirements:
            requirements_source = "mr_description"
        observer.complete_stage(
            current_stage,
            "Requirements input ready",
            source=requirements_source,
            has_requirements=bool(requirements_text.strip()),
            requirements_chars=len(requirements_text),
        )
        current_stage = None

        current_stage = "routing"
        observer.start_stage(current_stage, "Building deterministic route input and plan")
        route_input_payload = build_route_input(
            artifact_text,
            requirements_text=requirements_text,
            requirements_from_mr_description=args.use_mr_description_as_requirements,
            user_requested_exhaustive=args.user_requested_exhaustive,
            behavior_change_ambiguous=args.behavior_change_ambiguous,
        )
        route_plan = _plan_route(manifest, route_input_payload)
        observer.set_route_plan(route_input_payload, route_plan)
        observer.complete_stage(
            current_stage,
            "Adaptive route plan ready",
            summary=route_plan.get("summary"),
            planned=route_plan.get("total_passes"),
            full_matrix=route_plan.get("full_matrix"),
        )
        current_stage = None

        current_stage = "review_context"
        observer.start_stage(current_stage, "Building repository/package context")
        _build_review_context_artifact(manifest, project_dir, artifact_text)
        review_context_text = _read_text(Path(manifest["review_context_file"]))
        observer.complete_stage(
            current_stage,
            "Review context ready",
            context_status=(
                "unavailable"
                if "Repository/package context unavailable" in review_context_text
                else "available"
            ),
            review_context_file=manifest["review_context_file"],
        )
        current_stage = None

        current_stage = "static_analysis"
        observer.start_stage(current_stage, "Running static analysis")
        static_analysis_payload = _write_static_analysis_artifact(
            manifest,
            project_dir,
            route_input_payload.get("changed_files") or [],
            dry_run=args.dry_run,
        )
        static_analysis_counts = {
            "go_vet": len(static_analysis_payload.get("go_vet", [])) if isinstance(static_analysis_payload.get("go_vet"), list) else 0,
            "staticcheck": len(static_analysis_payload.get("staticcheck", [])) if isinstance(static_analysis_payload.get("staticcheck"), list) else 0,
            "gosec": len(static_analysis_payload.get("gosec", [])) if isinstance(static_analysis_payload.get("gosec"), list) else 0,
        }
        observer.complete_stage(
            current_stage,
            "Static analysis artifact ready",
            static_analysis_file=manifest["static_analysis_file"],
            total_findings=sum(static_analysis_counts.values()),
            **static_analysis_counts,
        )
        current_stage = None

        current_stage = "shuffle_diff"
        observer.start_stage(current_stage, "Preparing shuffled diff for pass diversity")
        _build_shuffled_diff(manifest, artifact_text)
        observer.complete_stage(
            current_stage,
            "Shuffled diff ready",
            shuffled_diff_file=manifest["shuffled_diff_file"],
        )
        current_stage = None

        current_stage = "reviewers"
        observer.start_stage(current_stage, "Running reviewer passes")
        reviewer_results, reviewers_summary = _run_reviewers(
            manifest,
            route_plan,
            observer=observer,
            project_dir=project_dir,
            requirements_available=bool(requirements_text.strip()),
            dry_run=args.dry_run,
            reviewer_timeout_sec=args.reviewer_timeout,
            max_reviewer_workers=args.max_reviewer_workers,
        )
        observer.complete_stage(
            current_stage,
            "Reviewer passes completed",
            completed=reviewers_summary["completed_passes"],
            succeeded=reviewers_summary["succeeded_passes"],
            failed=reviewers_summary["failed_passes"],
            finding_count=reviewers_summary["total_findings"],
            workers=reviewers_summary["worker_count"],
        )
        current_stage = None

        current_stage = "candidates"
        observer.start_stage(current_stage, "Synthesizing candidate findings")
        candidates, candidates_summary = _build_candidates(
            reviewer_results,
            manifest=manifest,
            route_plan=route_plan,
            static_analysis_payload=static_analysis_payload,
        )
        observer.complete_stage(
            current_stage,
            "Candidate findings ready",
            candidate_count=candidates_summary["candidate_count"],
            source_finding_count=candidates_summary["source_finding_count"],
        )
        current_stage = None

        current_stage = "verification"
        observer.start_stage(current_stage, "Running verification batches")
        verified_findings, verification_summary = _run_verification(
            manifest,
            observer=observer,
            candidates=candidates,
            artifact_text=artifact_text,
            project_dir=project_dir,
            requirements_text=requirements_text,
            dry_run=args.dry_run,
            verifier_timeout_sec=args.verifier_timeout,
            max_verifier_workers=args.max_verifier_workers,
        )
        observer.complete_stage(
            current_stage,
            "Verification completed",
            verified_count=verification_summary["verified_count"],
            batch_count=verification_summary["batch_count"],
            succeeded=verification_summary["successful_batches"],
            failed=verification_summary["failed_batches"],
            workers=verification_summary["worker_count"],
        )
        current_stage = None

        current_stage = "report"
        observer.start_stage(current_stage, "Writing final report")
        report_text = _write_report(manifest, verified_findings)
        observer.complete_stage(
            current_stage,
            "Final report ready",
            verified_count=len(verified_findings),
            report_file=manifest["report_file"],
        )
        current_stage = None

        summary = {
            "contract_version": "ccr.run_summary.v1",
            "run_id": manifest["run_id"],
            "mode": target.mode,
            "target": target.display_target,
            "project_dir": str(project_dir) if project_dir else None,
            "run_dir": manifest["run_dir"],
            "manifest_file": manifest["manifest_file"],
            "status_file": manifest["status_file"],
            "trace_file": manifest["trace_file"],
            "summary_file": manifest["summary_file"],
            "watch_cursor_file": manifest["watch_cursor_file"],
            "harness_stdout_file": manifest["harness_stdout_file"],
            "harness_stderr_file": manifest["harness_stderr_file"],
            "pid": os.getpid(),
            "detached": bool(args.detached_child),
            "review_plan_summary": route_plan.get("summary"),
            "report_file": manifest["report_file"],
            "reviewers_file": manifest["reviewers_file"],
            "candidates_file": manifest["candidates_file"],
            "verified_findings_file": manifest["verified_findings_file"],
            "posting_approval_file": manifest["posting_approval_file"],
            "posting_manifest_file": manifest["posting_manifest_file"],
            "posting_results_file": manifest["posting_results_file"],
            "reviewer_worker_count": reviewers_summary["worker_count"],
            "verifier_worker_count": verification_summary["worker_count"],
            "reviewer_timeout_sec": args.reviewer_timeout,
            "verifier_timeout_sec": args.verifier_timeout,
            "duration_ms": observer.current_duration_ms(),
            "verified_finding_count": len(verified_findings),
            "report_preview": report_text.strip().splitlines()[:8],
        }

        observer.complete_run(summary)
        if args.output_file:
            _write_json(Path(args.output_file).expanduser().resolve(), summary)
        return summary
    except Exception as exc:  # noqa: BLE001
        if current_stage is not None:
            observer.fail_stage(current_stage, f"Stage failed: {exc}")
        observer.fail_run(exc)
        raise


def main() -> None:
    parser = _build_arg_parser()
    args = parser.parse_args()
    try:
        if args.detach and not args.detached_child:
            payload = launch_ccr_detached(args, sys.argv[1:])
            print(json.dumps(payload, indent=2))
            return
        summary = run_ccr(args)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, indent=2), file=sys.stderr)
        sys.exit(1)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
