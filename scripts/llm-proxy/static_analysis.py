#!/usr/bin/env python3
"""
Static Analysis Pre-Filter Script

Runs Go static analysis tools in parallel, parses output into structured JSON,
and optionally filters findings to changed files only.

Usage:
    python3 static_analysis.py --project-dir /path/to/go/project [options]
"""

import argparse
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional


# ---------------------------------------------------------------------------
# Output schema
# ---------------------------------------------------------------------------

def empty_result() -> dict:
    return {
        "go_vet": [],
        "staticcheck": [],
        "gosec": [],
        "tools_available": {
            "go_vet": False,
            "staticcheck": False,
            "gosec": False,
        },
        "categories": {
            "logic": [],
            "security": [],
            "all": [],
        },
        "error": None,
    }


# ---------------------------------------------------------------------------
# Tool runners
# ---------------------------------------------------------------------------

def _is_available(executable: str) -> bool:
    """Return True if the executable can be found on PATH."""
    try:
        result = subprocess.run(
            ["which", executable],
            capture_output=True,
            start_new_session=True,
        )
        return result.returncode == 0
    except FileNotFoundError:
        return False


def _run_tool(cmd: list[str], cwd: str) -> tuple[int, str, str]:
    """
    Run a command and return (returncode, stdout, stderr).
    Returns (127, '', '') when the executable is not found.
    """
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            start_new_session=True,
            text=True,
        )
        stdout, stderr = proc.communicate()
        return proc.returncode, stdout, stderr
    except FileNotFoundError:
        return 127, "", f"{cmd[0]}: command not found"
    except Exception as exc:  # noqa: BLE001
        return 1, "", str(exc)


# ---------------------------------------------------------------------------
# Output parsers
# ---------------------------------------------------------------------------

# go vet / compiler-style: file.go:line:col: message
_GO_VET_RE = re.compile(
    r"^(?P<file>[^:\s][^:]*\.go):(?P<line>\d+)(?::\d+)?:\s*(?P<message>.+)$"
)

# staticcheck: file.go:line:col: message (code)
_STATICCHECK_RE = re.compile(
    r"^(?P<file>[^:\s][^:]*\.go):(?P<line>\d+)(?::\d+)?:\s*(?P<message>.+?)\s*(?:\((?P<code>[A-Z]+\d+)\))?$"
)

# gosec: [/abs/path/file.go:line] - G101 (message) ...
_GOSEC_ISSUE_RE = re.compile(
    r"\[(?P<file>[^\]]+\.go):(?P<line>\d+)\].*?- (?P<code>G\d+) \((?P<message>[^)]+)\)"
)


def _parse_go_vet(output: str, project_dir: str) -> list[dict]:
    findings = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        m = _GO_VET_RE.match(line)
        if m:
            findings.append({
                "tool": "go_vet",
                "file": _normalize_path(m.group("file"), project_dir),
                "line": int(m.group("line")),
                "message": m.group("message").strip(),
                "code": None,
            })
    return findings


def _parse_staticcheck(output: str, project_dir: str) -> list[dict]:
    findings = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        m = _STATICCHECK_RE.match(line)
        if m:
            findings.append({
                "tool": "staticcheck",
                "file": _normalize_path(m.group("file"), project_dir),
                "line": int(m.group("line")),
                "message": m.group("message").strip(),
                "code": m.group("code"),
            })
    return findings


def _parse_gosec(output: str, project_dir: str) -> list[dict]:
    findings = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        m = _GOSEC_ISSUE_RE.search(line)
        if m:
            findings.append({
                "tool": "gosec",
                "file": _normalize_path(m.group("file"), project_dir),
                "line": int(m.group("line")),
                "message": m.group("message").strip(),
                "code": m.group("code"),
            })
    return findings


def _normalize_path(path: str, project_dir: str) -> str:
    """Make path relative to project_dir when possible."""
    abs_path = path if os.path.isabs(path) else os.path.join(project_dir, path)
    try:
        return os.path.relpath(abs_path, project_dir)
    except ValueError:
        return path


# ---------------------------------------------------------------------------
# Filter helpers
# ---------------------------------------------------------------------------

def _build_changed_set(changed_files_arg: Optional[str], project_dir: str) -> Optional[set[str]]:
    """Return a set of normalised relative paths for filtering, or None for no filter."""
    if not changed_files_arg:
        return None
    result = set()
    for raw in changed_files_arg.split(","):
        f = raw.strip()
        if not f:
            continue
        # Normalise to relative path from project_dir
        result.add(_normalize_path(f, project_dir))
    return result if result else None


def _filter_findings(findings: list[dict], changed_set: Optional[set[str]]) -> list[dict]:
    if changed_set is None:
        return findings
    return [f for f in findings if f["file"] in changed_set]


# ---------------------------------------------------------------------------
# Category mapping
# ---------------------------------------------------------------------------

CATEGORY_MAP = {
    "logic": "go_vet",
    "security": "gosec",
    "all": "staticcheck",
}


def _build_categories(result: dict) -> dict:
    return {
        "logic": result["go_vet"],
        "security": result["gosec"],
        "all": result["staticcheck"],
    }


# ---------------------------------------------------------------------------
# Dry-run
# ---------------------------------------------------------------------------

def _dry_run_result() -> dict:
    res = empty_result()
    # Mark go_vet as available (it ships with Go) for dry-run display
    res["tools_available"]["go_vet"] = True
    res["categories"] = _build_categories(res)
    return res


# ---------------------------------------------------------------------------
# Main analysis logic
# ---------------------------------------------------------------------------

def run_analysis(project_dir: str, changed_files: Optional[str]) -> dict:
    result = empty_result()
    changed_set = _build_changed_set(changed_files, project_dir)

    tool_specs = [
        {
            "key": "go_vet",
            "cmd": ["go", "vet", "./..."],
            "exe": "go",
            "parser": _parse_go_vet,
        },
        {
            "key": "staticcheck",
            "cmd": ["staticcheck", "./..."],
            "exe": "staticcheck",
            "parser": _parse_staticcheck,
        },
        {
            "key": "gosec",
            "cmd": ["gosec", "./..."],
            "exe": "gosec",
            "parser": _parse_gosec,
        },
    ]

    def _run_spec(spec: dict) -> tuple[str, bool, list[dict]]:
        if not _is_available(spec["exe"]):
            print(
                f"[static_analysis] WARNING: {spec['exe']} not found, skipping",
                file=sys.stderr,
            )
            return spec["key"], False, []

        rc, stdout, stderr = _run_tool(spec["cmd"], project_dir)

        if rc == 127:
            print(
                f"[static_analysis] WARNING: {spec['exe']} not found (exit 127), skipping",
                file=sys.stderr,
            )
            return spec["key"], False, []

        # go vet / staticcheck exit 1 when issues found — that's fine
        combined_output = stdout + stderr
        findings = spec["parser"](combined_output, project_dir)
        return spec["key"], True, findings

    with ThreadPoolExecutor(max_workers=len(tool_specs)) as pool:
        futures = {pool.submit(_run_spec, spec): spec for spec in tool_specs}
        for future in as_completed(futures):
            try:
                key, available, findings = future.result()
                result["tools_available"][key] = available
                filtered = _filter_findings(findings, changed_set)
                result[key] = filtered
            except Exception as exc:  # noqa: BLE001
                spec = futures[future]
                print(
                    f"[static_analysis] ERROR running {spec['key']}: {exc}",
                    file=sys.stderr,
                )

    result["categories"] = _build_categories(result)
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run Go static analysis tools and emit structured JSON findings.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--project-dir",
        required=True,
        metavar="PATH",
        help="Path to the Go project root (directory containing go.mod).",
    )
    parser.add_argument(
        "--changed-files",
        default=None,
        metavar="FILE1,FILE2,...",
        help=(
            "Comma-separated list of changed file paths. "
            "When provided, only findings for these files are included in output."
        ),
    )
    parser.add_argument(
        "--output-file",
        default=None,
        metavar="PATH",
        help="Write JSON output to this file in addition to stdout.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Skip actually running tools; emit an empty-findings JSON structure "
            "with the expected schema. Useful for testing the script itself."
        ),
    )
    parser.add_argument(
        "--categories",
        action="store_true",
        help=(
            "Include a 'categories' key in the output mapping findings to persona "
            "categories: logic (go_vet), security (gosec), all (staticcheck)."
        ),
    )
    return parser


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()

    project_dir = os.path.abspath(args.project_dir)

    if args.dry_run:
        output = _dry_run_result()
    else:
        if not os.path.isdir(project_dir):
            output = empty_result()
            output["error"] = f"project_dir does not exist: {project_dir}"
        else:
            try:
                output = run_analysis(project_dir, args.changed_files)
            except Exception as exc:  # noqa: BLE001
                output = empty_result()
                output["error"] = str(exc)

    # Unless --categories flag was given, still include categories key
    # (it is always populated; the flag is an explicit request for the field)
    if not args.categories and "categories" in output:
        # keep it — it is part of the schema
        pass

    json_str = json.dumps(output, indent=2)

    print(json_str)

    if args.output_file:
        out_path = os.path.abspath(args.output_file)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(json_str)
        print(f"[static_analysis] Output written to {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
