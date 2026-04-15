#!/usr/bin/env python3
"""Deterministic pre-review context synthesis for CCR.

Builds a non-judgmental review-preparation artifact from the review diff,
requirements/spec text, and repository/package context. The output is meant to
improve downstream reviewer prompts without generating findings or verdicts.
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ccr_runtime.common import load_json_file, read_text, write_json

_DIFF_HEADER_RE = re.compile(r"^diff --git a/(.+?) b/(.+)$")
_BOOLISH_IDENTIFIER_RE = re.compile(
    r"\b(?:is|has|should|can|allow|omit|hide|show|enable|disable|use|need|needs|require|requires)[A-Z][A-Za-z0-9]*\b"
    r"|\b(?:is|has|should|can|allow|omit|hide|show|enable|disable|use|need|needs|require|requires)_[a-z0-9_]+\b"
    r"|\b[a-z][a-z0-9_]*(?:_enabled|_disabled|_empty|_present|_visible|_hidden)\b"
)
_CONDITIONAL_CUES = (
    "only if",
    "only when",
    "unless",
    "except",
    "if ",
    "when ",
    "while ",
    "empty",
    "non-empty",
    "hide",
    "show",
    "visible",
    "hidden",
    "state",
    "placeholder",
    "loading",
    "fallback",
)
_STATE_TERMS = (
    "trusted",
    "untrusted",
    "loading",
    "placeholder",
    "fallback",
    "empty",
    "non-empty",
    "history",
    "transaction",
    "transactions",
    "state",
    "visible",
    "hidden",
    "show",
    "hide",
)
_VISIBILITY_TERMS = ("show", "hide", "visible", "hidden", "placeholder", "widget")


def _normalize_line(text: str) -> str:
    return " ".join(text.strip().lstrip("-*•").split())


def _extract_changed_files(artifact_text: str) -> list[str]:
    files: list[str] = []
    for line in artifact_text.splitlines():
        match = _DIFF_HEADER_RE.match(line.strip())
        if not match:
            continue
        path = match.group(2).strip()
        if path not in files:
            files.append(path)
    return files


def _extract_requirement_clauses(requirements_text: str) -> list[dict[str, Any]]:
    clauses: list[dict[str, Any]] = []
    for raw_line in requirements_text.splitlines():
        line = _normalize_line(raw_line)
        if not line:
            continue
        lowered = line.lower()
        kind = "generic"
        if any(term in lowered for term in _VISIBILITY_TERMS):
            kind = "visibility"
        if any(term in lowered for term in ("state", "trusted", "untrusted", "placeholder", "loading", "fallback")):
            kind = "state"
        if any(term in lowered for term in ("empty", "non-empty", "history", "transaction", "transactions")):
            kind = "data_presence"
        clauses.append(
            {
                "id": f"R{len(clauses) + 1}",
                "text": line,
                "kind": kind,
                "conditional": any(cue in lowered for cue in _CONDITIONAL_CUES),
            }
        )
    return clauses


def _extract_identifiers(text: str, *, limit: int = 8) -> list[str]:
    identifiers: list[str] = []
    for match in _BOOLISH_IDENTIFIER_RE.finditer(text):
        token = match.group(0)
        if token not in identifiers:
            identifiers.append(token)
        if len(identifiers) >= limit:
            break
    return identifiers


def _extract_state_terms(text: str, *, limit: int = 8) -> list[str]:
    lowered = text.lower()
    return [term for term in _STATE_TERMS if term in lowered][:limit]


def _extract_diff_conditionals(artifact_text: str, *, limit: int = 12) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    for raw_line in artifact_text.splitlines():
        if not raw_line.startswith(("+", "-")) or raw_line.startswith(("+++", "---")):
            continue
        sign = "added" if raw_line.startswith("+") else "removed"
        line = raw_line[1:].strip()
        lowered = line.lower()
        if not line:
            continue
        if (
            "if " in lowered
            or any(term in lowered for term in _VISIBILITY_TERMS)
            or any(term in lowered for term in ("&&", "||"))
        ):
            results.append({"change": sign, "text": line})
        if len(results) >= limit:
            break
    return results


def _extract_context_snippets(review_context_text: str, tokens: list[str], files: list[str], *, limit: int = 10) -> list[dict[str, str]]:
    if not review_context_text.strip():
        return []
    search_terms = [token for token in tokens if token]
    search_terms.extend(Path(path).name for path in files)
    lowered_terms = [term.lower() for term in search_terms if term]

    snippets: list[dict[str, str]] = []
    for raw_line in review_context_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lowered = line.lower()
        if lowered_terms and not any(term in lowered for term in lowered_terms):
            continue
        snippet_type = "context"
        if "test" in lowered:
            snippet_type = "test"
        elif any(path.lower() in lowered for path in files):
            snippet_type = "file"
        snippets.append({"type": snippet_type, "text": line})
        if len(snippets) >= limit:
            break
    return snippets


def _build_dimensions(identifiers: list[str], state_terms: list[str], clauses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    dimensions: list[dict[str, Any]] = []
    for identifier in identifiers[:2]:
        dimensions.append(
            {
                "name": identifier,
                "type": "boolean",
                "values": [True, False],
                "source": "symbol",
            }
        )

    lowered_clause_text = "\n".join(clause["text"].lower() for clause in clauses)
    if any(term in lowered_clause_text for term in ("empty", "non-empty", "history", "transaction", "transactions")) or any(
        token in state_terms for token in ("empty", "non-empty", "history", "transaction", "transactions")
    ):
        dimensions.append(
            {
                "name": "data_presence",
                "type": "enum",
                "values": ["empty", "non_empty"],
                "source": "requirements",
            }
        )

    if any(value in state_terms for value in ("trusted", "untrusted")):
        dimensions.append(
            {
                "name": "device_state",
                "type": "enum",
                "values": ["trusted", "untrusted"],
                "source": "requirements+diff",
            }
        )

    render_state_values = [value for value in ("loading", "placeholder", "fallback") if value in state_terms]
    if render_state_values and len(dimensions) < 3:
        dimensions.append(
            {
                "name": "render_state",
                "type": "enum",
                "values": render_state_values[:2],
                "source": "requirements+diff",
            }
        )

    return dimensions


def _build_cases(dimensions: list[dict[str, Any]], requirement_ids: list[str], *, limit: int = 8) -> list[dict[str, Any]]:
    if not dimensions:
        return []
    chosen = dimensions[:3]
    value_sets = [dimension["values"][:2] for dimension in chosen]
    cases: list[dict[str, Any]] = []
    for index, combo in enumerate(itertools.product(*value_sets), start=1):
        inputs = {dimension["name"]: value for dimension, value in zip(chosen, combo)}
        cases.append(
            {
                "id": f"C{index}",
                "inputs": inputs,
                "check": "Compare this combination against the requirement clauses and sibling branches before deciding the diff is correct.",
                "requirement_ids": requirement_ids,
            }
        )
        if len(cases) >= limit:
            break
    return cases


def _build_invariants(identifiers: list[str], dimensions: list[dict[str, Any]], clauses: list[dict[str, Any]], context_snippets: list[dict[str, str]]) -> list[str]:
    invariants: list[str] = []
    dimension_names = {item["name"] for item in dimensions}
    if identifiers and "data_presence" in dimension_names:
        invariants.append(
            f"Requirement-derived visibility semantics mention both control flag(s) ({', '.join(identifiers[:2])}) and whether relevant data/history is empty."
        )
    if any(item["name"] == "device_state" for item in dimensions):
        invariants.append("State-specific branches should preserve the same underlying predicate unless the requirement explicitly changes behavior by state.")
    if context_snippets:
        invariants.append("Nearby code/tests are context for comparison, not proof that the changed branch preserves the requirement.")
    if not invariants and clauses:
        invariants.append("Requirement clauses should remain traceable to concrete branch predicates after the change.")
    return invariants


def _build_questions(dimensions: list[dict[str, Any]], diff_conditionals: list[dict[str, str]], context_snippets: list[dict[str, str]]) -> list[str]:
    questions: list[str] = []
    dimension_names = {item["name"] for item in dimensions}
    if any(item["type"] == "boolean" for item in dimensions) and "data_presence" in dimension_names:
        questions.append("Does every relevant branch preserve both the control flag and the data-presence predicate?")
    if "device_state" in dimension_names or "render_state" in dimension_names:
        questions.append("Do sibling state branches (for example trusted/untrusted/loading/fallback) use predicate parity for the same requirement?")
    if diff_conditionals:
        questions.append("Did the patch remove one operand from an existing conditional while fixing another one?")
    if context_snippets:
        questions.append("Do nearby tests exercise sibling branches, or do they only mirror the newly changed implementation?")
    if "data_presence" in dimension_names:
        questions.append("If emptiness/history now matters on this path, where is that data computed and does the patch introduce a new fetch or error path?")
    return questions[:6]


def build_review_prepare_payload(
    artifact_text: str,
    *,
    requirements_text: str,
    review_context_text: str = "",
    route_input: dict[str, Any] | None = None,
    route_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    changed_files = _extract_changed_files(artifact_text)
    clauses = _extract_requirement_clauses(requirements_text)
    identifiers = _extract_identifiers(requirements_text + "\n" + artifact_text)
    state_terms = _extract_state_terms(requirements_text + "\n" + artifact_text)
    diff_conditionals = _extract_diff_conditionals(artifact_text)
    context_snippets = _extract_context_snippets(review_context_text, identifiers, changed_files)
    dimensions = _build_dimensions(identifiers, state_terms, clauses)
    cases = _build_cases(dimensions, [clause["id"] for clause in clauses if clause.get("conditional")])
    invariants = _build_invariants(identifiers, dimensions, clauses, context_snippets)
    questions = _build_questions(dimensions, diff_conditionals, context_snippets)

    payload = {
        "contract_version": "ccr.review_prepare.v1",
        "summary": {
            "changed_file_count": len(changed_files),
            "requirement_clause_count": len(clauses),
            "conditional_clause_count": sum(1 for clause in clauses if clause.get("conditional")),
            "dimension_count": len(dimensions),
            "case_count": len(cases),
            "question_count": len(questions),
        },
        "requirements": {
            "has_requirements": bool(requirements_text.strip()),
            "clauses": clauses,
        },
        "changed": {
            "files": changed_files,
            "symbols": identifiers,
            "state_terms": state_terms,
            "conditionals": diff_conditionals,
        },
        "related_context": {
            "snippets": context_snippets,
        },
        "scenario_matrix": {
            "dimensions": dimensions,
            "cases": cases,
        },
        "invariants": invariants,
        "questions_to_verify": questions,
        "route_context": {
            "triggered_personas": (route_input or {}).get("triggered_personas") if isinstance(route_input, dict) else None,
            "highest_risk_personas": (route_input or {}).get("highest_risk_personas") if isinstance(route_input, dict) else None,
            "review_plan_summary": (route_plan or {}).get("summary") if isinstance(route_plan, dict) else None,
        },
    }
    payload["summary_text"] = (
        f"Prepared {payload['summary']['requirement_clause_count']} requirement clauses, "
        f"{payload['summary']['dimension_count']} scenario dimensions, and "
        f"{payload['summary']['case_count']} scenario cases for downstream reviewers."
    )
    return payload


def build_review_prepare_artifact(
    artifact_file: Path,
    *,
    requirements_file: Path,
    review_context_file: Path | None,
    output_file: Path,
    route_input_file: Path | None = None,
    route_plan_file: Path | None = None,
) -> dict[str, Any]:
    artifact_text = read_text(artifact_file)
    requirements_text = read_text(requirements_file) if requirements_file.is_file() else ""
    review_context_text = read_text(review_context_file) if review_context_file and review_context_file.is_file() else ""
    route_input = load_json_file(route_input_file, default={}) if route_input_file and route_input_file.is_file() else {}
    route_plan = load_json_file(route_plan_file, default={}) if route_plan_file and route_plan_file.is_file() else {}

    payload = build_review_prepare_payload(
        artifact_text,
        requirements_text=requirements_text,
        review_context_text=review_context_text,
        route_input=route_input if isinstance(route_input, dict) else {},
        route_plan=route_plan if isinstance(route_plan, dict) else {},
    )
    write_json(output_file, payload)
    return payload


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ccr-review-prepare",
        description="Build a deterministic pre-review context artifact for CCR.",
    )
    parser.add_argument("--artifact-file", required=True, help="Path to the review diff/artifact file.")
    parser.add_argument("--requirements-file", required=True, help="Path to requirements/spec text.")
    parser.add_argument("--review-context-file", default=None, help="Optional review context markdown file.")
    parser.add_argument("--route-input-file", default=None, help="Optional route_input.json file.")
    parser.add_argument("--route-plan-file", default=None, help="Optional route_plan.json file.")
    parser.add_argument("--output-file", required=True, help="Path to write review_prepare.json.")
    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    payload = build_review_prepare_artifact(
        Path(args.artifact_file).expanduser().resolve(),
        requirements_file=Path(args.requirements_file).expanduser().resolve(),
        review_context_file=Path(args.review_context_file).expanduser().resolve() if args.review_context_file else None,
        route_input_file=Path(args.route_input_file).expanduser().resolve() if args.route_input_file else None,
        route_plan_file=Path(args.route_plan_file).expanduser().resolve() if args.route_plan_file else None,
        output_file=Path(args.output_file).expanduser().resolve(),
    )
    print(json.dumps(payload, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
