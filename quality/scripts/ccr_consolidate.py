#!/usr/bin/env python3
"""Deterministic CCR candidate consolidation helper.

Builds enriched consolidated candidates from reviewer findings, deterministic route
metadata, and static-analysis artifacts.

Examples:
    python3 ccr_consolidate.py \
      --reviewer-results-file /tmp/reviewer_results.json \
      --route-plan-file /tmp/route_plan.json \
      --static-analysis-file /tmp/static_analysis.json \
      --output-file /tmp/candidates.json
"""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_SEVERITY_ORDER = {"bug": 0, "warning": 1, "info": 2}
_PRIMARY_PERSONA_ORDER = ("security", "logic", "concurrency", "performance", "requirements")
_PRIMARY_PERSONA_RANK = {name: index for index, name in enumerate(_PRIMARY_PERSONA_ORDER)}
_GENERIC_CATEGORY_TOKENS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "before",
    "can",
    "cause",
    "check",
    "code",
    "could",
    "current",
    "does",
    "ensure",
    "for",
    "from",
    "function",
    "handle",
    "if",
    "in",
    "is",
    "it",
    "its",
    "line",
    "logic",
    "make",
    "may",
    "missing",
    "more",
    "must",
    "need",
    "not",
    "path",
    "returns",
    "should",
    "that",
    "the",
    "their",
    "then",
    "there",
    "this",
    "through",
    "to",
    "use",
    "uses",
    "using",
    "when",
    "with",
}
_SYMBOL_CANDIDATE_RE = re.compile(r"\b([A-Z][A-Za-z0-9_]{2,}|[a-z_][A-Za-z0-9_]{2,})\b")
_SYMBOL_CALL_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_QUOTED_SYMBOL_RE = re.compile(r"[`'\"]([A-Za-z_][A-Za-z0-9_]*)[`'\"]")


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
    supporting_personas: list[str] = field(default_factory=list)
    support_count: int = 0
    available_pass_count: int = 0
    symbol: str | None = None
    normalized_category: str | None = None
    anchor_status: str = "unknown"
    source_findings: list[dict[str, Any]] = field(default_factory=list)
    prefilter: dict[str, Any] = field(default_factory=lambda: {"ready_for_verification": True, "drop_reasons": []})
    evidence_bundle: dict[str, Any] = field(
        default_factory=lambda: {
            "diff_hunk": None,
            "file_context": None,
            "requirements_excerpt": None,
            "static_analysis": [],
        }
    )

    def to_contract_dict(self) -> dict[str, Any]:
        return {
            "contract_version": "ccr.consolidated_candidate.v1",
            "candidate_id": self.candidate_id,
            "persona": self.persona,
            "severity": self.severity,
            "file": self.file,
            "line": self.line,
            "message": self.message,
            "reviewers": list(self.reviewers),
            "supporting_personas": list(self.supporting_personas),
            "consensus": self.consensus,
            "support_count": self.support_count,
            "available_pass_count": self.available_pass_count,
            "symbol": self.symbol,
            "normalized_category": self.normalized_category,
            "anchor_status": self.anchor_status,
            "evidence_sources": list(self.evidence_sources),
            "source_findings": [dict(item) for item in self.source_findings],
            "prefilter": {
                "ready_for_verification": bool(self.prefilter.get("ready_for_verification", True)),
                "drop_reasons": list(self.prefilter.get("drop_reasons", [])),
            },
            "evidence_bundle": {
                "diff_hunk": self.evidence_bundle.get("diff_hunk"),
                "file_context": self.evidence_bundle.get("file_context"),
                "requirements_excerpt": self.evidence_bundle.get("requirements_excerpt"),
                "static_analysis": [dict(item) for item in self.evidence_bundle.get("static_analysis", [])],
            },
        }


def _severity_rank(severity: str) -> int:
    return _SEVERITY_ORDER.get(severity, 99)


def _persona_rank(persona: str) -> int:
    return _PRIMARY_PERSONA_RANK.get(persona, len(_PRIMARY_PERSONA_ORDER))


def _message_key(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered


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


def _normalize_severity(raw: Any) -> str:
    severity = str(raw or "info").strip().lower()
    return severity if severity in _SEVERITY_ORDER else "info"


def _extract_symbol(message: str) -> str | None:
    preferred: list[str] = []
    fallback: list[str] = []
    for pattern in (_QUOTED_SYMBOL_RE, _SYMBOL_CALL_RE, _SYMBOL_CANDIDATE_RE):
        for match in pattern.finditer(message):
            symbol = match.group(1)
            lowered = symbol.lower()
            if lowered in _GENERIC_CATEGORY_TOKENS:
                continue
            if lowered in {"http", "json", "error", "errors", "token", "tokens", "context", "jwt", "jwts"}:
                continue
            if any(char.islower() for char in symbol) and any(char.isupper() for char in symbol):
                preferred.append(symbol)
            else:
                fallback.append(symbol)
    for pool in (preferred, fallback):
        if pool:
            return pool[0]
    return None


def _category_tokens(message: str, symbol: str | None = None) -> tuple[str, ...]:
    normalized = message.lower()
    if symbol:
        normalized = re.sub(rf"\b{re.escape(symbol.lower())}\b", " ", normalized)
    normalized = re.sub(r"[`'\"]([A-Za-z_][A-Za-z0-9_]*)[`'\"]", " ", normalized)
    normalized = re.sub(r"\b\d+\b", " ", normalized)
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    tokens: list[str] = []
    for token in normalized.split():
        if len(token) <= 2:
            continue
        if token in _GENERIC_CATEGORY_TOKENS:
            continue
        if token not in tokens:
            tokens.append(token)
    if not tokens:
        return ("generic",)
    if len(tokens) > 8:
        tokens = tokens[:8]
    return tuple(sorted(tokens))


def _normalized_category(message: str, symbol: str | None = None) -> str:
    tokens = _category_tokens(message, symbol=symbol)
    return "-".join(tokens)


def _jaccard_similarity(left: tuple[str, ...], right: tuple[str, ...]) -> float:
    left_set = set(left)
    right_set = set(right)
    if not left_set and not right_set:
        return 1.0
    if not left_set or not right_set:
        return 0.0
    return len(left_set & right_set) / len(left_set | right_set)


def _match_static_analysis_findings(static_analysis_payload: dict[str, Any], file_path: str, lines: list[int]) -> list[dict[str, Any]]:
    matched: list[dict[str, Any]] = []
    for tool_key in ("go_vet", "staticcheck", "gosec"):
        findings = static_analysis_payload.get(tool_key)
        if not isinstance(findings, list):
            continue
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            if str(finding.get("file") or "") != file_path:
                continue
            finding_line = int(finding.get("line", 0) or 0)
            if finding_line < 1:
                continue
            if any(abs(finding_line - line) <= 3 for line in lines):
                enriched = dict(finding)
                enriched.setdefault("tool", tool_key)
                matched.append(enriched)
    matched.sort(key=lambda item: (str(item.get("tool") or ""), int(item.get("line", 0) or 0), str(item.get("message") or "")))
    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, int, str]] = set()
    for item in matched:
        key = (str(item.get("tool") or ""), int(item.get("line", 0) or 0), str(item.get("message") or ""))
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _source_finding_sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
    return (
        str(item.get("file") or ""),
        int(item.get("line", 0) or 0),
        _severity_rank(str(item.get("severity") or "info")),
        str(item.get("persona") or ""),
        str(item.get("pass_name") or ""),
    )


def _normalize_source_finding(review: dict[str, Any], finding: dict[str, Any]) -> dict[str, Any] | None:
    file_path = str(finding.get("file") or "").strip()
    line = int(finding.get("line") or 0)
    message = str(finding.get("message") or "").strip()
    if not file_path or line < 1 or not message:
        return None
    severity = _normalize_severity(finding.get("severity"))
    symbol = _extract_symbol(message)
    category = _normalized_category(message, symbol=symbol)
    category_tokens = _category_tokens(message, symbol=symbol)
    return {
        "pass_name": str(review.get("pass_name") or ""),
        "persona": str(review.get("persona") or "").strip() or "logic",
        "provider": str(review.get("provider") or "") or None,
        "file": file_path,
        "line": line,
        "message": message,
        "severity": severity,
        "symbol": symbol,
        "normalized_category": category,
        "category_tokens": category_tokens,
    }


def _cluster_matches(item: dict[str, Any], cluster: list[dict[str, Any]]) -> bool:
    if not cluster:
        return False
    head = cluster[0]
    if item["file"] != head["file"]:
        return False
    head_line = int(head["line"])
    item_line = int(item["line"])
    close_lines = abs(item_line - head_line) <= 3
    same_symbol = bool(item.get("symbol") and head.get("symbol") and item["symbol"] == head["symbol"])
    overlap = _jaccard_similarity(tuple(item.get("category_tokens") or ()), tuple(head.get("category_tokens") or ()))
    same_category = (
        str(item.get("normalized_category") or "") == str(head.get("normalized_category") or "")
        or overlap >= 0.6
        or (same_symbol and overlap >= 0.5)
    )
    if not same_category:
        return False
    return same_symbol or close_lines


def _choose_primary_persona(personas: list[str]) -> str:
    if not personas:
        return "logic"
    ordered = sorted({persona for persona in personas if persona}, key=lambda item: (_persona_rank(item), item))
    return ordered[0] if ordered else "logic"


def _build_candidate_from_cluster(
    cluster: list[dict[str, Any]],
    *,
    candidate_id: str,
    pass_counts: dict[str, Any],
    static_analysis_payload: dict[str, Any],
) -> CandidateRecord:
    sorted_cluster = sorted(cluster, key=_source_finding_sort_key)
    reviewers = sorted({str(item.get("pass_name") or "") for item in sorted_cluster if str(item.get("pass_name") or "")})
    personas = [str(item.get("persona") or "") for item in sorted_cluster if str(item.get("persona") or "")]
    primary_persona = _choose_primary_persona(personas)
    supporting_personas = [
        persona
        for persona in sorted({persona for persona in personas if persona}, key=lambda item: (_persona_rank(item), item))
        if persona != primary_persona
    ]
    severity = min((_normalize_severity(item.get("severity")) for item in sorted_cluster), key=_severity_rank)
    file_path = str(sorted_cluster[0]["file"])
    line = min(int(item["line"]) for item in sorted_cluster)
    messages = [str(item.get("message") or "") for item in sorted_cluster]
    symbol_candidates = [str(item.get("symbol") or "") for item in sorted_cluster if item.get("symbol")]
    symbol = sorted(symbol_candidates, key=lambda value: (-symbol_candidates.count(value), value))[0] if symbol_candidates else None
    category_candidates = [str(item.get("normalized_category") or "") for item in sorted_cluster if str(item.get("normalized_category") or "")]
    normalized_category = sorted(category_candidates, key=lambda value: (-category_candidates.count(value), value))[0] if category_candidates else None
    support_count = len(reviewers)
    available_pass_count = max(
        [int(pass_counts.get(persona, 0) or 0) for persona in {primary_persona, *supporting_personas}] or [support_count]
    )
    if available_pass_count <= 0:
        available_pass_count = support_count
    matched_static_analysis = _match_static_analysis_findings(
        static_analysis_payload,
        file_path,
        [int(item["line"]) for item in sorted_cluster],
    )
    evidence_sources = ["reviewer", "diff_hunk"]
    evidence_sources.extend(str(item.get("tool") or "") for item in matched_static_analysis if str(item.get("tool") or ""))
    source_findings = [
        {
            "pass_name": str(item.get("pass_name") or ""),
            "provider": item.get("provider"),
            "persona": str(item.get("persona") or ""),
            "file": str(item.get("file") or ""),
            "line": int(item.get("line") or 0),
            "severity": _normalize_severity(item.get("severity")),
            "message": str(item.get("message") or ""),
        }
        for item in sorted_cluster
    ]
    return CandidateRecord(
        candidate_id=candidate_id,
        persona=primary_persona,
        severity=severity,
        file=file_path,
        line=line,
        message=_combine_messages(messages),
        reviewers=reviewers,
        supporting_personas=supporting_personas,
        consensus=f"{support_count}/{available_pass_count}",
        support_count=support_count,
        available_pass_count=available_pass_count,
        symbol=symbol,
        normalized_category=normalized_category,
        anchor_status="unknown",
        evidence_sources=_dedupe_preserve_order([item for item in evidence_sources if item]),
        source_findings=source_findings,
        prefilter={"ready_for_verification": True, "drop_reasons": []},
        evidence_bundle={
            "diff_hunk": None,
            "file_context": None,
            "requirements_excerpt": None,
            "static_analysis": matched_static_analysis,
        },
    )


def build_candidates_manifest(
    reviewer_results: list[dict[str, Any]],
    *,
    route_plan: dict[str, Any],
    static_analysis_payload: dict[str, Any],
) -> dict[str, Any]:
    pass_counts = route_plan.get("pass_counts") if isinstance(route_plan.get("pass_counts"), dict) else {}
    flattened: list[dict[str, Any]] = []
    skipped_invalid = 0
    for review in reviewer_results:
        if not isinstance(review, dict):
            continue
        output = review.get("result") if isinstance(review.get("result"), dict) else {}
        findings = output.get("findings") if isinstance(output.get("findings"), list) else []
        for finding in findings:
            if not isinstance(finding, dict):
                skipped_invalid += 1
                continue
            normalized = _normalize_source_finding(review, finding)
            if normalized is None:
                skipped_invalid += 1
                continue
            flattened.append(normalized)

    flattened.sort(
        key=lambda item: (
            str(item.get("file") or ""),
            int(item.get("line") or 0),
            str(item.get("normalized_category") or ""),
            _severity_rank(str(item.get("severity") or "info")),
            str(item.get("pass_name") or ""),
            str(item.get("persona") or ""),
        )
    )

    clusters: list[list[dict[str, Any]]] = []
    for item in flattened:
        assigned = False
        for cluster in clusters:
            if _cluster_matches(item, cluster):
                cluster.append(item)
                assigned = True
                break
        if not assigned:
            clusters.append([item])

    provisional_candidates: list[CandidateRecord] = []
    for cluster in clusters:
        provisional_candidates.append(
            _build_candidate_from_cluster(
                cluster,
                candidate_id="",
                pass_counts=pass_counts,
                static_analysis_payload=static_analysis_payload,
            )
        )

    provisional_candidates.sort(
        key=lambda item: (
            _persona_rank(item.persona),
            _severity_rank(item.severity),
            item.file,
            item.line,
            item.normalized_category or "",
            item.symbol or "",
            item.message,
        )
    )

    candidates: list[CandidateRecord] = []
    for index, candidate in enumerate(provisional_candidates, start=1):
        candidates.append(
            CandidateRecord(
                candidate_id=f"F{index}",
                persona=candidate.persona,
                severity=candidate.severity,
                file=candidate.file,
                line=candidate.line,
                message=candidate.message,
                reviewers=list(candidate.reviewers),
                supporting_personas=list(candidate.supporting_personas),
                consensus=candidate.consensus,
                support_count=candidate.support_count,
                available_pass_count=candidate.available_pass_count,
                symbol=candidate.symbol,
                normalized_category=candidate.normalized_category,
                anchor_status=candidate.anchor_status,
                evidence_sources=list(candidate.evidence_sources),
                source_findings=[dict(item) for item in candidate.source_findings],
                prefilter=dict(candidate.prefilter),
                evidence_bundle={
                    "diff_hunk": candidate.evidence_bundle.get("diff_hunk"),
                    "file_context": candidate.evidence_bundle.get("file_context"),
                    "requirements_excerpt": candidate.evidence_bundle.get("requirements_excerpt"),
                    "static_analysis": [dict(item) for item in candidate.evidence_bundle.get("static_analysis", [])],
                },
            )
        )

    summary = {
        "candidate_count": len(candidates),
        "source_finding_count": len(flattened),
        "skipped_invalid_finding_count": skipped_invalid,
    }
    payload = {
        "contract_version": "ccr.candidates_manifest.v1",
        "candidates": [candidate.to_contract_dict() for candidate in candidates],
        "summary": summary,
    }
    return payload


def build_candidates(
    reviewer_results: list[dict[str, Any]],
    *,
    route_plan: dict[str, Any],
    static_analysis_payload: dict[str, Any],
) -> tuple[list[CandidateRecord], dict[str, Any]]:
    payload = build_candidates_manifest(
        reviewer_results,
        route_plan=route_plan,
        static_analysis_payload=static_analysis_payload,
    )
    candidates = [
        CandidateRecord(
            candidate_id=str(item.get("candidate_id") or ""),
            persona=str(item.get("persona") or "logic"),
            severity=_normalize_severity(item.get("severity")),
            file=str(item.get("file") or ""),
            line=int(item.get("line") or 0),
            message=str(item.get("message") or ""),
            reviewers=list(item.get("reviewers") or []),
            supporting_personas=list(item.get("supporting_personas") or []),
            consensus=str(item.get("consensus") or "0/0"),
            support_count=int(item.get("support_count") or 0),
            available_pass_count=int(item.get("available_pass_count") or 0),
            symbol=item.get("symbol") if isinstance(item.get("symbol"), str) or item.get("symbol") is None else None,
            normalized_category=item.get("normalized_category") if isinstance(item.get("normalized_category"), str) or item.get("normalized_category") is None else None,
            anchor_status=str(item.get("anchor_status") or "unknown"),
            evidence_sources=list(item.get("evidence_sources") or []),
            source_findings=[dict(entry) for entry in item.get("source_findings") or [] if isinstance(entry, dict)],
            prefilter=dict(item.get("prefilter") or {"ready_for_verification": True, "drop_reasons": []}),
            evidence_bundle={
                "diff_hunk": (item.get("evidence_bundle") or {}).get("diff_hunk") if isinstance(item.get("evidence_bundle"), dict) else None,
                "file_context": (item.get("evidence_bundle") or {}).get("file_context") if isinstance(item.get("evidence_bundle"), dict) else None,
                "requirements_excerpt": (item.get("evidence_bundle") or {}).get("requirements_excerpt") if isinstance(item.get("evidence_bundle"), dict) else None,
                "static_analysis": [
                    dict(entry)
                    for entry in ((item.get("evidence_bundle") or {}).get("static_analysis") if isinstance(item.get("evidence_bundle"), dict) else []) or []
                    if isinstance(entry, dict)
                ],
            },
        )
        for item in payload.get("candidates", [])
        if isinstance(item, dict)
    ]
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    return candidates, summary


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_reviewer_results(path: Path) -> list[dict[str, Any]]:
    payload = _load_json(path)
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("reviewer_results", "results", "passes"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    raise ValueError(f"reviewer results file has unsupported shape: {path}")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ccr-consolidate",
        description="Build deterministic consolidated CCR candidates from reviewer results.",
    )
    parser.add_argument("--reviewer-results-file", required=True, help="JSON file containing reviewer result records.")
    parser.add_argument("--route-plan-file", required=True, help="Route plan JSON file.")
    parser.add_argument("--static-analysis-file", required=True, help="Static analysis JSON file.")
    parser.add_argument("--output-file", required=True, help="Where to write the candidates manifest JSON.")
    return parser


def main() -> None:
    args = _build_arg_parser().parse_args()
    reviewer_results = _load_reviewer_results(Path(args.reviewer_results_file).expanduser().resolve())
    route_plan = _load_json(Path(args.route_plan_file).expanduser().resolve())
    static_analysis_payload = _load_json(Path(args.static_analysis_file).expanduser().resolve())
    payload = build_candidates_manifest(
        reviewer_results,
        route_plan=route_plan if isinstance(route_plan, dict) else {},
        static_analysis_payload=static_analysis_payload if isinstance(static_analysis_payload, dict) else {},
    )
    output_path = Path(args.output_file).expanduser().resolve()
    _write_json(output_path, payload)
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
