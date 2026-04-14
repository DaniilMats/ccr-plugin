#!/usr/bin/env python3
"""
sisyphus-plan-review — Specialized wrapper over llm-proxy for Prometheus plan reviews.

Bakes in plan quality gate prompt and review criteria. Validates output against
the plan_review_response schema and produces structured JSON.

Usage:
    python3 sisyphus_plan_review.py --plan-file PATH [--provider codex|gemini]
                                    [--output-file PATH] [--dry-run]
"""
from __future__ import annotations

# Import resolution: works regardless of CWD
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import argparse
import json

from llm_proxy import run_proxy

# ── Constants ─────────────────────────────────────────────────────────────────

_HERE = os.path.dirname(os.path.abspath(__file__))

PROMPT_TEMPLATE_PATH = os.path.join(_HERE, "prompts", "plan_review.txt")
SCHEMA_PATH = os.path.join(_HERE, "schemas", "plan_review_response.schema.json")

PROVIDERS = ("codex", "gemini")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_prompt_template() -> str:
    """Load the baked-in review prompt template."""
    try:
        with open(PROMPT_TEMPLATE_PATH) as f:
            return f.read()
    except OSError as exc:
        print(
            "ERROR: Cannot read prompt template {}: {}".format(PROMPT_TEMPLATE_PATH, exc),
            file=sys.stderr,
        )
        sys.exit(1)


def _read_plan_file(path: str) -> str:
    """Read the plan file content."""
    try:
        with open(path) as f:
            return f.read()
    except OSError as exc:
        print("ERROR: Cannot read plan file {}: {}".format(path, exc), file=sys.stderr)
        sys.exit(1)


def _build_prompt(plan_content: str) -> str:
    """Substitute plan content into the prompt template."""
    template = _load_prompt_template()
    return template.replace("{plan_content}", plan_content)


def _parse_llm_response(raw_response: str) -> dict:
    """
    Extract JSON from the LLM response.

    The LLM should return pure JSON, but may include markdown fences or prose.
    Try progressively looser parsing strategies.
    """
    # Strategy 1: direct parse
    stripped = raw_response.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    # Strategy 2: strip markdown fences
    if "```" in stripped:
        lines = stripped.splitlines()
        inner = []
        in_block = False
        for line in lines:
            if line.startswith("```"):
                in_block = not in_block
                continue
            if in_block:
                inner.append(line)
        try:
            return json.loads("\n".join(inner))
        except json.JSONDecodeError:
            pass

    # Strategy 3: find first { ... } block
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(stripped[start : end + 1])
        except json.JSONDecodeError:
            pass

    # Could not parse — return error structure
    return {
        "verdict": "REJECT",
        "findings": [
            {
                "criterion": "CORRECTNESS",
                "status": "FAIL",
                "detail": "LLM response could not be parsed as JSON.",
            }
        ],
        "summary": "Parse failure: {}".format(raw_response[:200]),
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sisyphus-plan-review",
        description="Specialized plan quality gate reviewer using llm-proxy.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python3 sisyphus_plan_review.py --plan-file .sisyphus-claude/plans/my-plan.md
    python3 sisyphus_plan_review.py --plan-file plan.md --provider gemini --dry-run
    python3 sisyphus_plan_review.py --plan-file plan.md --output-file /tmp/review.json
""",
    )
    parser.add_argument(
        "--plan-file",
        required=True,
        dest="plan_file",
        metavar="PATH",
        help="Path to the plan markdown file to review.",
    )
    parser.add_argument(
        "--provider",
        default="codex",
        choices=list(PROVIDERS),
        help="LLM provider to use for the review (default: codex).",
    )
    parser.add_argument(
        "--output-file",
        default=None,
        dest="output_file",
        metavar="PATH",
        help="Optional path to write the structured JSON review output.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Return mock output without calling the LLM provider.",
    )
    return parser


def main() -> None:
    parser = _build_arg_parser()
    args = parser.parse_args()

    # Read plan content
    plan_content = _read_plan_file(args.plan_file)

    # Build full review prompt
    prompt = _build_prompt(plan_content)

    if args.dry_run:
        # Return a structured dry-run response without calling the LLM
        result = {
            "verdict": "OKAY",
            "findings": [
                {
                    "criterion": "COMPLETENESS",
                    "status": "PASS",
                    "detail": "[dry-run] Skipped — no LLM call made.",
                },
                {
                    "criterion": "CORRECTNESS",
                    "status": "PASS",
                    "detail": "[dry-run] Skipped — no LLM call made.",
                },
                {
                    "criterion": "VERIFIABILITY",
                    "status": "PASS",
                    "detail": "[dry-run] Skipped — no LLM call made.",
                },
                {
                    "criterion": "ORDERING",
                    "status": "PASS",
                    "detail": "[dry-run] Skipped — no LLM call made.",
                },
                {
                    "criterion": "RISK",
                    "status": "PASS",
                    "detail": "[dry-run] Skipped — no LLM call made.",
                },
            ],
            "summary": "[dry-run] Would call provider '{}' with plan: {}".format(
                args.provider, args.plan_file
            ),
            "raw_response": "[dry-run]",
        }

        if args.output_file:
            try:
                os.makedirs(os.path.dirname(os.path.abspath(args.output_file)), exist_ok=True)
                with open(args.output_file, "w") as f:
                    json.dump(result, f, indent=2)
            except OSError as exc:
                print("WARNING: Could not write output file: {}".format(exc), file=sys.stderr)

        print(json.dumps(result, indent=2))
        sys.exit(0)

    # Invoke llm-proxy via Python import (not subprocess)
    proxy_result = run_proxy(
        prompt=prompt,
        provider=args.provider,
        dry_run=False,
        response_schema=SCHEMA_PATH,
    )

    raw_response = proxy_result.get("response", "")
    exit_code = proxy_result.get("exit_code", 1)

    if exit_code != 0 or proxy_result.get("error"):
        error_msg = proxy_result.get("error") or "LLM provider returned non-zero exit code."
        result = {
            "verdict": "REJECT",
            "findings": [
                {
                    "criterion": "CORRECTNESS",
                    "status": "FAIL",
                    "detail": "LLM call failed: {}".format(error_msg),
                }
            ],
            "summary": "Review could not be completed due to LLM provider error.",
            "raw_response": raw_response,
        }
    else:
        parsed = _parse_llm_response(raw_response)
        result = {
            "verdict": parsed.get("verdict", "REJECT"),
            "findings": parsed.get("findings", []),
            "summary": parsed.get("summary", ""),
            "raw_response": raw_response,
        }

    if args.output_file:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(args.output_file)), exist_ok=True)
            with open(args.output_file, "w") as f:
                json.dump(result, f, indent=2)
        except OSError as exc:
            print("WARNING: Could not write output file: {}".format(exc), file=sys.stderr)

    print(json.dumps(result, indent=2))
    sys.exit(0 if result["verdict"] == "OKAY" else 1)


if __name__ == "__main__":
    main()
