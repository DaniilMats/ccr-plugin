#!/usr/bin/env python3
"""Deterministic MR comment posting helper for CCR.

This helper turns explicit user approval into a run-scoped posting plan and,
when requested, applies that plan to GitLab merge request discussions with
idempotency checks and structured result artifacts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ccr_runtime.common import load_json_file, ratio, read_text, run_command, utc_now, write_json
from ccr_runtime.finding_format import render_comment_body, structured_finding_fields


_POSTING_APPROVAL_CONTRACT = "ccr.posting_approval.v1"
_POSTING_MANIFEST_CONTRACT = "ccr.posting_manifest.v1"
_POSTING_RESULT_CONTRACT = "ccr.posting_result.v1"
_MR_URL_RE = re.compile(r"^https?://[^/]+/(?P<project>.+)/-/merge_requests/(?P<iid>\d+)(?:[/?#].*)?$")
_DIFF_HEADER_RE = re.compile(r"^diff --git a/(?P<old>.+) b/(?P<new>.+)$")
_HUNK_RE = re.compile(r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? \+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@")
_FINGERPRINT_RE = re.compile(r"<!--\s*ccr:fingerprint=(?P<fingerprint>[0-9a-f]{8,64})\b[^>]*-->", re.IGNORECASE)

# Backward-compatible local aliases while shared runtime helpers are adopted.
_utc_now = utc_now
_read_text = read_text
_load_json_file = load_json_file
_write_json = write_json
_ratio = ratio


@dataclass(frozen=True)
class DiffLineRef:
    old_path: str
    new_path: str
    old_line: int | None
    new_line: int | None
    line_kind: str


def _ensure_dict(payload: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object")
    return payload


def _parse_mr_target(target: str) -> tuple[str, int] | None:
    match = _MR_URL_RE.match(target.strip())
    if not match:
        return None
    return match.group("project"), int(match.group("iid"))


def _dedupe_ints(values: list[Any]) -> list[int]:
    result: list[int] = []
    seen: set[int] = set()
    for value in values:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number < 1 or number in seen:
            continue
        seen.add(number)
        result.append(number)
    return result


def _normalize_message(message: str) -> str:
    collapsed = re.sub(r"\s+", " ", message.strip())
    return collapsed.lower()


def _build_fingerprint(project: str, mr_iid: int | str, finding: dict[str, Any]) -> str:
    payload = {
        "project": project,
        "mr_iid": str(mr_iid),
        "file": str(finding.get("file") or ""),
        "line": int(finding.get("line") or 0),
        "message": _normalize_message(str(finding.get("message") or "")),
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    return digest[:16]


def _build_comment_body(finding: dict[str, Any], *, fingerprint: str, run_id: str) -> str:
    candidate_id = str(finding.get("candidate_id") or "unknown")
    finding_number = int(finding.get("finding_number") or 0)
    metadata = f"<!-- ccr:fingerprint={fingerprint} run_id={run_id} finding={finding_number} candidate_id={candidate_id} -->"
    body = render_comment_body(finding).strip()
    if body:
        return f"{body}\n\n{metadata}"
    return metadata


def _slugify(text: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")
    return slug or "finding"


def _request_payload_path(comments_dir: Path, finding: dict[str, Any]) -> Path:
    finding_number = int(finding.get("finding_number") or 0)
    candidate_id = _slugify(str(finding.get("candidate_id") or f"finding-{finding_number}"))
    return comments_dir / f"{finding_number:03d}-{candidate_id}.request.json"


def _response_payload_path(comments_dir: Path, finding: dict[str, Any]) -> Path:
    finding_number = int(finding.get("finding_number") or 0)
    candidate_id = _slugify(str(finding.get("candidate_id") or f"finding-{finding_number}"))
    return comments_dir / f"{finding_number:03d}-{candidate_id}.response.json"


def _cleanup_comments_dir(comments_dir: Path) -> None:
    comments_dir.mkdir(parents=True, exist_ok=True)
    for pattern in ("*.request.json", "*.response.json"):
        for path in comments_dir.glob(pattern):
            path.unlink(missing_ok=True)


def _index_verified_findings(verified_payload: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]]]:
    raw_findings = verified_payload.get("verified_findings") if isinstance(verified_payload.get("verified_findings"), list) else []
    normalized: list[dict[str, Any]] = []
    indexed: dict[int, dict[str, Any]] = {}
    next_number = 1
    for raw_finding in raw_findings:
        if not isinstance(raw_finding, dict):
            continue
        finding = dict(raw_finding)
        finding_number = int(finding.get("finding_number") or next_number)
        if finding_number < 1:
            finding_number = next_number
        finding["finding_number"] = finding_number
        next_number = max(next_number, finding_number + 1)
        normalized.append(finding)
        indexed[finding_number] = finding
    return normalized, indexed


def _build_diff_index(diff_text: str) -> dict[str, dict[int, DiffLineRef]]:
    index: dict[str, dict[int, DiffLineRef]] = {}
    old_path: str | None = None
    new_path: str | None = None
    old_line: int | None = None
    new_line: int | None = None

    for raw_line in diff_text.splitlines():
        header_match = _DIFF_HEADER_RE.match(raw_line)
        if header_match:
            old_path = header_match.group("old")
            new_path = header_match.group("new")
            old_line = None
            new_line = None
            continue

        hunk_match = _HUNK_RE.match(raw_line)
        if hunk_match:
            old_line = int(hunk_match.group("old_start"))
            new_line = int(hunk_match.group("new_start"))
            continue

        if old_path is None or new_path is None or old_line is None or new_line is None:
            continue

        if raw_line.startswith("+") and not raw_line.startswith("+++"):
            index.setdefault(new_path, {})[new_line] = DiffLineRef(
                old_path=old_path,
                new_path=new_path,
                old_line=None,
                new_line=new_line,
                line_kind="new",
            )
            new_line += 1
            continue

        if raw_line.startswith("-") and not raw_line.startswith("---"):
            index.setdefault(old_path, {})[old_line] = DiffLineRef(
                old_path=old_path,
                new_path=new_path,
                old_line=old_line,
                new_line=None,
                line_kind="old",
            )
            old_line += 1
            continue

        if raw_line.startswith(" "):
            ref = DiffLineRef(
                old_path=old_path,
                new_path=new_path,
                old_line=old_line,
                new_line=new_line,
                line_kind="context",
            )
            index.setdefault(new_path, {})[new_line] = ref
            old_line += 1
            new_line += 1
            continue

        if raw_line.startswith("\\"):
            continue

    return index


def _build_anchor(diff_index: dict[str, dict[int, DiffLineRef]], file_path: str, line: int, diff_refs: dict[str, str]) -> dict[str, Any] | None:
    ref = diff_index.get(file_path, {}).get(line)
    if ref is None:
        return None
    anchor: dict[str, Any] = {
        "position_type": "text",
        "base_sha": diff_refs["base_sha"],
        "start_sha": diff_refs["start_sha"],
        "head_sha": diff_refs["head_sha"],
        "old_path": ref.old_path,
        "new_path": ref.new_path,
        "line_kind": ref.line_kind,
    }
    if ref.old_line is not None:
        anchor["old_line"] = ref.old_line
    if ref.new_line is not None:
        anchor["new_line"] = ref.new_line
    return anchor


def _build_position_payload(anchor: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "position_type": "text",
        "base_sha": anchor["base_sha"],
        "start_sha": anchor["start_sha"],
        "head_sha": anchor["head_sha"],
        "old_path": anchor["old_path"],
        "new_path": anchor["new_path"],
    }
    if anchor.get("old_line") is not None:
        payload["old_line"] = int(anchor["old_line"])
    if anchor.get("new_line") is not None:
        payload["new_line"] = int(anchor["new_line"])
    return payload


def _normalize_approval_payload(
    approval: dict[str, Any],
    *,
    manifest_run_id: str,
    target_project: str,
    target_mr_iid: int,
) -> dict[str, Any]:
    if approval.get("contract_version") != _POSTING_APPROVAL_CONTRACT:
        raise ValueError(f"unsupported approval contract: {approval.get('contract_version')!r}")
    if str(approval.get("run_id") or "") != str(manifest_run_id):
        raise ValueError("approval run_id does not match run manifest")

    explicit_project = str(approval.get("project") or "").strip()
    explicit_mr_iid_raw = approval.get("mr_iid")
    if explicit_project and explicit_project != target_project:
        raise ValueError("approval target does not match run summary MR target")
    if explicit_mr_iid_raw not in (None, ""):
        explicit_mr_iid = int(explicit_mr_iid_raw)
        if explicit_mr_iid != target_mr_iid:
            raise ValueError("approval target does not match run summary MR target")

    return {
        "contract_version": _POSTING_APPROVAL_CONTRACT,
        "run_id": manifest_run_id,
        "project": explicit_project or target_project,
        "mr_iid": int(explicit_mr_iid_raw) if explicit_mr_iid_raw not in (None, "") else target_mr_iid,
        "approved_finding_numbers": _dedupe_ints(list(approval.get("approved_finding_numbers") or [])),
        "approved_all": bool(approval.get("approved_all", False)),
        "approved_at": str(approval.get("approved_at") or _utc_now()),
        "source": str(approval.get("source") or "user_selection"),
    }



def _load_context(manifest_file: Path, approval_file_override: Path | None) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str, int, dict[str, str], list[dict[str, Any]]]:
    manifest = _ensure_dict(_load_json_file(manifest_file), label="run manifest")
    summary = _ensure_dict(_load_json_file(Path(manifest["summary_file"]), default={}), label="run summary")
    if summary.get("mode") != "mr":
        raise ValueError("posting is only supported for MR runs")

    target = str(summary.get("target") or "")
    parsed_target = _parse_mr_target(target)
    if parsed_target is None:
        raise ValueError(f"could not parse MR target from run summary: {target}")
    target_project, target_mr_iid = parsed_target

    approval_path = approval_file_override or Path(manifest["posting_approval_file"])
    approval = _ensure_dict(_load_json_file(approval_path), label="posting approval")
    normalized_approval = _normalize_approval_payload(
        approval,
        manifest_run_id=str(manifest.get("run_id") or ""),
        target_project=target_project,
        target_mr_iid=target_mr_iid,
    )
    if approval != normalized_approval:
        _write_json(approval_path, normalized_approval)
    approval = normalized_approval

    mr_metadata = _ensure_dict(_load_json_file(Path(manifest["mr_metadata_file"]), default={}), label="mr metadata")
    diff_refs_raw = mr_metadata.get("diff_refs") if isinstance(mr_metadata.get("diff_refs"), dict) else {}
    diff_refs = {
        "base_sha": str(diff_refs_raw.get("base_sha") or ""),
        "start_sha": str(diff_refs_raw.get("start_sha") or ""),
        "head_sha": str(diff_refs_raw.get("head_sha") or ""),
    }
    if any(not value for value in diff_refs.values()):
        raise ValueError("mr_metadata.json is missing diff_refs.base_sha/start_sha/head_sha")

    verified_payload = _ensure_dict(_load_json_file(Path(manifest["verified_findings_file"]), default={}), label="verified findings")
    verified_findings, _ = _index_verified_findings(verified_payload)
    return manifest, summary, approval, str(approval["project"]), int(approval["mr_iid"]), diff_refs, verified_findings


_POSTING_RESULT_STATUSES = (
    "posted",
    "already_posted",
    "skipped_missing_anchor",
    "skipped_invalid_selection",
    "failed",
    "invalid_response",
)



def _normalize_metric_label(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    return text or None



def _normalize_line(value: Any) -> int | None:
    try:
        line = int(value)
    except (TypeError, ValueError):
        return None
    return line if line >= 1 else None



def _finding_metric_context(finding: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(finding, dict):
        return {
            "candidate_id": None,
            "persona": None,
            "severity": None,
            "file": None,
            "line": None,
            "message": None,
            "prepared_status": None,
            "fingerprint": None,
            "payload_file": None,
        }
    return {
        "candidate_id": str(finding.get("candidate_id") or "") or None,
        "persona": _normalize_metric_label(finding.get("persona")),
        "severity": _normalize_metric_label(finding.get("severity")),
        "file": str(finding.get("file") or "") or None,
        "line": _normalize_line(finding.get("line")),
        "message": str(finding.get("message") or "") or None,
        "prepared_status": str(finding.get("status") or "") or None,
        "fingerprint": str(finding.get("fingerprint") or "") or None,
        "payload_file": str(finding.get("payload_file") or "") or None,
    }



def _empty_breakdown_bucket() -> dict[str, int]:
    return {
        "approved_count": 0,
        "ready_count": 0,
        "missing_anchor_count": 0,
        "posted_count": 0,
        "already_posted_count": 0,
        "skipped_count": 0,
        "skipped_missing_anchor_count": 0,
        "skipped_invalid_selection_count": 0,
        "failed_count": 0,
        "invalid_response_count": 0,
    }



def _record_prepared_breakdown(breakdown: dict[str, dict[str, int]], label: str | None, finding: dict[str, Any]) -> None:
    if not label:
        return
    bucket = breakdown.setdefault(label, _empty_breakdown_bucket())
    bucket["approved_count"] += 1
    status = str(finding.get("status") or "")
    if status == "ready":
        bucket["ready_count"] += 1
    elif status == "missing_anchor":
        bucket["missing_anchor_count"] += 1



def _record_result_breakdown(breakdown: dict[str, dict[str, int]], label: str | None, result: dict[str, Any]) -> None:
    if not label:
        return
    bucket = breakdown.setdefault(label, _empty_breakdown_bucket())
    status = str(result.get("status") or "")
    if status == "posted":
        bucket["posted_count"] += 1
    elif status == "already_posted":
        bucket["already_posted_count"] += 1
    elif status == "skipped_missing_anchor":
        bucket["skipped_count"] += 1
        bucket["skipped_missing_anchor_count"] += 1
    elif status == "skipped_invalid_selection":
        bucket["skipped_count"] += 1
        bucket["skipped_invalid_selection_count"] += 1
    elif status == "invalid_response":
        bucket["failed_count"] += 1
        bucket["invalid_response_count"] += 1
    elif status == "failed":
        bucket["failed_count"] += 1



def _build_dimension_breakdown(approved_findings: list[dict[str, Any]], results: list[dict[str, Any]], *, field: str) -> dict[str, dict[str, int]]:
    breakdown: dict[str, dict[str, int]] = {}
    findings_by_number = {
        int(item.get("finding_number") or 0): item
        for item in approved_findings
        if isinstance(item, dict) and int(item.get("finding_number") or 0) > 0
    }
    for finding in approved_findings:
        if not isinstance(finding, dict):
            continue
        _record_prepared_breakdown(breakdown, _normalize_metric_label(finding.get(field)), finding)
    for result in results:
        if not isinstance(result, dict):
            continue
        label = _normalize_metric_label(result.get(field))
        if label is None:
            finding = findings_by_number.get(int(result.get("finding_number") or 0))
            if finding is not None:
                label = _normalize_metric_label(finding.get(field))
        _record_result_breakdown(breakdown, label, result)
    return {key: breakdown[key] for key in sorted(breakdown)}



def _build_prepare_summary(approved_findings: list[dict[str, Any]], invalid_numbers: list[int]) -> dict[str, Any]:
    ready_count = sum(1 for item in approved_findings if str(item.get("status") or "") == "ready")
    missing_anchor_count = sum(1 for item in approved_findings if str(item.get("status") or "") == "missing_anchor")
    return {
        "approved_count": len(approved_findings),
        "ready_count": ready_count,
        "missing_anchor_count": missing_anchor_count,
        "invalid_count": len(invalid_numbers),
        "status_counts": {
            "ready": ready_count,
            "missing_anchor": missing_anchor_count,
        },
        "persona_breakdown": _build_dimension_breakdown(approved_findings, [], field="persona"),
        "severity_breakdown": _build_dimension_breakdown(approved_findings, [], field="severity"),
    }



def _build_result_summary(
    prepared: dict[str, Any],
    results: list[dict[str, Any]],
    *,
    posted_count: int,
    already_posted_count: int,
    skipped_count: int,
    failed_count: int,
) -> dict[str, Any]:
    approved_findings = [item for item in (prepared.get("approved_findings") or []) if isinstance(item, dict)]
    invalid_numbers = _dedupe_ints(list(prepared.get("invalid_finding_numbers") or []))
    prepare_summary = _build_prepare_summary(approved_findings, invalid_numbers)
    status_counts = {status: 0 for status in _POSTING_RESULT_STATUSES}
    total_attempts = 0
    for result in results:
        if not isinstance(result, dict):
            continue
        status = str(result.get("status") or "")
        if status in status_counts:
            status_counts[status] += 1
        total_attempts += int(result.get("attempts") or 0)
    ready_count = int(prepare_summary["ready_count"])
    ready_resolved_count = posted_count + already_posted_count
    return {
        "approved_all": bool(prepared.get("approved_all")),
        "approved_count": int(prepare_summary["approved_count"]),
        "ready_count": ready_count,
        "missing_anchor_count": int(prepare_summary["missing_anchor_count"]),
        "invalid_count": int(prepare_summary["invalid_count"]),
        "ready_resolved_count": ready_resolved_count,
        "ready_resolution_rate": _ratio(ready_resolved_count, ready_count),
        "posted_count": posted_count,
        "already_posted_count": already_posted_count,
        "skipped_count": skipped_count,
        "failed_count": failed_count,
        "total_attempts": total_attempts,
        "status_counts": status_counts,
        "persona_breakdown": _build_dimension_breakdown(approved_findings, results, field="persona"),
        "severity_breakdown": _build_dimension_breakdown(approved_findings, results, field="severity"),
    }



def prepare_posting_manifest(manifest_file: Path, *, approval_file: Path | None = None) -> dict[str, Any]:
    manifest, _summary, approval, project, mr_iid, diff_refs, verified_findings = _load_context(manifest_file, approval_file)
    verified_by_number = {int(finding["finding_number"]): finding for finding in verified_findings}

    approved_all = bool(approval.get("approved_all"))
    requested_numbers = _dedupe_ints(
        [finding.get("finding_number") for finding in verified_findings]
        if approved_all
        else list(approval.get("approved_finding_numbers") or [])
    )
    invalid_numbers = [number for number in requested_numbers if number not in verified_by_number]

    diff_text = _read_text(Path(manifest["diff_file"]))
    diff_index = _build_diff_index(diff_text)
    comments_dir = Path(manifest["comments_dir"])
    _cleanup_comments_dir(comments_dir)

    approved_findings: list[dict[str, Any]] = []
    for number in requested_numbers:
        finding = verified_by_number.get(number)
        if finding is None:
            continue
        file_path = str(finding.get("file") or "")
        line = int(finding.get("line") or 0)
        fingerprint = _build_fingerprint(project, mr_iid, finding)
        anchor = _build_anchor(diff_index, file_path, line, diff_refs)
        sections = structured_finding_fields(finding)
        item = {
            "finding_number": number,
            "candidate_id": str(finding.get("candidate_id") or ""),
            "persona": _normalize_metric_label(finding.get("persona")),
            "severity": _normalize_metric_label(finding.get("severity")),
            "file": file_path,
            "line": line,
            "message": str(finding.get("message") or ""),
            "title": str(sections.get("title") or ""),
            "problem": str(sections.get("problem") or ""),
            "impact": str(sections.get("impact") or ""),
            "suggested_fixes": list(sections.get("suggested_fixes") or []),
            "fingerprint": fingerprint,
        }
        if anchor is None:
            item["status"] = "missing_anchor"
        else:
            comment_body = _build_comment_body(finding, fingerprint=fingerprint, run_id=str(manifest["run_id"]))
            request_path = _request_payload_path(comments_dir, finding)
            request_payload = {
                "body": comment_body,
                "position": _build_position_payload(anchor),
            }
            _write_json(request_path, request_payload)
            item["status"] = "ready"
            item["payload_file"] = str(request_path)
            item["anchor"] = anchor
        approved_findings.append(item)

    payload = {
        "contract_version": _POSTING_MANIFEST_CONTRACT,
        "run_id": manifest["run_id"],
        "project": project,
        "mr_iid": mr_iid,
        "prepared_at": _utc_now(),
        "approved_all": approved_all,
        "approved_finding_numbers": requested_numbers,
        "invalid_finding_numbers": invalid_numbers,
        "diff_refs": diff_refs,
        "approved_findings": approved_findings,
        "summary": _build_prepare_summary(approved_findings, invalid_numbers),
    }
    _write_json(Path(manifest["posting_manifest_file"]), payload)
    return payload


def _gitlab_api(glab_bin: str, project: str, path_suffix: str, *, method: str = "GET", input_file: Path | None = None) -> Any:
    encoded_project = quote(project, safe="")
    api_path = f"projects/{encoded_project}/{path_suffix.lstrip('/')}"
    cmd = [glab_bin, "api", api_path]
    if method.upper() != "GET":
        cmd.extend(["-X", method.upper()])
    if input_file is not None:
        cmd.extend(["-H", "Content-Type: application/json", "--input", str(input_file)])
    result = run_command(cmd)
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"glab api failed for {api_path}: {stderr}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"glab api returned invalid JSON for {api_path}: {exc}") from exc


def _list_discussions(glab_bin: str, project: str, mr_iid: int) -> list[dict[str, Any]]:
    discussions: list[dict[str, Any]] = []
    page = 1
    while True:
        payload = _gitlab_api(
            glab_bin,
            project,
            f"merge_requests/{mr_iid}/discussions?per_page=100&page={page}",
        )
        if not isinstance(payload, list):
            raise RuntimeError("GitLab discussions API returned a non-list payload")
        discussions.extend(item for item in payload if isinstance(item, dict))
        if len(payload) < 100:
            break
        page += 1
    return discussions


def _extract_existing_index(discussions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    existing: dict[str, dict[str, Any]] = {}
    for discussion in discussions:
        discussion_id = discussion.get("id")
        notes = discussion.get("notes") if isinstance(discussion.get("notes"), list) else []
        for note in notes:
            if not isinstance(note, dict):
                continue
            body = str(note.get("body") or "")
            match = _FINGERPRINT_RE.search(body)
            if match is None:
                continue
            fingerprint = match.group("fingerprint")
            existing[fingerprint] = {
                "discussion_id": discussion_id,
                "note_id": note.get("id"),
                "note": note,
                "discussion": discussion,
            }
    return existing


def _extract_diff_note_info(response: Any) -> tuple[str | int | None, str | int | None] | None:
    if not isinstance(response, dict):
        return None
    notes = response.get("notes") if isinstance(response.get("notes"), list) else []
    if not notes:
        return None
    first_note = notes[0] if isinstance(notes[0], dict) else None
    if not isinstance(first_note, dict):
        return None
    if str(first_note.get("type") or "") != "DiffNote":
        return None
    return response.get("id"), first_note.get("id")


def _write_response_snapshot(path: Path, payload: Any) -> None:
    if isinstance(payload, dict):
        _write_json(path, payload)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def apply_posting_plan(manifest_file: Path, *, approval_file: Path | None = None, glab_bin: str = "glab") -> dict[str, Any]:
    prepared = prepare_posting_manifest(manifest_file, approval_file=approval_file)
    manifest = _ensure_dict(_load_json_file(manifest_file), label="run manifest")
    comments_dir = Path(manifest["comments_dir"])
    project = str(prepared["project"])
    mr_iid = int(prepared["mr_iid"])
    started_at = _utc_now()
    started_mono = time.monotonic()

    discussions = _list_discussions(glab_bin, project, mr_iid)
    existing = _extract_existing_index(discussions)

    approved_numbers = _dedupe_ints(list(prepared.get("approved_finding_numbers") or []))
    invalid_numbers = set(_dedupe_ints(list(prepared.get("invalid_finding_numbers") or [])))
    findings_by_number = {
        int(item["finding_number"]): item
        for item in prepared.get("approved_findings", [])
        if isinstance(item, dict) and int(item.get("finding_number") or 0) > 0
    }

    results: list[dict[str, Any]] = []
    posted_count = 0
    already_posted_count = 0
    skipped_count = 0
    failed_count = 0

    for number in approved_numbers:
        if number in invalid_numbers:
            skipped_count += 1
            results.append(
                {
                    "finding_number": number,
                    **_finding_metric_context(None),
                    "status": "skipped_invalid_selection",
                    "response_file": None,
                    "discussion_id": None,
                    "note_id": None,
                    "error": "finding number was not present in verified_findings.json",
                    "attempts": 0,
                }
            )
            continue

        finding = findings_by_number.get(number)
        if finding is None:
            skipped_count += 1
            results.append(
                {
                    "finding_number": number,
                    **_finding_metric_context(None),
                    "status": "skipped_invalid_selection",
                    "response_file": None,
                    "discussion_id": None,
                    "note_id": None,
                    "error": "approved finding missing from posting manifest",
                    "attempts": 0,
                }
            )
            continue

        context = _finding_metric_context(finding)
        fingerprint = str(context["fingerprint"] or "")
        payload_file = str(context["payload_file"] or "") or None
        response_file = str(_response_payload_path(comments_dir, finding))

        if str(finding.get("status") or "") == "missing_anchor":
            skipped_count += 1
            results.append(
                {
                    "finding_number": number,
                    **context,
                    "status": "skipped_missing_anchor",
                    "response_file": None,
                    "discussion_id": None,
                    "note_id": None,
                    "error": "finding did not map to a diff position",
                    "attempts": 0,
                }
            )
            continue

        existing_hit = existing.get(fingerprint)
        if existing_hit is not None:
            already_posted_count += 1
            _write_response_snapshot(Path(response_file), _ensure_dict(existing_hit["discussion"], label="existing discussion"))
            results.append(
                {
                    "finding_number": number,
                    **context,
                    "status": "already_posted",
                    "response_file": response_file,
                    "discussion_id": existing_hit.get("discussion_id"),
                    "note_id": existing_hit.get("note_id"),
                    "error": None,
                    "attempts": 0,
                }
            )
            continue

        if payload_file is None:
            failed_count += 1
            results.append(
                {
                    "finding_number": number,
                    **context,
                    "status": "failed",
                    "payload_file": None,
                    "response_file": None,
                    "discussion_id": None,
                    "note_id": None,
                    "error": "ready finding is missing payload_file",
                    "attempts": 0,
                }
            )
            continue

        api_path = f"merge_requests/{mr_iid}/discussions"
        attempts = 0
        final_result: dict[str, Any] | None = None
        invalid_response = False
        last_error: str | None = None

        for attempt_index in range(2):
            attempts += 1
            try:
                response = _gitlab_api(glab_bin, project, api_path, method="POST", input_file=Path(payload_file))
            except Exception as exc:  # noqa: BLE001
                last_error = f"{type(exc).__name__}: {exc}"
                latest_discussions = _list_discussions(glab_bin, project, mr_iid)
                existing = _extract_existing_index(latest_discussions)
                existing_hit = existing.get(fingerprint)
                if existing_hit is not None:
                    already_posted_count += 1
                    _write_response_snapshot(Path(response_file), _ensure_dict(existing_hit["discussion"], label="existing discussion"))
                    final_result = {
                        "finding_number": number,
                        **context,
                        "status": "already_posted",
                        "response_file": response_file,
                        "discussion_id": existing_hit.get("discussion_id"),
                        "note_id": existing_hit.get("note_id"),
                        "error": last_error,
                        "attempts": attempts,
                    }
                    break
                if attempt_index == 1:
                    failed_count += 1
                    final_result = {
                        "finding_number": number,
                        **context,
                        "status": "failed",
                        "response_file": None,
                        "discussion_id": None,
                        "note_id": None,
                        "error": last_error,
                        "attempts": attempts,
                    }
                    break
                continue

            _write_response_snapshot(Path(response_file), response)
            note_info = _extract_diff_note_info(response)
            if note_info is None:
                invalid_response = True
                break

            discussion_id, note_id = note_info
            posted_count += 1
            final_result = {
                "finding_number": number,
                **context,
                "status": "posted",
                "response_file": response_file,
                "discussion_id": discussion_id,
                "note_id": note_id,
                "error": None,
                "attempts": attempts,
            }
            break

        if final_result is None and invalid_response:
            failed_count += 1
            final_result = {
                "finding_number": number,
                **context,
                "status": "invalid_response",
                "response_file": response_file,
                "discussion_id": None,
                "note_id": None,
                "error": "GitLab response did not contain a DiffNote",
                "attempts": attempts,
            }

        if final_result is None:
            failed_count += 1
            final_result = {
                "finding_number": number,
                **context,
                "status": "failed",
                "response_file": None,
                "discussion_id": None,
                "note_id": None,
                "error": last_error or "unknown posting failure",
                "attempts": attempts,
            }

        results.append(final_result)

    _write_json(Path(manifest["posting_manifest_file"]), prepared)
    finished_at = _utc_now()
    duration_ms = int((time.monotonic() - started_mono) * 1000)
    result_payload = {
        "contract_version": _POSTING_RESULT_CONTRACT,
        "run_id": manifest["run_id"],
        "project": project,
        "mr_iid": mr_iid,
        "approved_all": bool(prepared.get("approved_all")),
        "approved_finding_numbers": approved_numbers,
        "invalid_finding_numbers": sorted(invalid_numbers),
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": duration_ms,
        "posted_count": posted_count,
        "already_posted_count": already_posted_count,
        "skipped_count": skipped_count,
        "failed_count": failed_count,
        "summary": _build_result_summary(
            prepared,
            results,
            posted_count=posted_count,
            already_posted_count=already_posted_count,
            skipped_count=skipped_count,
            failed_count=failed_count,
        ),
        "results": results,
    }
    _write_json(Path(manifest["posting_results_file"]), result_payload)
    return result_payload


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ccr-post-comments",
        description="Prepare or apply deterministic CCR GitLab MR comment posting.",
    )
    parser.add_argument(
        "--manifest-file",
        required=True,
        help="Path to the run_manifest.json emitted by ccr_run.py / ccr_run_init.py.",
    )
    parser.add_argument(
        "--approval-file",
        default=None,
        help="Optional explicit path to posting_approval.json. Defaults to the manifest path.",
    )
    parser.add_argument(
        "--glab-bin",
        default="glab",
        help="glab executable to use for GitLab API requests (default: glab).",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--prepare-only",
        action="store_true",
        help="Build payload files and posting_manifest.json without posting to GitLab.",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Prepare the posting plan, check idempotency, and post ready findings.",
    )
    return parser


def main() -> None:
    parser = _build_arg_parser()
    args = parser.parse_args()
    manifest_file = Path(args.manifest_file).expanduser().resolve()
    approval_file = Path(args.approval_file).expanduser().resolve() if args.approval_file else None
    try:
        if args.prepare_only:
            payload = prepare_posting_manifest(manifest_file, approval_file=approval_file)
        else:
            payload = apply_posting_plan(manifest_file, approval_file=approval_file, glab_bin=args.glab_bin)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, indent=2), file=sys.stderr)
        sys.exit(1)
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
