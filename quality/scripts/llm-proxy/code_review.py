#!/usr/bin/env python3
"""
code_review — Specialized Go code review wrapper over llm-proxy.

Generates a git diff based on --scope, constructs a review prompt with baked-in
Go code quality criteria, and invokes llm-proxy for schema-validated JSON output.

Usage:
    python3 code_review.py --scope uncommitted --provider codex
    python3 code_review.py --scope commit:abc1234 --provider gemini
    python3 code_review.py --scope branch:main --output-file /tmp/review.json
    python3 code_review.py --scope file:internal/service/auth.go --provider codex
    python3 code_review.py --scope package:internal/service --provider codex
    python3 code_review.py --scope package:internal/service --artifact-output /tmp/review_artifact.txt --artifact-only
"""
from __future__ import annotations

# Import resolution: works regardless of CWD
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import argparse
import json
import re
import subprocess
from typing import Optional

from llm_proxy import build_llm_invocation, run_proxy

# ── Constants ────────────────────────────────────────────────────────────────

_HERE = os.path.dirname(os.path.abspath(__file__))

DEFAULT_SCHEMA_PATH = os.path.join(_HERE, "schemas", "code_review_response.schema.json")
DEFAULT_PROMPT_PATH = os.path.join(_HERE, "prompts", "code_review.txt")
DEFAULT_STYLE_GUIDE_PATH = os.path.join(_HERE, "prompts", "go_style_guide.txt")

PROVIDERS = ("codex", "gemini", "claude")
PERSONAS = ("logic", "security", "concurrency", "performance", "requirements")
MAX_PACKAGE_FILES = 12
MAX_PACKAGE_TOTAL_LINES = 3000

# Persona → SA category key mapping.
# Maps persona name to the key in the static_analysis result dict to include.
# None means skip SA for that persona.
_PERSONA_SA_CATEGORY: dict[str, Optional[str]] = {
    "logic": "go_vet",
    "security": "gosec",
    "concurrency": "all",
    "performance": "all",
    "requirements": None,  # SA not applicable for requirements review
}


# ── Diff generation ──────────────────────────────────────────────────────────

def _resolve_scope_review_files(scope: str, project_dir: str) -> tuple[str, list[str]] | None:
    """Resolve file/package scopes to concrete Go files inside project_dir."""
    if scope.startswith("file:"):
        raw_path = scope[len("file:"):].strip()
        if not raw_path:
            raise ValueError("file scope requires a path: file:<PATH>")
        abs_path = os.path.abspath(raw_path)
        if not os.path.isfile(abs_path):
            raise ValueError("file scope path does not exist or is not a file: {}".format(raw_path))
        if not abs_path.endswith(".go"):
            raise ValueError("file scope currently supports only .go files: {}".format(raw_path))
        if os.path.commonpath([project_dir, abs_path]) != project_dir:
            raise ValueError("file scope path must be inside the current project: {}".format(raw_path))
        return "file", [abs_path]

    if scope.startswith("package:"):
        raw_path = scope[len("package:"):].strip()
        if not raw_path:
            raise ValueError("package scope requires a path: package:<PATH>")
        abs_dir = os.path.abspath(raw_path)
        if not os.path.isdir(abs_dir):
            raise ValueError("package scope path does not exist or is not a directory: {}".format(raw_path))
        if os.path.commonpath([project_dir, abs_dir]) != project_dir:
            raise ValueError("package scope path must be inside the current project: {}".format(raw_path))

        files = []
        for name in os.listdir(abs_dir):
            if name.startswith(".") or not name.endswith(".go"):
                continue
            full = os.path.join(abs_dir, name)
            if os.path.isfile(full):
                files.append(full)

        if not files:
            raise ValueError("package scope requires at least one .go file in: {}".format(raw_path))

        files.sort(key=lambda p: (os.path.basename(p).endswith("_test.go"), os.path.basename(p)))
        if len(files) > MAX_PACKAGE_FILES:
            raise ValueError(
                "package scope is too large ({} files > max {}). Narrow the package or raise the limit.".format(
                    len(files), MAX_PACKAGE_FILES
                )
            )

        total_lines = 0
        for path in files:
            with open(path, "r", encoding="utf-8") as f:
                total_lines += len(f.read().splitlines())
        if total_lines > MAX_PACKAGE_TOTAL_LINES:
            raise ValueError(
                "package scope is too large ({} lines > max {}). Narrow the package or raise the limit.".format(
                    total_lines, MAX_PACKAGE_TOTAL_LINES
                )
            )
        return "package", files

    return None


def _render_synthetic_file_diff(abs_path: str, project_dir: str) -> str:
    rel_path = os.path.relpath(abs_path, project_dir)
    with open(abs_path, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()

    diff_lines = [
        "diff --git a/{path} b/{path}".format(path=rel_path),
        "new file mode 100644",
        "index 0000000..1111111",
        "--- /dev/null",
        "+++ b/{path}".format(path=rel_path),
        "@@ -0,0 +1,{count} @@".format(count=len(lines)),
    ]
    diff_lines.extend("+" + line for line in lines)
    return "\n".join(diff_lines)


def _build_synthetic_review_artifact(scope_kind: str, abs_files: list[str], project_dir: str) -> str:
    header = [
        "NOTE: This is a synthetic full-code review artifact.",
        "Treat added lines as the current contents of existing code under review.",
        "Scope: {} review of existing code.".format(scope_kind),
        "",
    ]
    rendered = [_render_synthetic_file_diff(path, project_dir) for path in abs_files]
    return "\n\n".join(header + rendered)


def _scope_changed_files(scope: str, project_dir: str) -> Optional[str]:
    resolved = _resolve_scope_review_files(scope, project_dir)
    if resolved is None:
        return None
    _, abs_files = resolved
    return ",".join(os.path.relpath(path, project_dir) for path in abs_files)


def _generate_diff(scope: str) -> str:
    """
    Generate a review artifact string from the given scope.

    Scope formats:
        uncommitted         — staged + unstaged changes (git diff HEAD)
        commit:SHA          — changes introduced by a single commit (git show SHA)
        branch:BASE         — changes on current branch vs BASE (git diff BASE...HEAD)
        file:PATH           — synthetic full-file diff for an existing Go file
        package:PATH        — synthetic full-package diff for Go files in a directory
    """
    project_dir = os.path.abspath(os.getcwd())
    resolved = _resolve_scope_review_files(scope, project_dir)
    if resolved is not None:
        scope_kind, abs_files = resolved
        return _build_synthetic_review_artifact(scope_kind, abs_files, project_dir)

    if scope == "uncommitted":
        cmd = ["git", "diff", "HEAD"]
    elif scope.startswith("commit:"):
        sha = scope[len("commit:"):]
        if not sha:
            raise ValueError("commit scope requires a SHA: commit:<SHA>")
        cmd = ["git", "show", sha]
    elif scope.startswith("branch:"):
        base = scope[len("branch:"):]
        if not base:
            raise ValueError("branch scope requires a base branch: branch:<BASE>")
        cmd = ["git", "diff", "{}...HEAD".format(base)]
    else:
        raise ValueError(
            "Unknown scope '{}'. Must be one of: uncommitted, commit:<SHA>, branch:<BASE>, file:<PATH>, package:<PATH>".format(scope)
        )

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError("git command failed: {}\n{}".format(" ".join(cmd), result.stderr.strip()))
        diff = result.stdout.strip()
        if not diff:
            return "(no changes)"
        return diff
    except FileNotFoundError:
        raise RuntimeError("git not found — ensure git is installed and on PATH")


# ── Static analysis integration ───────────────────────────────────────────────

def _run_static_analysis_auto(project_dir: str, changed_files: Optional[str]) -> dict:
    """Run static_analysis.run_analysis() on project_dir and return the result dict."""
    try:
        import static_analysis  # noqa: PLC0415 — available via sys.path.insert above
        return static_analysis.run_analysis(project_dir, changed_files=changed_files)
    except ImportError as exc:
        return {"error": "static_analysis module not available: {}".format(exc)}


def _load_static_analysis_json(path: str) -> dict:
    """Load a pre-generated static analysis JSON file."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        return {"error": "Could not load static analysis JSON: {}".format(exc)}


def _format_sa_for_prompt(sa_data: dict, persona: Optional[str]) -> str:
    """
    Convert static analysis data to a human-readable string for embedding in a prompt.

    Filters to the relevant category for the given persona.
    When persona is None, includes all findings.
    """
    if not sa_data or sa_data.get("error"):
        err = sa_data.get("error", "unknown error") if sa_data else "no data"
        return "(static analysis unavailable: {})".format(err)

    # Determine which tool output to use based on persona
    if persona is not None:
        category_key = _PERSONA_SA_CATEGORY.get(persona)
        if category_key is None:
            # Persona explicitly maps to skip (e.g., requirements)
            return ""
    else:
        category_key = "all"

    # Collect findings from the appropriate category
    if category_key == "go_vet":
        findings = sa_data.get("go_vet", [])
    elif category_key == "gosec":
        findings = sa_data.get("gosec", [])
    else:
        # "all" or fallback: merge all tools
        findings = (
            sa_data.get("go_vet", [])
            + sa_data.get("staticcheck", [])
            + sa_data.get("gosec", [])
        )

    if not findings:
        return "(no static analysis findings)"

    lines = ["## Static Analysis Findings\n"]
    for f in findings:
        tool = f.get("tool", "unknown")
        file_ = f.get("file", "?")
        line = f.get("line", "?")
        code = f.get("code", "")
        msg = f.get("message", "")
        code_str = " [{}]".format(code) if code else ""
        lines.append("- {file}:{line} ({tool}){code}: {msg}".format(
            file=file_, line=line, tool=tool, code=code_str, msg=msg
        ))
    return "\n".join(lines)


# ── Prompt construction ───────────────────────────────────────────────────────

_SEMANTIC_GUARDRAIL_PERSONAS = {None, "logic", "requirements"}
_SEMANTIC_REQUIREMENT_CUES = (
    "only if",
    "only when",
    "unless",
    "except",
    "if ",
    "when ",
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
_SEMANTIC_STATE_TERMS = (
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
_SEMANTIC_IDENTIFIER_RE = re.compile(
    r"\b(?:is|has|should|can|allow|omit|hide|show|enable|disable|use|need|needs|require|requires)[A-Z][A-Za-z0-9]*\b"
    r"|\b(?:is|has|should|can|allow|omit|hide|show|enable|disable|use|need|needs|require|requires)_[a-z0-9_]+\b"
    r"|\b[a-z][a-z0-9_]*(?:_enabled|_disabled|_empty|_present|_visible|_hidden)\b"
)


def _load_text(path: str) -> str:
    """Read a text file and return its contents."""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _extract_semantic_requirement_clauses(requirements_text: str, limit: int = 3) -> list[str]:
    clauses: list[str] = []
    for raw_line in requirements_text.splitlines():
        line = " ".join(raw_line.strip().lstrip("-*•").split())
        if not line:
            continue
        lowered = line.lower()
        if any(cue in lowered for cue in _SEMANTIC_REQUIREMENT_CUES):
            clauses.append(line)
            if len(clauses) >= limit:
                break
    return clauses



def _extract_semantic_identifiers(text: str, limit: int = 8) -> list[str]:
    identifiers: list[str] = []
    for match in _SEMANTIC_IDENTIFIER_RE.finditer(text):
        token = match.group(0)
        if token not in identifiers:
            identifiers.append(token)
        if len(identifiers) >= limit:
            break
    return identifiers



def _extract_semantic_state_terms(text: str, limit: int = 8) -> list[str]:
    lowered = text.lower()
    return [term for term in _SEMANTIC_STATE_TERMS if term in lowered][:limit]



def _build_semantic_guardrails(diff: str, requirements_text: str, persona: Optional[str] = None) -> str:
    if persona not in _SEMANTIC_GUARDRAIL_PERSONAS:
        return ""

    stripped_requirements = requirements_text.strip()
    if not stripped_requirements:
        return ""

    clauses = _extract_semantic_requirement_clauses(stripped_requirements)
    identifiers = _extract_semantic_identifiers(stripped_requirements + "\n" + diff)
    state_terms = _extract_semantic_state_terms(stripped_requirements + "\n" + diff)

    lines = ["## Semantic Guardrails", ""]
    if clauses:
        lines.append("Detected conditional requirement clauses:")
        for clause in clauses:
            lines.append(f"- {clause}")
        lines.append("")

    focus_terms: list[str] = []
    if identifiers:
        focus_terms.append("symbols: " + ", ".join(identifiers))
    if state_terms:
        focus_terms.append("states/data: " + ", ".join(state_terms))
    if focus_terms:
        lines.append("Focus terms: " + " · ".join(focus_terms))
        lines.append("")

    lines.extend(
        [
            "Before deciding the change is correct:",
            "- Build the smallest truth table covering the flag(s), data presence, and UI/runtime state mentioned above.",
            "- Compare sibling branches for predicate parity; if one path uses both a flag and a data-presence check while another drops one operand, treat that as suspicious.",
            "- Do not collapse a data-dependent rule into a pure flag toggle (or the reverse) without evidence that the requirement really changed.",
            "- If a fallback/placeholder/loading/untrusted branch now depends on data emptiness, verify where that data comes from and whether the patch adds a new fetch or error path.",
            "- Treat tests added in the same diff as non-independent evidence; they can mirror the same wrong assumption as the implementation.",
            "- If one plausible counterexample remains, report it instead of declaring the behavior correct.",
        ]
    )
    return "\n".join(lines)



def _build_prompt(
    diff: str,
    style_guide_path: str,
    persona: Optional[str] = None,
    static_analysis_text: str = "",
    requirements_text: str = "",
    review_context_text: str = "",
    review_prepare_text: str = "",
) -> str:
    """
    Construct the full review prompt.

    When persona is given, loads prompts/review_{persona}.txt instead of the
    default code_review.txt. All placeholders are filled via str.replace() so
    literal JSON braces in templates do not cause KeyErrors.
    """
    # Choose template path based on persona
    if persona is not None:
        prompt_path = os.path.join(_HERE, "prompts", "review_{}.txt".format(persona))
    else:
        prompt_path = DEFAULT_PROMPT_PATH

    template = _load_text(prompt_path)

    # Embed style guide if available
    try:
        style_guide_text = _load_text(style_guide_path)
        style_guide_section = "## Go Style Guide\n\n" + style_guide_text
    except OSError:
        style_guide_section = ""

    semantic_guardrails_text = _build_semantic_guardrails(
        diff,
        requirements_text=requirements_text,
        persona=persona,
    )

    # Fill all known placeholders using str.replace (safe for JSON-brace-heavy templates)
    result = template.replace("{diff}", diff)
    result = result.replace("{static_analysis}", static_analysis_text)
    result = result.replace("{style_guide_section}", style_guide_section)
    result = result.replace("{requirements}", requirements_text)
    result = result.replace("{review_context}", review_context_text)
    result = result.replace("{review_prepare}", review_prepare_text)
    result = result.replace("{semantic_guardrails}", semantic_guardrails_text)
    return result


# ── Output post-processing ────────────────────────────────────────────────────

def _make_review_output(
    *,
    findings: list[dict],
    summary: str,
    raw_response: str,
    llm_invocation: dict | None = None,
) -> dict:
    payload = {
        "contract_version": "ccr.reviewer_result.v1",
        "findings": findings,
        "summary": summary,
        "raw_response": raw_response,
    }
    if llm_invocation is not None:
        payload["llm_invocation"] = llm_invocation
    return payload



def _dry_run_review_output(provider: str) -> dict:
    return _make_review_output(
        findings=[],
        summary="[dry-run] Review skipped.",
        raw_response="[dry-run]",
        llm_invocation=build_llm_invocation({"provider": provider}, provider=provider),
    )



def _extract_review_output(proxy_result: dict, *, provider: Optional[str] = None) -> dict:
    """
    Parse the LLM response into the code review output format:
    {"findings": [...], "summary": "...", "raw_response": "...", "llm_invocation": {...}}
    """
    raw = str(proxy_result.get("response") or "")
    llm_invocation = build_llm_invocation(proxy_result, provider=provider)

    if proxy_result.get("exit_code", 0) != 0 or proxy_result.get("error"):
        return _make_review_output(
            findings=[],
            summary="Review could not be completed: {}".format(
                proxy_result.get("error", "unknown error")
            ),
            raw_response=raw,
            llm_invocation=llm_invocation,
        )

    try:
        text = raw.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines).strip()

        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise ValueError("review response must decode to a JSON object")
        findings = parsed.get("findings") if isinstance(parsed.get("findings"), list) else []
        summary = parsed.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            summary = "Review response did not include a valid summary. See raw_response for details."
        parsed["contract_version"] = "ccr.reviewer_result.v1"
        parsed["findings"] = findings
        parsed["summary"] = summary
        parsed["raw_response"] = raw
        parsed["llm_invocation"] = llm_invocation
        return parsed
    except (json.JSONDecodeError, ValueError):
        return _make_review_output(
            findings=[],
            summary="Review response was not valid JSON. See raw_response for details.",
            raw_response=raw,
            llm_invocation=llm_invocation,
        )


# ── CLI ───────────────────────────────────────────────────────────────────────

def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="code-review",
        description=(
            "Specialized Go code review wrapper over llm-proxy. "
            "Generates a diff from --scope, constructs a Go-focused review prompt, "
            "and returns structured JSON findings."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s --scope uncommitted --provider codex\n"
            "  %(prog)s --scope commit:abc1234 --provider gemini\n"
            "  %(prog)s --scope branch:main --output-file /tmp/review.json --dry-run\n"
            "  %(prog)s --scope file:internal/service/auth.go --provider codex\n"
            "  %(prog)s --scope package:internal/service --provider codex\n"
            "  %(prog)s --scope package:internal/service --artifact-output /tmp/review_artifact.txt --artifact-only\n"
        ),
    )
    parser.add_argument(
        "--scope",
        required=False,
        help=(
            "What to review. One of:\n"
            "  uncommitted      — staged + unstaged changes (git diff HEAD)\n"
            "  commit:<SHA>     — a single commit (git show SHA)\n"
            "  branch:<BASE>    — current branch vs BASE (git diff BASE...HEAD)\n"
            "  file:<PATH>      — existing Go file, reviewed via a synthetic full-file diff\n"
            "  package:<PATH>   — Go package directory, reviewed via synthetic full-file diffs"
        ),
    )
    parser.add_argument(
        "--provider",
        default="codex",
        choices=list(PROVIDERS),
        help="LLM provider to use (default: codex).",
    )
    parser.add_argument(
        "--output-file",
        default=None,
        dest="output_file",
        help="Optional path to write JSON output to (in addition to stdout).",
    )
    parser.add_argument(
        "--artifact-output",
        default=None,
        dest="artifact_output",
        help="Optional path to write the generated review artifact (real diff or synthetic audit diff).",
    )
    parser.add_argument(
        "--artifact-only",
        action="store_true",
        dest="artifact_only",
        help="Generate the review artifact and stop without invoking the LLM.",
    )
    parser.add_argument(
        "--style-guide",
        default=DEFAULT_STYLE_GUIDE_PATH,
        dest="style_guide",
        help=(
            "Path to Go style guide file to embed in the prompt "
            "(default: prompts/go_style_guide.txt)."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Return mock output without calling the provider.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=600,
        help="Timeout in seconds (default: 600). Gemini and Claude with --effort max can take several minutes on non-trivial diffs.",
    )
    # ── New flags ──────────────────────────────────────────────────────────────
    parser.add_argument(
        "--persona",
        default=None,
        choices=list(PERSONAS),
        help=(
            "Review persona to use. When specified, loads prompts/review_{persona}.txt "
            "instead of the default code_review.txt. "
            "Choices: logic, security, concurrency, performance, requirements."
        ),
    )
    parser.add_argument(
        "--diff-file",
        default=None,
        dest="diff_file",
        help=(
            "Path to a file containing the diff to review. "
            "When specified, reads diff from file instead of generating via git. "
            "Enables MR review workflow where diff is pre-fetched."
        ),
    )
    parser.add_argument(
        "--static-analysis",
        default=None,
        dest="static_analysis",
        metavar="auto|skip|PATH",
        help=(
            "Static analysis mode:\n"
            "  auto  — run static_analysis.py on CWD\n"
            "  skip  — no static analysis\n"
            "  PATH  — load pre-generated static analysis JSON from PATH"
        ),
    )
    parser.add_argument(
        "--requirements-file",
        default=None,
        dest="requirements_file",
        help=(
            "Path to a requirements text file. When specified with "
            "--persona requirements, fills the {requirements} placeholder in the prompt."
        ),
    )
    parser.add_argument(
        "--review-context-file",
        default=None,
        dest="review_context_file",
        help=(
            "Path to repository/package context markdown. When specified, fills the "
            "{review_context} placeholder in the prompt."
        ),
    )
    parser.add_argument(
        "--review-prepare-file",
        default=None,
        dest="review_prepare_file",
        help=(
            "Path to deterministic pre-review context JSON. When specified, fills the "
            "{review_prepare} placeholder in supported prompts."
        ),
    )
    return parser


def main() -> None:
    parser = _build_arg_parser()
    args = parser.parse_args()

    # Require at least one diff source (unless dry-run)
    if args.diff_file is None and args.scope is None and not args.dry_run:
        parser.error("one of --scope or --diff-file is required (unless --dry-run)")

    # Step 1: Get diff — from file, dry-run placeholder, or git
    if args.diff_file is not None:
        try:
            diff = _load_text(args.diff_file).strip() or "(no changes)"
        except OSError as exc:
            error_out = _make_review_output(
                findings=[],
                summary="Failed to read diff file: {}".format(exc),
                raw_response="",
            )
            print(json.dumps(error_out, indent=2))
            sys.exit(1)
    elif args.dry_run:
        diff = "(dry-run: no diff generated)"
    else:
        try:
            diff = _generate_diff(args.scope)
        except (ValueError, RuntimeError) as exc:
            error_out = _make_review_output(
                findings=[],
                summary="Failed to generate diff: {}".format(exc),
                raw_response="",
            )
            print(json.dumps(error_out, indent=2))
            sys.exit(1)

    if args.artifact_output:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(args.artifact_output)), exist_ok=True)
            with open(args.artifact_output, "w", encoding="utf-8") as f:
                f.write(diff)
        except OSError as exc:
            error_out = _make_review_output(
                findings=[],
                summary="Failed to write review artifact: {}".format(exc),
                raw_response="",
            )
            print(json.dumps(error_out, indent=2))
            sys.exit(1)

    if args.artifact_only:
        if not args.artifact_output:
            print(diff)
        sys.exit(0)

    # Step 2: Resolve static analysis text
    static_analysis_text = ""
    sa_mode = args.static_analysis

    if sa_mode is None or sa_mode == "skip":
        static_analysis_text = ""
    elif sa_mode == "auto":
        if args.dry_run:
            static_analysis_text = "(dry-run: static analysis skipped)"
        else:
            changed_files = _scope_changed_files(args.scope, os.path.abspath(os.getcwd())) if args.scope else None
            sa_data = _run_static_analysis_auto(os.getcwd(), changed_files)
            static_analysis_text = _format_sa_for_prompt(sa_data, args.persona)
    else:
        # Treat sa_mode as a file path to pre-generated JSON
        sa_data = _load_static_analysis_json(sa_mode)
        static_analysis_text = _format_sa_for_prompt(sa_data, args.persona)

    # Step 3: Resolve requirements text
    requirements_text = ""
    if args.requirements_file is not None:
        try:
            requirements_text = _load_text(args.requirements_file)
        except OSError as exc:
            print(
                "WARNING: Could not load requirements file {}: {}".format(
                    args.requirements_file, exc
                ),
                file=sys.stderr,
            )

    # Step 4: Resolve review context text
    review_context_text = ""
    if args.review_context_file is not None:
        try:
            review_context_text = _load_text(args.review_context_file)
        except OSError as exc:
            print(
                "WARNING: Could not load review context file {}: {}".format(
                    args.review_context_file, exc
                ),
                file=sys.stderr,
            )

    # Step 5: Resolve review preparation text
    review_prepare_text = ""
    if args.review_prepare_file is not None:
        try:
            review_prepare_text = _load_text(args.review_prepare_file)
        except OSError as exc:
            print(
                "WARNING: Could not load review prepare file {}: {}".format(
                    args.review_prepare_file, exc
                ),
                file=sys.stderr,
            )

    # Step 6: Build prompt
    prompt = _build_prompt(
        diff=diff,
        style_guide_path=args.style_guide,
        persona=args.persona,
        static_analysis_text=static_analysis_text,
        requirements_text=requirements_text,
        review_context_text=review_context_text,
        review_prepare_text=review_prepare_text,
    )

    if args.dry_run:
        review_output = _dry_run_review_output(args.provider)
        out_json = json.dumps(review_output, indent=2)
        print(out_json)
        if args.output_file:
            try:
                os.makedirs(os.path.dirname(os.path.abspath(args.output_file)), exist_ok=True)
                with open(args.output_file, "w", encoding="utf-8") as f:
                    f.write(out_json)
            except OSError as exc:
                print("WARNING: Could not write output file: {}".format(exc), file=sys.stderr)
        sys.exit(0)

    # Step 7: Invoke llm-proxy
    schema_path = os.path.join(_HERE, "schemas", "code_review_response.schema.json")

    proxy_result = run_proxy(
        prompt=prompt,
        provider=args.provider,
        dry_run=False,
        timeout=args.timeout,
        response_schema=schema_path,
        output_file=None,  # We handle output ourselves below
    )

    # Step 8: Extract and structure review output
    review_output = _extract_review_output(proxy_result, provider=args.provider)

    # Step 9: Write output
    out_json = json.dumps(review_output, indent=2)
    print(out_json)

    if args.output_file:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(args.output_file)), exist_ok=True)
            with open(args.output_file, "w", encoding="utf-8") as f:
                f.write(out_json)
        except OSError as exc:
            print("WARNING: Could not write output file: {}".format(exc), file=sys.stderr)

    # Exit non-zero only if the LLM call itself failed (not a review with 0 findings)
    exit_code = proxy_result.get("exit_code", 0)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
