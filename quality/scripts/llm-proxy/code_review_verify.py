#!/usr/bin/env python3
"""code_review_verify — Specialized wrapper over llm-proxy for CCR verification.

Bakes in the code review verification prompt and schema so CCR verifier tasks become
more consistent and easier to evaluate.

Usage:
    python3 code_review_verify.py --input-file PATH [--provider codex|gemini]
                                  [--output-file PATH] [--dry-run]

Default verifier provider: codex.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import argparse
import json

from llm_proxy import build_llm_invocation, run_proxy


_HERE = os.path.dirname(os.path.abspath(__file__))
PROMPT_TEMPLATE_PATH = os.path.join(_HERE, "prompts", "review_verify.txt")
SCHEMA_PATH = os.path.join(_HERE, "schemas", "code_review_verification_response.schema.json")
PROVIDERS = ("codex", "gemini", "claude")
DEFAULT_PROVIDER = "codex"


def _load_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _load_input_payload(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: Cannot read verification input file {path}: {exc}", file=sys.stderr)
        sys.exit(1)


def _sanitize_input_payload(payload: dict) -> dict:
    """Drop eval-only metadata before sending the batch to the LLM."""
    return {
        "file": payload.get("file", ""),
        "diff_hunk": payload.get("diff_hunk", ""),
        "file_context": payload.get("file_context", ""),
        "requirements": payload.get("requirements", ""),
        "candidates": payload.get("candidates", []),
    }


def _build_prompt(payload: dict) -> str:
    template = _load_text(PROMPT_TEMPLATE_PATH)
    batch_json = json.dumps(_sanitize_input_payload(payload), indent=2, ensure_ascii=False)
    return template.replace("{verification_batch_json}", batch_json)


def _parse_llm_response(raw_response: str) -> dict:
    stripped = raw_response.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

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

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(stripped[start : end + 1])
        except json.JSONDecodeError:
            pass

    return {
        "verified_findings": [],
        "summary": "Verification response could not be parsed as JSON.",
    }


def _build_verification_output(
    *,
    verified_findings: list[dict],
    summary: str,
    raw_response: str,
    llm_invocation: dict | None = None,
) -> dict:
    payload = {
        "contract_version": "ccr.verification_result.v1",
        "verified_findings": verified_findings,
        "summary": summary,
        "raw_response": raw_response,
    }
    if llm_invocation is not None:
        payload["llm_invocation"] = llm_invocation
    return payload



def _dry_run_result(payload: dict, provider: str) -> dict:
    verified_findings = []
    for candidate in payload.get("candidates", []):
        message = candidate.get("message", "[dry-run] No message provided.")
        verified_findings.append(
            {
                "candidate_id": candidate.get("candidate_id", "unknown"),
                "verdict": "uncertain",
                "file": candidate.get("file") or payload.get("file", "unknown"),
                "line": candidate.get("line", 1),
                "revised_message": message,
                "title": str(message).strip().rstrip("."),
                "problem": str(message),
                "impact": "[dry-run] User-visible impact was not verified.",
                "suggested_fixes": ["[dry-run] Add a concrete fix recommendation during live verification."],
                "evidence": f"[dry-run] Verification skipped. Provider would be '{provider}'.",
            }
        )
    return _build_verification_output(
        verified_findings=verified_findings,
        summary="[dry-run] Verification skipped.",
        raw_response="[dry-run]",
        llm_invocation=build_llm_invocation({"provider": provider}, provider=provider),
    )



def _result_from_proxy_result(proxy_result: dict, *, provider: str) -> dict:
    raw_response = str(proxy_result.get("response") or "")
    llm_invocation = build_llm_invocation(proxy_result, provider=provider)
    exit_code = proxy_result.get("exit_code", 1)
    if exit_code != 0 or proxy_result.get("error"):
        error_msg = proxy_result.get("error") or "LLM provider returned non-zero exit code."
        return _build_verification_output(
            verified_findings=[],
            summary=f"Verification failed: {error_msg}",
            raw_response=raw_response,
            llm_invocation=llm_invocation,
        )

    parsed = _parse_llm_response(raw_response)
    verified_findings = parsed.get("verified_findings") if isinstance(parsed.get("verified_findings"), list) else []
    summary = parsed.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        summary = "Verification completed."
    return _build_verification_output(
        verified_findings=verified_findings,
        summary=summary,
        raw_response=raw_response,
        llm_invocation=llm_invocation,
    )


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="code-review-verify",
        description="Specialized code review finding verifier using llm-proxy.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python3 code_review_verify.py --input-file /tmp/batch.json --provider codex
    python3 code_review_verify.py --input-file /tmp/batch.json --provider gemini --output-file /tmp/verify.json
    python3 code_review_verify.py --input-file /tmp/batch.json --dry-run
""",
    )
    parser.add_argument("--input-file", required=True, help="Path to the verifier batch JSON file.")
    parser.add_argument("--provider", default=DEFAULT_PROVIDER, choices=list(PROVIDERS), help=f"LLM provider to use for verification (default: {DEFAULT_PROVIDER}).")
    parser.add_argument("--output-file", default=None, help="Optional path to write the structured JSON output.")
    parser.add_argument("--dry-run", action="store_true", help="Return mock structured output without calling the LLM provider.")
    return parser


def _write_output(path: str | None, payload: dict) -> None:
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
    except OSError as exc:
        print(f"WARNING: Could not write output file: {exc}", file=sys.stderr)


def main() -> None:
    parser = _build_arg_parser()
    args = parser.parse_args()

    payload = _load_input_payload(args.input_file)
    prompt = _build_prompt(payload)

    if args.dry_run:
        result = _dry_run_result(payload, args.provider)
        _write_output(args.output_file, result)
        print(json.dumps(result, indent=2))
        sys.exit(0)

    proxy_result = run_proxy(
        prompt=prompt,
        provider=args.provider,
        dry_run=False,
        response_schema=SCHEMA_PATH,
    )

    exit_code = proxy_result.get("exit_code", 1)
    if exit_code != 0 or proxy_result.get("error"):
        error_msg = proxy_result.get("error") or "LLM provider returned non-zero exit code."
        print(f"ERROR: LLM provider '{args.provider}' failed: {error_msg}", file=sys.stderr)
        result = _result_from_proxy_result(proxy_result, provider=args.provider)
        _write_output(args.output_file, result)
        print(json.dumps(result, indent=2))
        sys.exit(1)

    result = _result_from_proxy_result(proxy_result, provider=args.provider)
    _write_output(args.output_file, result)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
