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
  quality/scripts/ccr_post_comments.py \
  quality/scripts/ccr_consolidate.py \
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

echo "[smoke] consolidation helper"
python3 - <<'PY' "$TMP_DIR/consolidate_input.reviewers.json" "$TMP_DIR/consolidate_input.route_plan.json" "$TMP_DIR/consolidate_input.static_analysis.json"
import json, sys
from pathlib import Path
reviewers_path = Path(sys.argv[1])
route_plan_path = Path(sys.argv[2])
static_analysis_path = Path(sys.argv[3])
reviewers_path.write_text(json.dumps([
    {
        "pass_name": "security_p1",
        "persona": "security",
        "provider": "codex",
        "result": {
            "contract_version": "ccr.reviewer_result.v1",
            "findings": [
                {
                    "severity": "bug",
                    "file": "internal/auth/jwt.go",
                    "line": 12,
                    "message": "`ValidateToken` skips JWT token expiry validation."
                }
            ],
            "summary": "security summary"
        }
    },
    {
        "pass_name": "logic_p1",
        "persona": "logic",
        "provider": "gemini",
        "result": {
            "contract_version": "ccr.reviewer_result.v1",
            "findings": [
                {
                    "severity": "warning",
                    "file": "internal/auth/jwt.go",
                    "line": 13,
                    "message": "JWT token expiry validation is missing in ValidateToken."
                }
            ],
            "summary": "logic summary"
        }
    }
], indent=2) + "\n", encoding="utf-8")
route_plan_path.write_text(json.dumps({"pass_counts": {"security": 3, "logic": 3}}, indent=2) + "\n", encoding="utf-8")
static_analysis_path.write_text(json.dumps({
    "go_vet": [],
    "staticcheck": [],
    "gosec": [
        {
            "tool": "gosec",
            "file": "internal/auth/jwt.go",
            "line": 12,
            "message": "Token validation path does not enforce expiry checks.",
            "code": "G999"
        }
    ]
}, indent=2) + "\n", encoding="utf-8")
PY
python3 quality/scripts/ccr_consolidate.py \
  --reviewer-results-file "$TMP_DIR/consolidate_input.reviewers.json" \
  --route-plan-file "$TMP_DIR/consolidate_input.route_plan.json" \
  --static-analysis-file "$TMP_DIR/consolidate_input.static_analysis.json" \
  --output-file "$TMP_DIR/candidates.json" > "$TMP_DIR/candidates.stdout.json"
python3 - <<'PY' "$TMP_DIR/candidates.stdout.json" "$TMP_DIR/candidates.json"
import json, sys
from pathlib import Path
stdout_payload = json.loads(Path(sys.argv[1]).read_text())
written_payload = json.loads(Path(sys.argv[2]).read_text())
assert stdout_payload["contract_version"] == "ccr.candidates_manifest.v1"
assert written_payload["summary"]["candidate_count"] == 1
assert written_payload["candidates"][0]["persona"] == "security"
assert written_payload["candidates"][0]["supporting_personas"] == ["logic"]
PY

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
verification_prepare = json.loads(Path(summary["verification_prepare_file"]).read_text())
assert verification_prepare["contract_version"] == "ccr.verification_prepare.v1"
assert verification_prepare["summary"]["batch_count"] == 0
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
verification_prepare = json.loads(Path(launch["verification_prepare_file"]).read_text())
assert summary["contract_version"] == "ccr.run_summary.v1"
assert status["contract_version"] == "ccr.run_status.v1"
assert verification_prepare["contract_version"] == "ccr.verification_prepare.v1"
PY

echo "[smoke] posting helper prepare-only"
python3 quality/scripts/ccr_run_init.py --base-dir "$TMP_DIR/phase2" > "$TMP_DIR/phase2_manifest.json"
python3 - <<'PY' "$TMP_DIR/phase2_manifest.json"
import json, sys
from pathlib import Path
manifest = json.loads(Path(sys.argv[1]).read_text())
Path(manifest["diff_file"]).write_text("""diff --git a/internal/auth/jwt.go b/internal/auth/jwt.go
index 1111111..2222222 100644
--- a/internal/auth/jwt.go
+++ b/internal/auth/jwt.go
@@ -1,1 +1,2 @@
 package auth
+func Validate() {}
""", encoding="utf-8")
Path(manifest["summary_file"]).write_text(json.dumps({
    "contract_version": "ccr.run_summary.v1",
    "run_id": manifest["run_id"],
    "mode": "mr",
    "target": "https://gitlab.com/group/project/-/merge_requests/200",
}) + "\n", encoding="utf-8")
Path(manifest["mr_metadata_file"]).write_text(json.dumps({
    "iid": 200,
    "diff_refs": {
        "base_sha": "base-sha",
        "start_sha": "start-sha",
        "head_sha": "head-sha",
    },
}) + "\n", encoding="utf-8")
Path(manifest["verified_findings_file"]).write_text(json.dumps({
    "contract_version": "ccr.verified_findings.v1",
    "verified_findings": [
        {
            "finding_number": 1,
            "candidate_id": "F1",
            "file": "internal/auth/jwt.go",
            "line": 2,
            "message": "Validate the token before returning.",
        }
    ],
    "summary": {"verified_count": 1},
}) + "\n", encoding="utf-8")
Path(manifest["posting_approval_file"]).write_text(json.dumps({
    "contract_version": "ccr.posting_approval.v1",
    "run_id": manifest["run_id"],
    "project": "group/project",
    "mr_iid": 200,
    "approved_finding_numbers": [1],
    "approved_all": False,
    "approved_at": "2026-04-15T00:00:00Z",
    "source": "smoke",
}) + "\n", encoding="utf-8")
PY
python3 quality/scripts/ccr_post_comments.py \
  --manifest-file "$TMP_DIR/phase2_manifest.json" \
  --prepare-only > "$TMP_DIR/posting_prepare.json"
python3 - <<'PY' "$TMP_DIR/posting_prepare.json" "$TMP_DIR/phase2_manifest.json"
import json, sys
from pathlib import Path
prepared = json.loads(Path(sys.argv[1]).read_text())
manifest = json.loads(Path(sys.argv[2]).read_text())
assert prepared["contract_version"] == "ccr.posting_manifest.v1"
assert prepared["summary"]["ready_count"] == 1
assert Path(manifest["posting_manifest_file"]).is_file()
PY

echo "[smoke] ok"
