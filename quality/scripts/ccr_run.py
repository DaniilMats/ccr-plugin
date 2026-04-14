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
    python3 ccr_run.py https://gitlab.com/group/project/-/merge_requests/1234
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

    cwd = project_dir if project_dir and project_dir.is_dir() else Path.cwd()
    try:
        result = _run_command(cmd, cwd=cwd, timeout=timeout_sec + 30)
        stderr = result.stderr
        exit_code = result.returncode
    except subprocess.TimeoutExpired:
        stderr = f"timed out after {timeout_sec + 30} seconds\n"
        exit_code = -1

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
    project_dir: Path | None,
    requirements_available: bool,
    dry_run: bool,
    reviewer_timeout_sec: int,
) -> list[dict[str, Any]]:
    passes = route_plan.get("passes") or []
    specs = [_PASS_SPECS[pass_name] for pass_name in passes]
    max_workers = max(1, min(len(specs), 8))

    results: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_map = {
            pool.submit(
                _run_reviewer_pass,
                spec,
                manifest=manifest,
                project_dir=project_dir,
                requirements_available=requirements_available,
                dry_run=dry_run,
                timeout_sec=reviewer_timeout_sec,
            ): spec.pass_name
            for spec in specs
        }
        for future in concurrent.futures.as_completed(future_map):
            results.append(future.result())

    results.sort(key=lambda item: passes.index(item["pass_name"]))
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
        "summary": {
            "planned_passes": len(passes),
            "succeeded_passes": sum(1 for item in results if item["status"] == "succeeded"),
            "failed_passes": sum(1 for item in results if item["status"] != "succeeded"),
            "total_findings": sum(item["finding_count"] for item in results),
        },
    }
    _write_json(Path(manifest["reviewers_file"]), reviewers_payload)
    return results


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
) -> list[CandidateRecord]:
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

    candidates_payload = {
        "contract_version": "ccr.candidates_manifest.v1",
        "candidates": [candidate.to_contract_dict() for candidate in candidates],
        "summary": {
            "candidate_count": len(candidates),
            "source_finding_count": len(flattened),
        },
    }
    _write_json(Path(manifest["candidates_file"]), candidates_payload)
    return candidates


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
    last_exit_code = 1
    last_stderr = ""

    cwd = project_dir if project_dir and project_dir.is_dir() else Path.cwd()

    for provider in providers:
        used_provider = provider
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
        if output_path.is_file() and last_exit_code == 0:
            break

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
        "exit_code": last_exit_code,
        "status": "succeeded" if last_exit_code == 0 else "failed",
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
    candidates: list[CandidateRecord],
    artifact_text: str,
    project_dir: Path | None,
    requirements_text: str,
    dry_run: bool,
    verifier_timeout_sec: int,
) -> list[dict[str, Any]]:
    if not candidates:
        payload = {
            "contract_version": "ccr.verified_findings.v1",
            "verified_findings": [],
            "verification_batches": [],
            "summary": {
                "verified_count": 0,
                "batch_count": 0,
                "successful_batches": 0,
            },
        }
        _write_json(Path(manifest["verified_findings_file"]), payload)
        return []

    batches = _write_verification_batches(
        manifest,
        candidates=candidates,
        artifact_text=artifact_text,
        project_dir=project_dir,
        requirements_text=requirements_text,
    )

    max_workers = max(1, min(len(batches), 4))
    results: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_map = {
            pool.submit(
                _run_single_verification_batch,
                batch,
                manifest=manifest,
                project_dir=project_dir,
                dry_run=dry_run,
                verifier_timeout_sec=verifier_timeout_sec,
            ): batch["batch_id"]
            for batch in batches
        }
        for future in concurrent.futures.as_completed(future_map):
            results.append(future.result())

    results.sort(key=lambda item: item["batch_id"])
    return _merge_verified_findings(manifest, candidates=candidates, verification_results=results)


def _format_report(verified_findings: list[dict[str, Any]]) -> str:
    if not verified_findings:
        return "Проверенных замечаний не найдено.\n"

    lines: list[str] = []
    finding_number = 1
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
            lines.append(
                f"{finding_number}. [{item['severity'].upper()}] {item['file']}:{item['line']} — {confidence} — {item['message']}"
            )
            if item.get("evidence"):
                lines.append(f"   Evidence: {item['evidence']}")
            finding_number += 1
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
        "--verifier-timeout",
        type=int,
        default=300,
        help="Per-verifier timeout in seconds (default: 300).",
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
    return parser


def run_ccr(args: argparse.Namespace) -> dict[str, Any]:
    detection_cwd = Path(args.project_dir).expanduser().resolve() if args.project_dir else None
    target = detect_review_target(args.target, artifact_file=args.artifact_file, cwd=detection_cwd)
    project_dir = _resolve_project_dir(target, args.project_dir)

    manifest = _prepare_run_manifest(
        Path(args.base_dir).expanduser().resolve(),
        args.run_id,
        args.manifest_output,
    )

    mr_metadata = _load_json_file(Path(manifest["mr_metadata_file"]), default={})
    mr_metadata = _materialize_review_artifact(
        manifest,
        target,
        project_dir=project_dir,
        mr_metadata=mr_metadata,
    )

    requirements_text = _materialize_requirements(
        manifest,
        requirements_text=args.requirements_text,
        requirements_file=args.requirements_file,
        requirements_stdin=args.requirements_stdin,
        use_mr_description=args.use_mr_description_as_requirements,
        mr_metadata=mr_metadata,
    )

    artifact_text = _read_text(Path(manifest["diff_file"]))
    if target.mode != "mr" and not Path(manifest["mr_metadata_file"]).is_file():
        _write_json(Path(manifest["mr_metadata_file"]), mr_metadata or {})

    route_input_payload = build_route_input(
        artifact_text,
        requirements_text=requirements_text,
        requirements_from_mr_description=args.use_mr_description_as_requirements,
        user_requested_exhaustive=args.user_requested_exhaustive,
        behavior_change_ambiguous=args.behavior_change_ambiguous,
    )
    route_plan = _plan_route(manifest, route_input_payload)

    _build_review_context_artifact(manifest, project_dir, artifact_text)
    static_analysis_payload = _write_static_analysis_artifact(
        manifest,
        project_dir,
        route_input_payload.get("changed_files") or [],
        dry_run=args.dry_run,
    )
    _build_shuffled_diff(manifest, artifact_text)

    reviewer_results = _run_reviewers(
        manifest,
        route_plan,
        project_dir=project_dir,
        requirements_available=bool(requirements_text.strip()),
        dry_run=args.dry_run,
        reviewer_timeout_sec=args.reviewer_timeout,
    )
    candidates = _build_candidates(
        reviewer_results,
        manifest=manifest,
        route_plan=route_plan,
        static_analysis_payload=static_analysis_payload,
    )
    verified_findings = _run_verification(
        manifest,
        candidates=candidates,
        artifact_text=artifact_text,
        project_dir=project_dir,
        requirements_text=requirements_text,
        dry_run=args.dry_run,
        verifier_timeout_sec=args.verifier_timeout,
    )
    report_text = _write_report(manifest, verified_findings)

    summary = {
        "run_id": manifest["run_id"],
        "mode": target.mode,
        "target": target.display_target,
        "project_dir": str(project_dir) if project_dir else None,
        "review_plan_summary": route_plan.get("summary"),
        "report_file": manifest["report_file"],
        "manifest_file": manifest["manifest_file"],
        "reviewers_file": manifest["reviewers_file"],
        "candidates_file": manifest["candidates_file"],
        "verified_findings_file": manifest["verified_findings_file"],
        "verified_finding_count": len(verified_findings),
        "report_preview": report_text.strip().splitlines()[:8],
    }

    if args.output_file:
        _write_json(Path(args.output_file).expanduser().resolve(), summary)
    return summary


def main() -> None:
    parser = _build_arg_parser()
    args = parser.parse_args()
    try:
        summary = run_ccr(args)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, indent=2), file=sys.stderr)
        sys.exit(1)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
