#!/usr/bin/env python3
"""
sisyphus-research-verify — Specialized wrapper over llm-proxy for research findings verification.

Bakes in research verification prompt and criteria. Validates output against
the research_verify_response schema and produces structured JSON.

Usage:
    python3 sisyphus_research_verify.py --findings-file PATH [--provider codex|gemini]
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

PROMPT_TEMPLATE_PATH = os.path.join(_HERE, "prompts", "research_verify.txt")
SCHEMA_PATH = os.path.join(_HERE, "schemas", "research_verify_response.schema.json")

PROVIDERS = ("codex", "gemini")

# Dry-run mock verdict values: VERIFIED | ISSUES_FOUND
_DRY_RUN_VERDICT = "VERIFIED"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_prompt_template() -> str:
    """Load the baked-in verification prompt template."""
    try:
        with open(PROMPT_TEMPLATE_PATH) as f:
            return f.read()
    except OSError as exc:
        print(
            "ERROR: Cannot read prompt template {}: {}".format(PROMPT_TEMPLATE_PATH, exc),
            file=sys.stderr,
        )
        sys.exit(1)


def _read_findings_file(path: str) -> str:
    """Read the research findings file content."""
    try:
        with open(path) as f:
            return f.read()
    except OSError as exc:
        print(
            "ERROR: Cannot read findings file {}: {}".format(path, exc),
            file=sys.stderr,
        )
        sys.exit(1)


def _build_prompt(findings_content: str) -> str:
    """Substitute findings content into the prompt template."""
    template = _load_prompt_template()
    return template.replace("{findings_content}", findings_content)


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
        "verdict": "ISSUES_FOUND",
        "findings": [
            {
                "claim": "LLM response parsing",
                "status": "unverifiable",
                "detail": "LLM response could not be parsed as JSON: {}".format(
                    raw_response[:200]
                ),
            }
        ],
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sisyphus-research-verify",
        description="Specialized research findings verifier using llm-proxy.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python3 sisyphus_research_verify.py --findings-file /tmp/research.md
    python3 sisyphus_research_verify.py --findings-file research.md --provider gemini --dry-run
    python3 sisyphus_research_verify.py --findings-file research.md --output-file /tmp/verify.json
""",
    )
    parser.add_argument(
        "--findings-file",
        required=True,
        dest="findings_file",
        metavar="PATH",
        help="Path to the research findings markdown file to verify.",
    )
    parser.add_argument(
        "--provider",
        default="codex",
        choices=list(PROVIDERS),
        help="LLM provider to use for verification (default: codex).",
    )
    parser.add_argument(
        "--output-file",
        default=None,
        dest="output_file",
        metavar="PATH",
        help="Optional path to write the structured JSON verification output.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Return mock VERIFIED output without calling the LLM provider.",
    )
    return parser


def main() -> None:
    parser = _build_arg_parser()
    args = parser.parse_args()

    # Read findings content
    findings_content = _read_findings_file(args.findings_file)

    # Build full verification prompt
    prompt = _build_prompt(findings_content)

    if args.dry_run:
        # Return a structured dry-run response without calling the LLM
        result = {
            "verdict": _DRY_RUN_VERDICT,
            "findings": [
                {
                    "claim": "Research findings document",
                    "status": "verified",
                    "detail": "[dry-run] Skipped — no LLM call made. Provider would be '{}'.".format(
                        args.provider
                    ),
                }
            ],
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
    # Retry logic is built into llm_proxy.run_proxy() via MAX_SCHEMA_RETRIES — do not add here
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
        print(
            "ERROR: LLM provider '{}' failed: {}".format(args.provider, error_msg),
            file=sys.stderr,
        )
        result = {
            "verdict": "ISSUES_FOUND",
            "findings": [
                {
                    "claim": "LLM provider invocation",
                    "status": "unverifiable",
                    "detail": "LLM call failed: {}".format(error_msg),
                }
            ],
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
        sys.exit(1)

    parsed = _parse_llm_response(raw_response)
    result = {
        "verdict": parsed.get("verdict", "ISSUES_FOUND"),
        "findings": parsed.get("findings", []),
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
    sys.exit(0 if result["verdict"] == "VERIFIED" else 1)


if __name__ == "__main__":
    main()
