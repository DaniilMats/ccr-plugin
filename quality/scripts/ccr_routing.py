#!/usr/bin/env python3
"""Shared adaptive fanout routing helper for CCR.

This module is the source of truth for CCR reviewer selection so runtime behavior
and eval behavior cannot drift apart.

Examples:
    python3 scripts/ccr_routing.py --input-file /tmp/ccr_route_input.json
    python3 scripts/ccr_routing.py --input-file /tmp/ccr_route_input.json --output-file /tmp/ccr_route_plan.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


Persona = Literal["security", "concurrency", "performance", "requirements"]
PassName = Literal[
    "logic_p1",
    "logic_p2",
    "logic_p3",
    "security_p1",
    "security_p2",
    "security_p3",
    "concurrency_p1",
    "concurrency_p2",
    "concurrency_p3",
    "performance_p1",
    "performance_p2",
    "performance_p3",
    "requirements_p1",
    "requirements_p2",
]

SPECIALTY_ORDER: tuple[Persona, ...] = (
    "security",
    "concurrency",
    "performance",
    "requirements",
)
GENERIC_COVERAGE_ORDER: tuple[Persona, ...] = (
    "security",
    "performance",
    "requirements",
    "concurrency",
)
DEFAULT_RISK_PRIORITY: tuple[Persona, ...] = (
    "security",
    "concurrency",
    "performance",
    "requirements",
)
DISPLAY_LABELS = {
    "logic": "Logic",
    "security": "Security",
    "concurrency": "Concurrency",
    "performance": "Performance",
    "requirements": "Requirements",
}
FULL_CODE_MATRIX: list[PassName] = [
    "logic_p1",
    "logic_p2",
    "logic_p3",
    "security_p1",
    "security_p2",
    "security_p3",
    "concurrency_p1",
    "concurrency_p2",
    "concurrency_p3",
    "performance_p1",
    "performance_p2",
    "performance_p3",
]
FULL_REQUIREMENTS_MATRIX: list[PassName] = [
    "requirements_p1",
    "requirements_p2",
]


class RoutingInput(BaseModel):
    """Runtime input for CCR adaptive fanout planning."""

    model_config = ConfigDict(extra="ignore")

    changed_files: list[str] = Field(default_factory=list)
    changed_file_count: int | None = None
    changed_lines: int = 0
    has_requirements: bool = False
    requirements_from_mr_description: bool = False
    user_requested_exhaustive: bool = False
    behavior_change_ambiguous: bool = False
    triggered_personas: list[Persona] = Field(default_factory=list)
    highest_risk_personas: list[Persona] = Field(default_factory=list)
    critical_surfaces: list[str] = Field(default_factory=list)

    def effective_changed_file_count(self) -> int:
        if self.changed_file_count is not None:
            return self.changed_file_count
        return len(self.changed_files)

    def has_spec_text(self) -> bool:
        return self.has_requirements or self.requirements_from_mr_description


class RoutingPlan(BaseModel):
    passes: list[PassName]
    total_passes: int
    full_matrix: bool
    pass_counts: dict[str, int]
    reasons: list[str]
    summary: str


def _pass_name(persona: str, number: int) -> PassName:
    return f"{persona}_p{number}"  # type: ignore[return-value]


def _add_pass(passes: list[PassName], pass_name: PassName) -> None:
    if pass_name not in passes:
        passes.append(pass_name)


def _full_matrix(request: RoutingInput) -> list[PassName]:
    passes = list(FULL_CODE_MATRIX)
    if request.has_spec_text():
        passes.extend(FULL_REQUIREMENTS_MATRIX)
    return passes


def _count_passes(passes: list[PassName]) -> dict[str, int]:
    counts = {
        "logic": 0,
        "security": 0,
        "concurrency": 0,
        "performance": 0,
        "requirements": 0,
    }
    for pass_name in passes:
        persona = pass_name.split("_", 1)[0]
        counts[persona] += 1
    return counts


def _format_pass_summary(pass_counts: dict[str, int]) -> str:
    parts = []
    for persona in ("logic", "security", "concurrency", "performance", "requirements"):
        count = pass_counts.get(persona, 0)
        if count:
            parts.append(f"{DISPLAY_LABELS[persona]} x{count}")
    return ", ".join(parts)


def _routing_tier(request: RoutingInput, *, full_matrix: bool, total_passes: int) -> str:
    if request.user_requested_exhaustive:
        return "exhaustive MR"
    if full_matrix:
        return "high-risk MR"
    if total_passes <= 4:
        return "small MR"
    return "medium-risk MR"


def explain_full_matrix_reasons(request: RoutingInput) -> list[str]:
    reasons = []
    if len(set(request.triggered_personas)) >= 3:
        reasons.append("3+ specialty personas are triggered")
    if request.changed_lines >= 400:
        reasons.append(f"large diff ({request.changed_lines} changed lines)")
    file_count = request.effective_changed_file_count()
    if file_count > 8:
        reasons.append(f"wide diff ({file_count} changed files)")
    if request.critical_surfaces:
        reasons.append("critical surfaces touched: " + ", ".join(request.critical_surfaces))
    if request.behavior_change_ambiguous:
        reasons.append("behavior-changing requirements are ambiguous")
    if request.user_requested_exhaustive:
        reasons.append("user requested an exhaustive review")
    return reasons


def should_escalate_full_matrix(request: RoutingInput) -> bool:
    return bool(explain_full_matrix_reasons(request))


def plan_review_passes(request: RoutingInput) -> list[PassName]:
    """Return the reviewer pass set for a CCR routing request."""
    if should_escalate_full_matrix(request):
        return _full_matrix(request)

    # Baseline: Logic x3 across Gemini + Codex + Claude Opus for max diversity
    # on the core persona. Specialty personas only get Pass 3 via full matrix.
    passes: list[PassName] = ["logic_p1", "logic_p2", "logic_p3"]
    triggered = set(request.triggered_personas)

    for persona in SPECIALTY_ORDER:
        if persona in triggered:
            _add_pass(passes, _pass_name(persona, 1))

    if len(passes) < 4:
        for persona in GENERIC_COVERAGE_ORDER:
            if persona == "requirements" and not request.has_spec_text():
                continue
            _add_pass(passes, _pass_name(persona, 1))
            if len(passes) >= 4:
                break

    risk_priority = [
        persona
        for persona in (request.highest_risk_personas or list(DEFAULT_RISK_PRIORITY))
        if persona in triggered
    ]
    for persona in risk_priority[:2]:
        _add_pass(passes, _pass_name(persona, 2))

    return passes


def build_routing_plan(request: RoutingInput) -> RoutingPlan:
    passes = plan_review_passes(request)
    pass_counts = _count_passes(passes)
    full_matrix = should_escalate_full_matrix(request)

    reasons = ["always include Logic x3 baseline (Gemini + Codex + Claude)"]
    if full_matrix:
        reasons.extend(explain_full_matrix_reasons(request))
        if request.has_spec_text():
            reasons.append("requirements/spec text exists, so the full matrix includes Requirements x2 (14 total)")
        else:
            reasons.append("no requirements/spec text, so the full matrix stays at the 12-pass code matrix")
    else:
        triggered = [persona for persona in SPECIALTY_ORDER if persona in set(request.triggered_personas)]
        if triggered:
            reasons.append("triggered specialty personas: " + ", ".join(triggered))
        generic_added = [
            persona
            for persona in GENERIC_COVERAGE_ORDER
            if pass_counts[persona] > 0 and persona not in set(request.triggered_personas)
        ]
        if generic_added:
            reasons.append("generic coverage filler added: " + ", ".join(generic_added))
        duplicated = [persona for persona in SPECIALTY_ORDER if pass_counts[persona] == 2]
        if duplicated:
            reasons.append("highest-risk personas duplicated on pass 2: " + ", ".join(duplicated))

    tier = _routing_tier(request, full_matrix=full_matrix, total_passes=len(passes))
    summary = f"Review plan: {tier} → {_format_pass_summary(pass_counts)}"

    return RoutingPlan(
        passes=passes,
        total_passes=len(passes),
        full_matrix=full_matrix,
        pass_counts=pass_counts,
        reasons=reasons,
        summary=summary,
    )


def _load_input(path: str | Path) -> RoutingInput:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return RoutingInput.model_validate(data)


def _write_output(path: str | None, payload: dict) -> None:
    if not path:
        return
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ccr-routing",
        description="Shared adaptive fanout routing helper for CCR.",
    )
    parser.add_argument("--input-file", required=True, help="Path to routing input JSON")
    parser.add_argument("--output-file", default=None, help="Optional path to write routing plan JSON")
    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    request = _load_input(args.input_file)
    plan = build_routing_plan(request)
    payload = plan.model_dump()
    _write_output(args.output_file, payload)
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
