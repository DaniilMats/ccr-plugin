#!/usr/bin/env python3
"""
llm-proxy — Unified CLI interface for Codex and Gemini LLMs.

Usage:
    python3 llm_proxy.py --provider codex --prompt "Review this code" [options]

Callable API:
    from llm_proxy import run_proxy
    result = run_proxy(prompt="...", provider="codex")
"""
from __future__ import annotations

# Import resolution: works regardless of CWD
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import argparse
import json
import math
import time
from typing import Optional

from adapters.base import ProxyResponse
from adapters.codex import CodexAdapter
from adapters.gemini import GeminiAdapter
from validator import validate_response

# ── Constants ───────────────────────────────────────────────────────────────

PROVIDERS = ("codex", "gemini")
MAX_SCHEMA_RETRIES = 2  # Up to 3 total attempts (1 original + 2 retries)

SCHEMA_RETRY_PROMPT_TEMPLATE = (
    "Your previous response did not match the required schema.\n"
    "Violations:\n{violations}\n\n"
    "Please respond with valid JSON matching this schema:\n{schema}\n\n"
    "Original request:\n{original_prompt}"
)


# ── Core run_proxy() API ─────────────────────────────────────────────────────

def run_proxy(
    prompt: str,
    provider: str,
    scope: Optional[str] = None,
    thread_id: Optional[str] = None,
    timeout: int = 300,
    dry_run: bool = False,
    response_schema: Optional[str] = None,
    output_file: Optional[str] = None,
) -> dict:
    """
    Execute an LLM provider with the given prompt and return the result as a dict.

    Args:
        prompt: The prompt text to send to the LLM.
        provider: One of "codex" or "gemini".
        scope: Optional review scope (commit:SHA, branch:BASE, uncommitted, file:PATH).
        thread_id: Optional thread/session ID to resume.
        timeout: Total timeout in seconds across the initial call plus schema retries.
        dry_run: If True, return a mock response without calling the provider.
        response_schema: Path to a JSON Schema file for response validation.
        output_file: Optional path to write JSON output to.

    Returns:
        dict with keys: response, thread_id, tokens, duration_ms, exit_code,
                        error, timed_out, schema_valid, schema_retries.
    """
    started_at = time.monotonic()

    def _elapsed_ms() -> int:
        return int((time.monotonic() - started_at) * 1000)

    def _timeout_result(current_thread_id: Optional[str]) -> ProxyResponse:
        return ProxyResponse(
            response="",
            thread_id=current_thread_id,
            tokens=0,
            duration_ms=_elapsed_ms(),
            exit_code=-1,
            error="Timed out after {} seconds".format(timeout),
            timed_out=True,
        )

    if provider not in PROVIDERS:
        result = ProxyResponse(
            exit_code=1,
            error="Unknown provider '{}'. Must be one of: {}".format(provider, ", ".join(PROVIDERS)),
            duration_ms=_elapsed_ms(),
        )
        return result.to_dict()

    if dry_run:
        result = ProxyResponse(
            response="[dry-run] Would call provider '{}' with prompt: {}".format(
                provider, prompt[:100]
            ),
            thread_id=thread_id,
            tokens=0,
            duration_ms=0,
            exit_code=0,
            error=None,
            timed_out=False,
            schema_valid=True,
            schema_retries=0,
        )
        out = result.to_dict()
        _maybe_write_output(out, output_file)
        return out

    # Build adapter
    adapter = _build_adapter(provider)

    # Load schema once if provided
    schema_text: Optional[str] = None
    if response_schema:
        try:
            with open(response_schema) as f:
                schema_text = f.read()
        except OSError as exc:
            result = ProxyResponse(
                exit_code=1,
                error="Cannot read schema file: {}".format(exc),
                duration_ms=_elapsed_ms(),
            )
            out = result.to_dict()
            _maybe_write_output(out, output_file)
            return out

    deadline = started_at + timeout

    # Run with optional schema-retry loop
    current_prompt = prompt
    schema_retries = 0
    last_response: Optional[ProxyResponse] = None

    for attempt in range(MAX_SCHEMA_RETRIES + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            last_response = _timeout_result(thread_id)
            break

        remaining_timeout = max(1, int(math.ceil(remaining)))
        last_response = adapter.run(
            prompt=current_prompt,
            scope=scope if attempt == 0 else None,  # scope only on first call
            thread_id=thread_id,
            timeout=remaining_timeout,
        )

        # Propagate thread_id from first successful call
        if last_response.thread_id:
            thread_id = last_response.thread_id

        # If call itself failed (exit_code != 0 or error), don't retry schema
        if last_response.exit_code != 0 or last_response.timed_out:
            break

        # If no schema validation requested, we're done
        if not response_schema:
            break

        # Validate response against schema
        is_valid, violations = validate_response(last_response.response, response_schema)
        last_response.schema_valid = is_valid
        last_response.schema_violations = violations

        if is_valid or attempt == MAX_SCHEMA_RETRIES:
            break

        # Retry with schema guidance, but stay inside the original timeout budget.
        schema_retries += 1
        current_prompt = SCHEMA_RETRY_PROMPT_TEMPLATE.format(
            violations="\n".join("- " + v for v in violations),
            schema=schema_text or "",
            original_prompt=prompt,
        )

    if last_response is None:
        last_response = ProxyResponse(exit_code=1, error="No response generated", duration_ms=_elapsed_ms())

    last_response.schema_retries = schema_retries
    last_response.duration_ms = _elapsed_ms()
    out = last_response.to_dict()
    _maybe_write_output(out, output_file)
    return out


# ── Helpers ──────────────────────────────────────────────────────────────────

def _build_adapter(provider: str):
    """Instantiate the correct adapter for the given provider."""
    if provider == "codex":
        return CodexAdapter()
    elif provider == "gemini":
        return GeminiAdapter()
    raise ValueError("Unknown provider: {}".format(provider))


def _maybe_write_output(data: dict, output_file: Optional[str]) -> None:
    """Write JSON output to a file if specified."""
    if output_file:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(output_file)), exist_ok=True)
            with open(output_file, "w") as f:
                json.dump(data, f, indent=2)
        except OSError as exc:
            # Can't write output file — log to stderr but don't fail
            print("WARNING: Could not write output file: {}".format(exc), file=sys.stderr)


# ── CLI ───────────────────────────────────────────────────────────────────────

def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="llm-proxy",
        description="Unified CLI interface for Codex and Gemini LLMs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--provider",
        required=True,
        choices=list(PROVIDERS),
        help="LLM provider to use.",
    )
    parser.add_argument(
        "--prompt",
        required=True,
        help="Prompt text to send to the LLM.",
    )
    parser.add_argument(
        "--scope",
        default=None,
        help=(
            "Review scope context. One of: commit:SHA, branch:BASE, "
            "uncommitted, file:PATH."
        ),
    )
    parser.add_argument(
        "--thread-id",
        default=None,
        dest="thread_id",
        help="Thread/session ID to resume a previous conversation.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Timeout in seconds (default: 300).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Return mock output without calling the provider.",
    )
    parser.add_argument(
        "--output-file",
        default=None,
        dest="output_file",
        help="Optional path to write JSON output to (in addition to stdout).",
    )
    parser.add_argument(
        "--response-schema",
        default=None,
        dest="response_schema",
        help=(
            "Path to a JSON Schema file. When provided, the LLM response is "
            "validated against the schema and re-prompted on failure (up to 2 retries)."
        ),
    )
    return parser


def main() -> None:
    parser = _build_arg_parser()
    args = parser.parse_args()

    result = run_proxy(
        prompt=args.prompt,
        provider=args.provider,
        scope=args.scope,
        thread_id=args.thread_id,
        timeout=args.timeout,
        dry_run=args.dry_run,
        response_schema=args.response_schema,
        output_file=args.output_file,
    )

    print(json.dumps(result, indent=2))

    # Exit with the provider's exit code (0 on success)
    sys.exit(result.get("exit_code", 0))


if __name__ == "__main__":
    main()
