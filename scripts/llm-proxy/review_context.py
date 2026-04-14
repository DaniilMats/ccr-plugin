#!/usr/bin/env python3
"""review_context.py — Build compact repository/package context for CCR review prompts.

Consumes a review artifact (real diff or synthetic full-code diff) and emits a
compact markdown summary for the focused Go files/packages so reviewer prompts
can reason about local conventions, neighboring tests, and package shape.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable


_DIFF_HEADER_RE = re.compile(r"^diff --git a/(.+?) b/(.+)$")
_GO_MODULE_RE = re.compile(r"^module\s+(\S+)\s*$", re.MULTILINE)
_PACKAGE_RE = re.compile(r"^\s*package\s+([A-Za-z_]\w*)\s*$", re.MULTILINE)
_IMPORT_BLOCK_RE = re.compile(r"^\s*import\s*\((.*?)^\s*\)", re.MULTILINE | re.DOTALL)
_SINGLE_IMPORT_RE = re.compile(r'^\s*import\s+(?:[A-Za-z_]\w*\s+)?"([^"]+)"', re.MULTILINE)
_QUOTED_IMPORT_RE = re.compile(r'"([^"]+)"')
_FUNC_RE = re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*\(", re.MULTILINE)
_TYPE_RE = re.compile(r"^\s*type\s+([A-Za-z_]\w*)\s+", re.MULTILINE)
_VAR_RE = re.compile(r"^\s*var\s+([A-Za-z_]\w*)\s", re.MULTILINE)
_CONST_RE = re.compile(r"^\s*const\s+([A-Za-z_]\w*)\s", re.MULTILINE)
_TEST_FUNC_RE = re.compile(r"^\s*func\s+((?:Test|Benchmark|Example)[A-Za-z0-9_]*)\s*\(", re.MULTILINE)

_MAX_FOCUS_FILES = 20
_MAX_PACKAGE_FILES = 12
_MAX_SYMBOLS = 15
_MAX_TEST_SYMBOLS = 12
_MAX_IMPORTS = 12
_MAX_PACKAGE_DIRS = 8
_REPOMAP_TOKEN_BUDGET = 640
_REPOMAP_TIMEOUT_SEC = 20


def _load_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="replace")


def _dedupe_preserve_order(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _limited(items: list[str], max_items: int) -> list[str]:
    if len(items) <= max_items:
        return items
    hidden = len(items) - max_items
    return items[:max_items] + [f"... (+{hidden} more)"]


def _shorten(text: str, max_len: int = 220) -> str:
    normalized = re.sub(r"\s+", " ", text).strip()
    if len(normalized) <= max_len:
        return normalized
    return normalized[: max_len - 1].rstrip() + "…"


def _append_bullet_block(lines: list[str], label: str, items: list[str], max_items: int) -> None:
    lines.append(f"- {label}:")
    if not items:
        lines.append("  - (none)")
        return
    for item in _limited(items, max_items):
        lines.append(f"  - {item}")


def _extract_focus_files_from_artifact(artifact_text: str) -> list[str]:
    focus_files: list[str] = []
    for line in artifact_text.splitlines():
        match = _DIFF_HEADER_RE.match(line.strip())
        if not match:
            continue
        a_path, b_path = match.groups()
        candidate = b_path if b_path != "/dev/null" else a_path
        if candidate == "/dev/null":
            continue
        focus_files.append(candidate)
    return _dedupe_preserve_order(focus_files)


def _read_go_module_path(project_dir: Path) -> str | None:
    go_mod = project_dir / "go.mod"
    if not go_mod.is_file():
        return None
    match = _GO_MODULE_RE.search(_load_text(go_mod))
    return match.group(1) if match else None


def _derive_import_path(module_path: str | None, rel_dir: str) -> str | None:
    if not module_path:
        return None
    if rel_dir in ("", "."):
        return module_path
    return module_path.rstrip("/") + "/" + rel_dir.replace(os.sep, "/")


def _list_package_go_files(package_dir: Path) -> list[Path]:
    if not package_dir.is_dir():
        return []
    files = [
        path
        for path in package_dir.iterdir()
        if path.is_file() and path.suffix == ".go" and not path.name.startswith(".")
    ]
    return sorted(files, key=lambda p: (p.name.endswith("_test.go"), p.name))


def _extract_package_name(go_files: list[Path]) -> str | None:
    for path in go_files:
        match = _PACKAGE_RE.search(_load_text(path))
        if match:
            return match.group(1)
    return None


def _extract_imports(go_files: list[Path]) -> list[str]:
    imports: list[str] = []
    for path in go_files:
        text = _load_text(path)
        for block in _IMPORT_BLOCK_RE.findall(text):
            imports.extend(_QUOTED_IMPORT_RE.findall(block))
        imports.extend(_SINGLE_IMPORT_RE.findall(text))
    return _dedupe_preserve_order(imports)


def _extract_exported_symbols(go_files: list[Path]) -> list[str]:
    symbols: list[str] = []
    for path in go_files:
        if path.name.endswith("_test.go"):
            continue
        text = _load_text(path)
        candidates = (
            _FUNC_RE.findall(text)
            + _TYPE_RE.findall(text)
            + _VAR_RE.findall(text)
            + _CONST_RE.findall(text)
        )
        for name in candidates:
            if name and name[0].isupper():
                symbols.append(name)
    return _dedupe_preserve_order(symbols)


def _extract_test_symbols(go_files: list[Path]) -> list[str]:
    symbols: list[str] = []
    for path in go_files:
        if not path.name.endswith("_test.go"):
            continue
        symbols.extend(_TEST_FUNC_RE.findall(_load_text(path)))
    return _dedupe_preserve_order(symbols)


def _extract_package_doc(go_files: list[Path]) -> str | None:
    preferred = sorted(go_files, key=lambda p: (p.name != "doc.go", p.name.endswith("_test.go"), p.name))
    for path in preferred:
        lines = _load_text(path).splitlines()
        for idx, line in enumerate(lines):
            if not _PACKAGE_RE.match(line):
                continue
            comments: list[str] = []
            cursor = idx - 1
            while cursor >= 0:
                stripped = lines[cursor].strip()
                if not stripped:
                    cursor -= 1
                    continue
                if stripped.startswith("//"):
                    comments.append(stripped[2:].strip())
                    cursor -= 1
                    continue
                break
            if comments:
                comments.reverse()
                return _shorten(" ".join(part for part in comments if part))
            break
    return None


def _build_focused_repomap(project_dir: Path, focus_files: list[str]) -> str:
    if not focus_files:
        return ""

    repomap_script = Path(__file__).resolve().parents[1] / "repomap.py"
    if not repomap_script.is_file():
        return ""

    cmd = [
        "python3",
        str(repomap_script),
        str(project_dir),
        "--tokens",
        str(_REPOMAP_TOKEN_BUDGET),
        "--format",
        "markdown",
        "--focus-files",
        ",".join(focus_files),
    ]
    if (project_dir / "go.mod").is_file():
        cmd.extend(["--lang", "go"])

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=_REPOMAP_TIMEOUT_SEC,
            cwd=project_dir,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""

    if result.returncode != 0:
        return ""

    return result.stdout.strip()


def _render_placeholder(project_dir: str, focus_files: list[str], reason: str) -> str:
    lines = [
        "## Review Target",
        f"- Repo root: {project_dir}",
        f"- Repository/package context unavailable: {reason}",
    ]
    if focus_files:
        _append_bullet_block(lines, "Focus files", focus_files, _MAX_FOCUS_FILES)
    return "\n".join(lines)


def _render_package_summary(
    project_dir: Path,
    rel_dir: str,
    focused_rel_files: list[str],
    module_path: str | None,
) -> str:
    package_dir = project_dir if rel_dir in ("", ".") else project_dir / rel_dir
    package_files = _list_package_go_files(package_dir)

    lines = [f"## Focus Package: {rel_dir or '.'}"]
    if not package_dir.is_dir():
        lines.append("- Local package directory not available in this checkout.")
        return "\n".join(lines)

    if not package_files:
        lines.append("- No .go files found in the local package directory.")
        return "\n".join(lines)

    package_name = _extract_package_name(package_files) or "(unknown)"
    import_path = _derive_import_path(module_path, rel_dir)
    package_doc = _extract_package_doc(package_files)
    exported_symbols = _extract_exported_symbols(package_files)
    test_symbols = _extract_test_symbols(package_files)
    imports = _extract_imports(package_files)
    internal_deps = [imp for imp in imports if module_path and imp.startswith(module_path)]

    lines.append(f"- Package name: {package_name}")
    if import_path:
        lines.append(f"- Import path: {import_path}")
    if package_doc:
        lines.append(f"- Package doc: {_shorten(package_doc)}")

    _append_bullet_block(
        lines,
        "Focused files in this package",
        [Path(path).name for path in focused_rel_files],
        _MAX_PACKAGE_FILES,
    )
    _append_bullet_block(
        lines,
        "Package files",
        [path.name for path in package_files],
        _MAX_PACKAGE_FILES,
    )
    _append_bullet_block(lines, "Exported symbols", exported_symbols, _MAX_SYMBOLS)
    _append_bullet_block(lines, "Test symbols", test_symbols, _MAX_TEST_SYMBOLS)
    _append_bullet_block(lines, "Notable imports", imports, _MAX_IMPORTS)
    if internal_deps:
        _append_bullet_block(lines, "Internal deps", internal_deps, _MAX_IMPORTS)

    return "\n".join(lines)


def build_review_context(project_dir: str, artifact_text: str) -> str:
    project_root = Path(project_dir).expanduser().resolve()
    focus_files = _extract_focus_files_from_artifact(artifact_text)

    if not project_root.exists() or not project_root.is_dir():
        return _render_placeholder(str(project_root), focus_files, "project directory does not exist")

    module_path = _read_go_module_path(project_root)
    missing_focus_files: list[str] = []
    focus_go_files: list[str] = []

    for rel_path in focus_files:
        abs_path = project_root / rel_path
        if abs_path.is_file():
            if abs_path.suffix == ".go":
                focus_go_files.append(rel_path)
        else:
            missing_focus_files.append(rel_path)

    package_dirs = _dedupe_preserve_order(
        str(Path(rel_path).parent).replace("\\", "/") or "."
        for rel_path in focus_go_files
    )

    sections = [
        "## Review Target",
        f"- Repo root: {project_root}",
        f"- Go module: {module_path or '(not found)'}",
        f"- Focus file count: {len(focus_files)}",
    ]
    _append_bullet_block(sections, "Focus files", focus_files, _MAX_FOCUS_FILES)
    if missing_focus_files:
        _append_bullet_block(sections, "Missing from local checkout", missing_focus_files, _MAX_FOCUS_FILES)
    if package_dirs:
        _append_bullet_block(sections, "Focus package dirs", package_dirs, _MAX_PACKAGE_DIRS)

    for rel_dir in package_dirs[:_MAX_PACKAGE_DIRS]:
        focused_in_dir = [rel_path for rel_path in focus_go_files if (str(Path(rel_path).parent).replace("\\", "/") or ".") == rel_dir]
        sections.append("")
        sections.append(_render_package_summary(project_root, rel_dir, focused_in_dir, module_path))

    repomap = _build_focused_repomap(project_root, focus_go_files or focus_files)
    if repomap:
        sections.append("")
        sections.append("## Focused Repo Map")
        sections.append(repomap)

    return "\n".join(section for section in sections if section is not None).strip()


def _safe_build_review_context(project_dir: str, artifact_text: str) -> str:
    try:
        return build_review_context(project_dir, artifact_text)
    except Exception as exc:  # pragma: no cover - defensive fallback
        focus_files = _extract_focus_files_from_artifact(artifact_text)
        return _render_placeholder(project_dir, focus_files, f"unexpected error: {exc}")


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="review_context.py",
        description="Build compact repository/package context for CCR reviewer prompts.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s --project-dir . --artifact-file /tmp/ccr_mr_diff.txt\n"
            "  %(prog)s --project-dir ~/projects/service --artifact-file /tmp/review.txt --output-file /tmp/ccr_review_context.md\n"
        ),
    )
    parser.add_argument("--project-dir", required=True, help="Path to the local repository checkout.")
    parser.add_argument("--artifact-file", required=True, help="Path to the review artifact (real diff or synthetic full-code diff).")
    parser.add_argument("--output-file", default=None, help="Optional path to write the rendered markdown context.")
    return parser


def main() -> None:
    parser = _build_arg_parser()
    args = parser.parse_args()

    artifact_path = Path(args.artifact_file).expanduser()
    if artifact_path.is_file():
        artifact_text = _load_text(artifact_path)
    else:
        artifact_text = ""

    context = _safe_build_review_context(args.project_dir, artifact_text)

    if args.output_file:
        output_path = Path(args.output_file).expanduser()
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(context + "\n", encoding="utf-8")
        except OSError as exc:
            print(f"WARNING: Could not write output file: {exc}", file=sys.stderr)

    print(context)


if __name__ == "__main__":
    main()
