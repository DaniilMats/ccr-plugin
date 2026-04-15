#!/usr/bin/env python3
"""Initialize an isolated CCR run workspace.

Creates a per-run directory, emits a run manifest with stable artifact paths,
and prints the manifest as JSON so the agent can reuse the paths across the
review workflow.

Examples:
    python3 ccr_run_init.py
    python3 ccr_run_init.py --base-dir /tmp/ccr --output-file /tmp/ccr-last-run.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ccr_runtime.common import write_json
from ccr_runtime.manifest import DEFAULT_BASE_DIR, RUN_MANIFEST_VERSION, build_manifest, build_run_id

# Backward-compatible aliases for existing tests/importers.
_build_manifest = build_manifest
_build_run_id = build_run_id
_write_json = write_json


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ccr-run-init",
        description="Create an isolated CCR run workspace and print its manifest as JSON.",
    )
    parser.add_argument(
        "--base-dir",
        default=DEFAULT_BASE_DIR,
        help=f"Base directory for run workspaces (default: {DEFAULT_BASE_DIR}).",
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="Optional explicit run_id. Normally auto-generated.",
    )
    parser.add_argument(
        "--output-file",
        default=None,
        help="Optional extra path to also write the manifest JSON.",
    )
    return parser


def main() -> None:
    args = _build_parser().parse_args()

    base_dir = Path(args.base_dir).expanduser().resolve()
    run_id = args.run_id or build_run_id()
    manifest = build_manifest(base_dir, run_id)

    manifest_path = Path(manifest["manifest_file"])
    write_json(manifest_path, manifest)

    if args.output_file:
        write_json(Path(args.output_file).expanduser().resolve(), manifest)

    print(json.dumps(manifest, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
