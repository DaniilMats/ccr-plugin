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
    python3 ccr_run.py uncommitted --requirements-text "Expected behavior..." --dry-run
    python3 ccr_run.py package:internal/service --project-dir ~/src/my-repo --requirements-text "Expected behavior..." --dry-run
    python3 ccr_run.py https://gitlab.com/group/project/-/merge_requests/1234 --use-mr-description-as-requirements --detach
    python3 ccr_run.py --artifact-file /tmp/review_artifact.txt --project-dir ~/src/my-repo --requirements-file /tmp/spec.txt --dry-run
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ccr_consolidate import CandidateRecord, build_candidates as consolidate_candidates
from ccr_routing import RoutingInput, build_routing_plan
from ccr_runtime.common import (
    dedupe_preserve_order as _dedupe_preserve_order,
    duration_ms as _duration_ms,
    estimate_parallel_stage_duration as _estimate_parallel_stage_duration,
    format_milliseconds_short as _format_milliseconds_short,
    format_seconds_short as _format_seconds_short,
    load_json_file as _load_json_file,
    ratio,
    read_text as _read_text,
    run_command as _run_command,
    utc_now as _utc_now,
    write_json as _write_json,
    write_text as _write_text,
)
from ccr_runtime.manifest import DEFAULT_BASE_DIR, build_manifest as _build_manifest, build_run_id as _build_run_id
from ccr_runtime.observer import RunObserver
from ccr_runtime.reporting import format_report as _format_report, write_report as _ccr_runtime_write_report
from ccr_runtime.reviewers import PASS_SPECS as _PASS_SPECS, ReviewerPassSpec, run_reviewers as _run_reviewers
from ccr_runtime.telemetry import (
    aggregate_llm_metrics as _aggregate_llm_metrics,
    collect_llm_invocations as _collect_llm_invocations,
    empty_llm_metrics as _empty_llm_metrics,
    invocation_event_fields as _invocation_event_fields,
    llm_metrics_from_summary as _llm_metrics_from_summary,
    llm_summary_fields as _llm_summary_fields,
    merge_llm_metrics as _merge_llm_metrics,
    normalize_llm_invocation as _normalize_llm_invocation,
)
from ccr_runtime.verification import merge_verified_findings as _merge_verified_findings, run_verification as _run_verification


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

_PERSONA_ORDER = ("security", "concurrency", "performance", "requirements")

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


def _write_report(manifest: dict[str, Any], verified_findings: list[dict[str, Any]]) -> str:
    return _ccr_runtime_write_report(Path(manifest["report_file"]), verified_findings)


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


def _validate_requirements_input(target: ReviewTarget, args: argparse.Namespace) -> None:
    selected_sources: list[str] = []
    if args.requirements_text is not None:
        selected_sources.append("--requirements-text")
    if args.requirements_file:
        selected_sources.append("--requirements-file")
    if args.requirements_stdin:
        selected_sources.append("--requirements-stdin")
    if args.use_mr_description_as_requirements:
        selected_sources.append("--use-mr-description-as-requirements")

    if not selected_sources:
        raise ValueError(
            "CCR requires non-empty requirements/spec input before launch. "
            "Pass exactly one of --requirements-text, --requirements-file, --requirements-stdin, "
            "or --use-mr-description-as-requirements (MR targets only)."
        )
    if len(selected_sources) > 1:
        raise ValueError(
            "use exactly one requirements source: --requirements-text, --requirements-file, "
            "--requirements-stdin, or --use-mr-description-as-requirements"
        )
    if args.requirements_text is not None and not str(args.requirements_text).strip():
        raise ValueError("--requirements-text cannot be empty")
    if args.requirements_file:
        req_path = Path(args.requirements_file).expanduser().resolve()
        if not req_path.is_file():
            raise ValueError(f"requirements file does not exist: {req_path}")
        if not _read_text(req_path).strip():
            raise ValueError(f"requirements file is empty: {req_path}")
    if args.use_mr_description_as_requirements and target.mode != "mr":
        raise ValueError("--use-mr-description-as-requirements is only valid for MR targets")



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
        for enabled in (
            requirements_text is not None,
            bool(requirements_file),
            bool(requirements_stdin),
            bool(use_mr_description),
        )
        if enabled
    )
    if chosen_sources != 1:
        raise ValueError(
            "CCR requires exactly one requirements source: --requirements-text, --requirements-file, "
            "--requirements-stdin, or --use-mr-description-as-requirements"
        )

    if requirements_text is not None:
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

    if not text:
        if use_mr_description:
            raise ValueError("MR description is empty; provide explicit requirements/spec text before launching CCR")
        raise ValueError("requirements/spec text is empty; provide non-empty requirements before launching CCR")

    requirements_path = Path(manifest["requirements_file"])
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


def _build_candidates(
    reviewer_results: list[dict[str, Any]],
    *,
    manifest: dict[str, Any],
    route_plan: dict[str, Any],
    static_analysis_payload: dict[str, Any],
) -> tuple[list[CandidateRecord], dict[str, Any]]:
    candidates, candidates_summary = consolidate_candidates(
        reviewer_results,
        route_plan=route_plan,
        static_analysis_payload=static_analysis_payload,
    )
    candidates_payload = {
        "contract_version": "ccr.candidates_manifest.v1",
        "candidates": [candidate.to_contract_dict() for candidate in candidates],
        "summary": candidates_summary,
    }
    _write_json(Path(manifest["candidates_file"]), candidates_payload)
    return candidates, candidates_summary


_ratio = ratio


def _write_run_metrics(
    manifest: dict[str, Any],
    *,
    target: ReviewTarget,
    route_input: dict[str, Any],
    route_plan: dict[str, Any],
    requirements_source: str,
    requirements_text: str,
    reviewers_summary: dict[str, Any],
    candidates_summary: dict[str, Any],
    verification_summary: dict[str, Any],
) -> dict[str, Any]:
    reviewer_llm_metrics = _llm_metrics_from_summary(reviewers_summary)
    verification_llm_metrics = _llm_metrics_from_summary(verification_summary)
    combined_llm_metrics = _merge_llm_metrics(reviewer_llm_metrics, verification_llm_metrics)

    source_finding_count = int(candidates_summary.get("source_finding_count") or 0)
    candidate_count = int(candidates_summary.get("candidate_count") or 0)
    duplicate_merge_count = max(0, source_finding_count - candidate_count)

    payload = {
        "contract_version": "ccr.run_metrics.v1",
        "run_id": manifest["run_id"],
        "generated_at": _utc_now(),
        "mode": target.mode,
        "target": target.display_target,
        "requirements": {
            "source": requirements_source,
            "has_requirements": bool(requirements_text.strip()),
            "requirements_chars": len(requirements_text),
        },
        "route": {
            "summary": route_plan.get("summary"),
            "total_passes": route_plan.get("total_passes"),
            "full_matrix": route_plan.get("full_matrix"),
            "pass_counts": route_plan.get("pass_counts"),
            "triggered_personas": route_input.get("triggered_personas"),
            "highest_risk_personas": route_input.get("highest_risk_personas"),
            "critical_surfaces": route_input.get("critical_surfaces"),
            "changed_file_count": route_input.get("changed_file_count"),
            "changed_lines": route_input.get("changed_lines"),
        },
        "reviewers": {
            **reviewers_summary,
            "availability_rate": _ratio(
                int(reviewers_summary.get("succeeded_passes") or 0),
                int(reviewers_summary.get("planned_passes") or 0),
            ),
        },
        "candidates": {
            **candidates_summary,
            "duplicate_merge_count": duplicate_merge_count,
            "duplicate_merge_rate": _ratio(duplicate_merge_count, source_finding_count),
        },
        "verification": dict(verification_summary),
        "llm": {
            "total_calls": int(combined_llm_metrics.get("call_count") or 0),
            "reviewer_calls": int(reviewer_llm_metrics.get("call_count") or 0),
            "verifier_calls": int(verification_llm_metrics.get("call_count") or 0),
            "total_tokens": int(combined_llm_metrics.get("total_tokens") or 0),
            "total_duration_ms": int(combined_llm_metrics.get("total_duration_ms") or 0),
            "schema_retry_count": int(combined_llm_metrics.get("schema_retry_count") or 0),
            "schema_retry_rate": combined_llm_metrics.get("schema_retry_rate"),
            "schema_violation_count": int(combined_llm_metrics.get("schema_violation_count") or 0),
            "timed_out_calls": int(combined_llm_metrics.get("timed_out_calls") or 0),
            "failed_calls": int(combined_llm_metrics.get("failed_calls") or 0),
            "provider_breakdown": dict(combined_llm_metrics.get("provider_breakdown") or {}),
        },
        "posting": {
            "posting_supported": target.mode == "mr",
            "posting_approval_file": manifest["posting_approval_file"],
            "posting_manifest_file": manifest["posting_manifest_file"],
            "posting_results_file": manifest["posting_results_file"],
        },
    }
    _write_json(Path(manifest["run_metrics_file"]), payload)
    return payload



def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ccr-run",
        description="Deterministic CCR review harness.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s uncommitted --requirements-text \"Expected behavior...\" --dry-run\n"
            "  %(prog)s package:internal/auth --project-dir tests/fixtures/go_repo --requirements-text \"Expected behavior...\" --dry-run\n"
            "  %(prog)s https://gitlab.com/group/project/-/merge_requests/1234 --use-mr-description-as-requirements\n"
            "  %(prog)s --artifact-file /tmp/review_artifact.txt --project-dir ~/src/repo --requirements-file /tmp/spec.txt --dry-run\n"
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
        help="Inline non-empty requirements/spec text to inject into reviewer prompts. CCR refuses to launch without requirements.",
    )
    parser.add_argument(
        "--requirements-file",
        default=None,
        help="Path to non-empty requirements/spec text to inject into reviewer prompts.",
    )
    parser.add_argument(
        "--requirements-stdin",
        action="store_true",
        help="Read non-empty requirements/spec text from stdin and persist it into the run workspace.",
    )
    parser.add_argument(
        "--use-mr-description-as-requirements",
        action="store_true",
        help="For MR mode, use the MR description as requirements/spec text. The MR description must be non-empty.",
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
    _validate_requirements_input(target, args)
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
        if not requirements_text.strip():
            raise ValueError("requirements/spec text from stdin is empty; provide non-empty requirements before launching CCR")
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
        "state": "launched",
        "done": False,
        "launched_at": _utc_now(),
    }
    return launch_payload


def run_ccr(args: argparse.Namespace) -> dict[str, Any]:
    detection_cwd = Path(args.project_dir).expanduser().resolve() if args.project_dir else None
    target = detect_review_target(args.target, artifact_file=args.artifact_file, cwd=detection_cwd)
    _validate_requirements_input(target, args)
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
        if args.requirements_text is not None:
            requirements_source = "inline"
        elif args.requirements_file:
            requirements_source = "file"
        elif args.requirements_stdin:
            requirements_source = "stdin"
        else:
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
            llm_call_count=reviewers_summary.get("llm_call_count"),
            schema_retry_count=reviewers_summary.get("schema_retry_count"),
            total_tokens=reviewers_summary.get("total_tokens"),
            provider_breakdown=reviewers_summary.get("provider_breakdown"),
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
            duplicate_merge_count=max(0, int(candidates_summary.get("source_finding_count") or 0) - int(candidates_summary.get("candidate_count") or 0)),
            duplicate_merge_rate=_ratio(
                max(0, int(candidates_summary.get("source_finding_count") or 0) - int(candidates_summary.get("candidate_count") or 0)),
                int(candidates_summary.get("source_finding_count") or 0),
            ),
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
            ready_count=verification_summary.get("ready_count"),
            dropped_count=verification_summary.get("dropped_count"),
            confirmed_count=verification_summary.get("confirmed_count"),
            uncertain_count=verification_summary.get("uncertain_count"),
            rejected_count=verification_summary.get("rejected_count"),
            anchor_failure_count=verification_summary.get("anchor_failure_count"),
            llm_call_count=verification_summary.get("llm_call_count"),
            schema_retry_count=verification_summary.get("schema_retry_count"),
            total_tokens=verification_summary.get("total_tokens"),
            provider_breakdown=verification_summary.get("provider_breakdown"),
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

        _write_run_metrics(
            manifest,
            target=target,
            route_input=route_input_payload,
            route_plan=route_plan,
            requirements_source=requirements_source,
            requirements_text=requirements_text,
            reviewers_summary=reviewers_summary,
            candidates_summary=candidates_summary,
            verification_summary=verification_summary,
        )

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
            "run_metrics_file": manifest["run_metrics_file"],
            "watch_cursor_file": manifest["watch_cursor_file"],
            "harness_stdout_file": manifest["harness_stdout_file"],
            "harness_stderr_file": manifest["harness_stderr_file"],
            "pid": os.getpid(),
            "detached": bool(args.detached_child),
            "review_plan_summary": route_plan.get("summary"),
            "report_file": manifest["report_file"],
            "reviewers_file": manifest["reviewers_file"],
            "candidates_file": manifest["candidates_file"],
            "verification_prepare_file": manifest["verification_prepare_file"],
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
