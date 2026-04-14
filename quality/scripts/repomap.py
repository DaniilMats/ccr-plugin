#!/usr/bin/env python3
"""Lightweight focused repo map generator for CCR.

This is intentionally small and deterministic. It produces a compact markdown map
for a list of focus files so `review_context.py` can enrich reviewer prompts
without depending on a missing external utility.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path


PACKAGE_RE = re.compile(r"^\s*package\s+([A-Za-z_]\w*)\s*$", re.MULTILINE)
FUNC_RE = re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*\(", re.MULTILINE)
TYPE_RE = re.compile(r"^\s*type\s+([A-Za-z_]\w*)\s+", re.MULTILINE)
VAR_RE = re.compile(r"^\s*var\s+([A-Za-z_]\w*)\s", re.MULTILINE)
CONST_RE = re.compile(r"^\s*const\s+([A-Za-z_]\w*)\s", re.MULTILINE)
MAX_SYMBOLS = 8


def _load_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="replace")


def _package_name(text: str) -> str:
    match = PACKAGE_RE.search(text)
    return match.group(1) if match else "(unknown)"


def _exported_symbols(text: str) -> list[str]:
    symbols: list[str] = []
    for name in FUNC_RE.findall(text) + TYPE_RE.findall(text) + VAR_RE.findall(text) + CONST_RE.findall(text):
        if name and name[0].isupper() and name not in symbols:
            symbols.append(name)
    return symbols[:MAX_SYMBOLS]


def _render_markdown(project_dir: Path, focus_files: list[str]) -> str:
    lines = ["### Focused Repo Map"]
    if not focus_files:
        lines.append("- No focus files provided.")
        return "\n".join(lines)

    for rel_path in focus_files:
        path = (project_dir / rel_path).resolve()
        lines.append(f"- `{rel_path}`")
        if not path.is_file():
            lines.append("  - missing from local checkout")
            continue
        text = _load_text(path)
        lines.append(f"  - package: {_package_name(text)}")
        symbols = _exported_symbols(text)
        if symbols:
            lines.append("  - exported symbols: " + ", ".join(symbols))
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="repomap",
        description="Generate a lightweight markdown repo map for focus files.",
    )
    parser.add_argument("project_dir", help="Repository root")
    parser.add_argument("--tokens", type=int, default=640, help="Ignored compatibility flag.")
    parser.add_argument("--format", default="markdown", help="Output format. Only markdown is supported.")
    parser.add_argument("--focus-files", default="", help="Comma-separated list of focus files relative to project_dir.")
    parser.add_argument("--lang", default="", help="Ignored compatibility flag.")
    return parser


def main() -> None:
    args = _build_parser().parse_args()
    project_dir = Path(args.project_dir).expanduser().resolve()
    focus_files = [item.strip() for item in args.focus_files.split(",") if item.strip()]
    print(_render_markdown(project_dir, focus_files))


if __name__ == "__main__":
    main()
