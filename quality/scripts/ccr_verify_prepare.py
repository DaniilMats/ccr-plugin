#!/usr/bin/env python3
"""Deterministic CCR verification-preparation helper.

Enriches consolidated candidates with diff/file evidence, applies deterministic
prefilters, writes verification batches, and emits `verification_prepare.json`.

Examples:
    python3 ccr_verify_prepare.py \
      --candidates-file /tmp/candidates.json \
      --artifact-file /tmp/review_artifact.txt \
      --verify-batch-dir /tmp/verify_batches \
      --output-file /tmp/verification_prepare.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ccr_consolidate import CandidateRecord
from ccr_run_init import _write_json

_DIFF_HEADER_RE = re.compile(r"^diff --git a/(.+?) b/(.+)$")
_HUNK_HEADER_RE = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? \+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@"
)


def _utc_now() -> str:
    import time

    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _load_json_file(path: Path, *, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered


def _candidate_from_contract(item: dict[str, Any]) -> CandidateRecord:
    evidence_bundle = item.get("evidence_bundle") if isinstance(item.get("evidence_bundle"), dict) else {}
    prefilter = item.get("prefilter") if isinstance(item.get("prefilter"), dict) else {}
    return CandidateRecord(
        candidate_id=str(item.get("candidate_id") or ""),
        persona=str(item.get("persona") or "logic"),
        severity=str(item.get("severity") or "info"),
        file=str(item.get("file") or ""),
        line=int(item.get("line") or 0),
        message=str(item.get("message") or ""),
        reviewers=[str(entry) for entry in (item.get("reviewers") or [])],
        consensus=str(item.get("consensus") or "0/0"),
        evidence_sources=[str(entry) for entry in (item.get("evidence_sources") or [])],
        supporting_personas=[str(entry) for entry in (item.get("supporting_personas") or [])],
        support_count=int(item.get("support_count") or 0),
        available_pass_count=int(item.get("available_pass_count") or 0),
        symbol=str(item.get("symbol")) if isinstance(item.get("symbol"), str) else None,
        normalized_category=str(item.get("normalized_category")) if isinstance(item.get("normalized_category"), str) else None,
        anchor_status=str(item.get("anchor_status") or "unknown"),
        source_findings=[dict(entry) for entry in (item.get("source_findings") or []) if isinstance(entry, dict)],
        prefilter={
            "ready_for_verification": bool(prefilter.get("ready_for_verification", True)),
            "drop_reasons": [str(entry) for entry in (prefilter.get("drop_reasons") or [])],
        },
        evidence_bundle={
            "diff_hunk": evidence_bundle.get("diff_hunk"),
            "file_context": evidence_bundle.get("file_context"),
            "requirements_excerpt": evidence_bundle.get("requirements_excerpt"),
            "static_analysis": [dict(entry) for entry in (evidence_bundle.get("static_analysis") or []) if isinstance(entry, dict)],
        },
    )


def _load_candidates_manifest(path: Path) -> tuple[list[CandidateRecord], dict[str, Any]]:
    payload = _load_json_file(path, default={})
    if not isinstance(payload, dict):
        raise ValueError(f"candidates file has unsupported shape: {path}")
    items = payload.get("candidates") if isinstance(payload.get("candidates"), list) else []
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    candidates = [_candidate_from_contract(item) for item in items if isinstance(item, dict)]
    return candidates, summary


def _split_diff_blocks(diff_text: str) -> dict[str, str]:
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


def _parse_diff_hunks(file_block: str) -> list[dict[str, Any]]:
    lines = file_block.splitlines()
    hunks: list[dict[str, Any]] = []
    current_hunk_lines: list[str] = []
    current_new_lines: set[int] = set()
    current_old_lines: set[int] = set()
    old_line = 0
    new_line = 0

    def finish_current() -> None:
        if not current_hunk_lines:
            return
        hunks.append(
            {
                "text": "\n".join(current_hunk_lines).strip(),
                "new_lines": set(current_new_lines),
                "old_lines": set(current_old_lines),
            }
        )

    for line in lines:
        header_match = _HUNK_HEADER_RE.match(line)
        if header_match:
            finish_current()
            current_hunk_lines = [line]
            current_new_lines = set()
            current_old_lines = set()
            old_line = int(header_match.group("old_start"))
            new_line = int(header_match.group("new_start"))
            continue
        if not current_hunk_lines:
            continue
        current_hunk_lines.append(line)
        if line.startswith("+") and not line.startswith("+++"):
            current_new_lines.add(new_line)
            new_line += 1
            continue
        if line.startswith("-") and not line.startswith("---"):
            current_old_lines.add(old_line)
            old_line += 1
            continue
        if line.startswith(" "):
            current_old_lines.add(old_line)
            current_new_lines.add(new_line)
            old_line += 1
            new_line += 1
            continue
        if line.startswith("\\"):
            continue
        current_old_lines.add(old_line)
        current_new_lines.add(new_line)
        old_line += 1
        new_line += 1

    finish_current()
    return hunks


def _build_diff_index(diff_text: str) -> dict[str, dict[str, Any]]:
    return {
        file_path: {
            "text": block,
            "hunks": _parse_diff_hunks(block),
        }
        for file_path, block in _split_diff_blocks(diff_text).items()
    }


def _find_matching_hunk(file_entry: dict[str, Any] | None, target_line: int) -> str | None:
    if not isinstance(file_entry, dict):
        return None
    hunks = file_entry.get("hunks") if isinstance(file_entry.get("hunks"), list) else []
    for hunk in hunks:
        if not isinstance(hunk, dict):
            continue
        new_lines = hunk.get("new_lines") if isinstance(hunk.get("new_lines"), set) else set(hunk.get("new_lines") or [])
        old_lines = hunk.get("old_lines") if isinstance(hunk.get("old_lines"), set) else set(hunk.get("old_lines") or [])
        if target_line in new_lines or target_line in old_lines:
            text = hunk.get("text")
            return str(text) if text else None
    return None


def _extract_file_context(project_dir: Path | None, rel_path: str, target_lines: list[int], radius: int = 20) -> str | None:
    if project_dir is None:
        return None
    path = project_dir / rel_path
    if not path.is_file():
        return None
    lines = _read_text(path).splitlines()
    if not lines:
        return None
    valid_targets = [line for line in target_lines if 1 <= line <= len(lines)]
    if not valid_targets:
        return None
    start = max(1, min(valid_targets) - radius)
    end = min(len(lines), max(valid_targets) + radius)
    snippet = [f"{line_no:4d}: {lines[line_no - 1]}" for line_no in range(start, end + 1)]
    return "\n".join(snippet)


def _requirements_excerpt(requirements_text: str, limit: int = 1200) -> str | None:
    text = requirements_text.strip()
    if not text:
        return None
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _has_concrete_source(candidate: CandidateRecord, requirements_excerpt: str | None) -> bool:
    if candidate.source_findings:
        return True
    static_analysis = candidate.evidence_bundle.get("static_analysis") if isinstance(candidate.evidence_bundle, dict) else []
    if isinstance(static_analysis, list) and static_analysis:
        return True
    if requirements_excerpt:
        return True
    return False


def _candidate_contract_with_prefilter(candidate: CandidateRecord) -> dict[str, Any]:
    payload = candidate.to_contract_dict()
    payload["ready_for_verification"] = bool(candidate.prefilter.get("ready_for_verification", True))
    payload["drop_reasons"] = list(candidate.prefilter.get("drop_reasons", []))
    return payload


def _prepare_candidate(
    candidate: CandidateRecord,
    *,
    diff_index: dict[str, dict[str, Any]],
    project_dir: Path | None,
    requirements_text: str,
) -> CandidateRecord:
    file_entry = diff_index.get(candidate.file)
    diff_hunk = _find_matching_hunk(file_entry, candidate.line)
    file_context = _extract_file_context(project_dir, candidate.file, [candidate.line])
    requirements_excerpt = _requirements_excerpt(requirements_text)

    anchor_status = "diff" if diff_hunk else ("file_context" if file_context else "missing")
    drop_reasons: list[str] = []
    local_file_exists = bool(project_dir and (project_dir / candidate.file).is_file())

    if candidate.line < 1:
        drop_reasons.append("invalid_line")
    if not file_entry and not local_file_exists:
        drop_reasons.append("missing_file")
    if anchor_status == "missing":
        drop_reasons.append("missing_anchor")
    if not diff_hunk and not file_context:
        drop_reasons.append("missing_evidence")
    if not _has_concrete_source(candidate, requirements_excerpt):
        drop_reasons.append("missing_concrete_source")

    evidence_sources = list(candidate.evidence_sources)
    if diff_hunk:
        evidence_sources.append("diff_hunk")
    if file_context:
        evidence_sources.append("file_context")
    if requirements_excerpt:
        evidence_sources.append("requirements")

    existing_bundle = candidate.evidence_bundle if isinstance(candidate.evidence_bundle, dict) else {}
    updated_bundle = {
        "diff_hunk": diff_hunk,
        "file_context": file_context,
        "requirements_excerpt": requirements_excerpt,
        "static_analysis": [dict(item) for item in (existing_bundle.get("static_analysis") or []) if isinstance(item, dict)],
    }

    deduped_reasons = _dedupe_preserve_order([reason for reason in drop_reasons if reason])
    return replace(
        candidate,
        anchor_status=anchor_status,
        evidence_sources=_dedupe_preserve_order([item for item in evidence_sources if item]),
        prefilter={
            "ready_for_verification": not deduped_reasons,
            "drop_reasons": deduped_reasons,
        },
        evidence_bundle=updated_bundle,
    )


def _write_verification_batches(
    candidates: list[CandidateRecord],
    *,
    diff_index: dict[str, dict[str, Any]],
    project_dir: Path | None,
    requirements_text: str,
    verify_batch_dir: Path,
) -> list[dict[str, Any]]:
    verify_batch_dir.mkdir(parents=True, exist_ok=True)
    grouped_by_file: dict[str, list[CandidateRecord]] = {}
    for candidate in candidates:
        grouped_by_file.setdefault(candidate.file, []).append(candidate)

    batches: list[dict[str, Any]] = []
    batch_index = 1
    for file_path in sorted(grouped_by_file):
        file_candidates = sorted(grouped_by_file[file_path], key=lambda item: (item.line, item.candidate_id))
        file_entry = diff_index.get(file_path) or {}
        for offset in range(0, len(file_candidates), 5):
            chunk = file_candidates[offset : offset + 5]
            batch_payload = {
                "contract_version": "ccr.verification_batch.v1",
                "file": file_path,
                "diff_hunk": str(file_entry.get("text") or ""),
                "file_context": _extract_file_context(project_dir, file_path, [candidate.line for candidate in chunk]) or "",
                "requirements": requirements_text,
                "candidates": [
                    {
                        "candidate_id": candidate.candidate_id,
                        "file": candidate.file,
                        "line": candidate.line,
                        "message": candidate.message,
                        "persona": candidate.persona,
                        "severity": candidate.severity,
                        "reviewers": list(candidate.reviewers),
                        "consensus": candidate.consensus,
                        "symbol": candidate.symbol,
                        "anchor_status": candidate.anchor_status,
                        "evidence_sources": list(candidate.evidence_sources),
                        "source_findings": [dict(item) for item in candidate.source_findings],
                        "evidence_bundle": {
                            "diff_hunk": candidate.evidence_bundle.get("diff_hunk"),
                            "file_context": candidate.evidence_bundle.get("file_context"),
                            "requirements_excerpt": candidate.evidence_bundle.get("requirements_excerpt"),
                            "static_analysis": [dict(item) for item in candidate.evidence_bundle.get("static_analysis", [])],
                        },
                        "prefilter": {
                            "ready_for_verification": bool(candidate.prefilter.get("ready_for_verification", True)),
                            "drop_reasons": list(candidate.prefilter.get("drop_reasons", [])),
                        },
                    }
                    for candidate in chunk
                ],
            }
            batch_path = verify_batch_dir / f"verify_batch_{batch_index:03d}.json"
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


def prepare_verification_artifacts(
    candidates: list[CandidateRecord],
    *,
    artifact_text: str,
    project_dir: Path | None,
    requirements_text: str,
    verify_batch_dir: Path,
    output_file: Path | None = None,
) -> dict[str, Any]:
    diff_index = _build_diff_index(artifact_text)
    prepared_candidates = [
        _prepare_candidate(
            candidate,
            diff_index=diff_index,
            project_dir=project_dir,
            requirements_text=requirements_text,
        )
        for candidate in candidates
    ]
    ready_candidates = [candidate for candidate in prepared_candidates if bool(candidate.prefilter.get("ready_for_verification", True))]
    dropped_candidates = [candidate for candidate in prepared_candidates if not bool(candidate.prefilter.get("ready_for_verification", True))]
    batches = _write_verification_batches(
        ready_candidates,
        diff_index=diff_index,
        project_dir=project_dir,
        requirements_text=requirements_text,
        verify_batch_dir=verify_batch_dir,
    )
    payload = {
        "contract_version": "ccr.verification_prepare.v1",
        "prepared_at": _utc_now(),
        "ready_candidates": [_candidate_contract_with_prefilter(candidate) for candidate in ready_candidates],
        "dropped_candidates": [_candidate_contract_with_prefilter(candidate) for candidate in dropped_candidates],
        "batches": [
            {
                "batch_id": batch["batch_id"],
                "batch_file": batch["batch_file"],
                "file": batch.get("payload", {}).get("file") if isinstance(batch.get("payload"), dict) else None,
                "candidate_ids": [
                    str(item.get("candidate_id") or "")
                    for item in (batch.get("payload", {}).get("candidates") if isinstance(batch.get("payload"), dict) else [])
                    if isinstance(item, dict) and item.get("candidate_id")
                ],
                "candidate_count": len(
                    [
                        item
                        for item in (batch.get("payload", {}).get("candidates") if isinstance(batch.get("payload"), dict) else [])
                        if isinstance(item, dict)
                    ]
                ),
            }
            for batch in batches
        ],
        "summary": {
            "candidate_count": len(prepared_candidates),
            "ready_count": len(ready_candidates),
            "dropped_count": len(dropped_candidates),
            "batch_count": len(batches),
        },
    }
    if output_file is not None:
        _write_json(output_file, payload)
    return {
        "prepared_candidates": prepared_candidates,
        "ready_candidates": ready_candidates,
        "dropped_candidates": dropped_candidates,
        "batches": batches,
        "payload": payload,
    }


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ccr-verify-prepare",
        description="Prepare deterministic CCR verification batches and artifacts.",
    )
    parser.add_argument("--candidates-file", required=True, help="Candidates manifest JSON file.")
    parser.add_argument("--artifact-file", required=True, help="Review artifact / diff text file.")
    parser.add_argument("--verify-batch-dir", required=True, help="Directory where verifier batch JSON files should be written.")
    parser.add_argument("--output-file", required=True, help="Where to write verification_prepare.json.")
    parser.add_argument("--project-dir", default=None, help="Optional checkout root for file-context extraction.")
    parser.add_argument("--requirements-file", default=None, help="Optional requirements/spec text file.")
    return parser


def main() -> None:
    args = _build_arg_parser().parse_args()
    candidates, _summary = _load_candidates_manifest(Path(args.candidates_file).expanduser().resolve())
    artifact_text = _read_text(Path(args.artifact_file).expanduser().resolve())
    requirements_text = ""
    if args.requirements_file:
        requirements_text = _read_text(Path(args.requirements_file).expanduser().resolve())
    project_dir = Path(args.project_dir).expanduser().resolve() if args.project_dir else None
    result = prepare_verification_artifacts(
        candidates,
        artifact_text=artifact_text,
        project_dir=project_dir,
        requirements_text=requirements_text,
        verify_batch_dir=Path(args.verify_batch_dir).expanduser().resolve(),
        output_file=Path(args.output_file).expanduser().resolve(),
    )
    print(json.dumps(result["payload"], indent=2))


if __name__ == "__main__":
    main()
