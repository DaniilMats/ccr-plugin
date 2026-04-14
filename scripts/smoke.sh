#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

cd "$ROOT_DIR"

echo "[smoke] py_compile"
python3 -m py_compile \
  quality/scripts/ccr_run_init.py \
  quality/scripts/ccr_run.py \
  quality/scripts/ccr_watch.py \
  quality/scripts/repomap.py \
  quality/scripts/ccr_routing.py \
  quality/scripts/llm-proxy/code_review.py \
  quality/scripts/llm-proxy/code_review_verify.py \
  quality/scripts/llm-proxy/review_context.py \
  quality/scripts/llm-proxy/static_analysis.py \
  quality/scripts/llm-proxy/llm_proxy.py \
  quality/scripts/llm-proxy/adapters/base.py \
  quality/scripts/llm-proxy/adapters/codex.py \
  quality/scripts/llm-proxy/adapters/gemini.py \
  quality/scripts/llm-proxy/adapters/claude.py \
  tests/*.py

echo "[smoke] unit tests"
python3 -m unittest discover -s tests -v

echo "[smoke] run init"
python3 quality/scripts/ccr_run_init.py --base-dir "$TMP_DIR/ccr" > "$TMP_DIR/run_manifest.json"

echo "[smoke] routing helper"
python3 quality/scripts/ccr_routing.py \
  --input-file tests/fixtures/routing/route_input_small.json \
  --output-file "$TMP_DIR/route_plan.json" > /dev/null

echo "[smoke] repomap"
python3 quality/scripts/repomap.py \
  tests/fixtures/go_repo \
  --focus-files internal/auth/jwt.go > "$TMP_DIR/repomap.md"

echo "[smoke] review context"
python3 quality/scripts/llm-proxy/review_context.py \
  --project-dir tests/fixtures/go_repo \
  --artifact-file tests/fixtures/go_repo/review_artifact.txt \
  --output-file "$TMP_DIR/review_context.md" > /dev/null

echo "[smoke] deterministic harness"
python3 quality/scripts/ccr_run.py \
  package:internal/auth \
  --project-dir tests/fixtures/go_repo \
  --dry-run \
  --base-dir "$TMP_DIR/phase1" > "$TMP_DIR/ccr_run_summary.json"

python3 - <<'PY' "$TMP_DIR/ccr_run_summary.json"
import json, sys
from pathlib import Path
summary = json.loads(Path(sys.argv[1]).read_text())
status = json.loads(Path(summary["status_file"]).read_text())
assert summary["contract_version"] == "ccr.run_summary.v1"
assert status["contract_version"] == "ccr.run_status.v1"
assert status["state"] == "completed"
assert Path(summary["trace_file"]).is_file()
PY

echo "[smoke] detached harness + watch"
python3 quality/scripts/ccr_run.py \
  package:internal/auth \
  --project-dir tests/fixtures/go_repo \
  --dry-run \
  --base-dir "$TMP_DIR/phase12" \
  --detach > "$TMP_DIR/ccr_run_launch.json"

python3 - <<'PY' "$TMP_DIR/ccr_run_launch.json" "$ROOT_DIR"
import json, subprocess, sys
from pathlib import Path
launch = json.loads(Path(sys.argv[1]).read_text())
root = Path(sys.argv[2])
assert launch["contract_version"] == "ccr.run_launch.v1"
cursor = Path(launch["watch_cursor_file"])
payload = None
for _ in range(10):
    result = subprocess.run([
        "python3",
        str(root / "quality/scripts/ccr_watch.py"),
        "--status-file", launch["status_file"],
        "--trace-file", launch["trace_file"],
        "--pid", str(launch["pid"]),
        "--cursor-file", str(cursor),
        "--wait-seconds", "2",
        "--emit-heartbeat",
    ], capture_output=True, text=True, check=True)
    payload = json.loads(result.stdout)
    if payload["done"]:
        break
assert payload is not None
assert payload["contract_version"] == "ccr.watch_result.v1"
assert payload["done"] is True
assert payload["state"] == "completed"
text_result = subprocess.run([
    "python3",
    str(root / "quality/scripts/ccr_watch.py"),
    "--status-file", launch["status_file"],
    "--trace-file", launch["trace_file"],
    "--cursor-file", str(cursor),
    "--format", "text",
    "--quiet-unchanged",
], capture_output=True, text=True, check=True)
assert text_result.stdout.strip() == ""
summary = json.loads(Path(launch["summary_file"]).read_text())
status = json.loads(Path(launch["status_file"]).read_text())
assert summary["contract_version"] == "ccr.run_summary.v1"
assert status["contract_version"] == "ccr.run_status.v1"
PY

echo "[smoke] ok"
