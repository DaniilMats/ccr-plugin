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

echo "[smoke] ok"
