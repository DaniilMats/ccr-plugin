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
  quality/scripts/ccr_report.py \
  quality/scripts/ccr_eval.py \
  quality/scripts/ccr_consolidate.py \
  quality/scripts/ccr_verify_prepare.py \
  quality/scripts/repomap.py \
  quality/scripts/ccr_routing.py \
  quality/scripts/ccr_runtime/common.py \
  quality/scripts/ccr_runtime/manifest.py \
  quality/scripts/ccr_runtime/observer.py \
  quality/scripts/ccr_runtime/reporting.py \
  quality/scripts/ccr_runtime/reviewers.py \
  quality/scripts/ccr_runtime/telemetry.py \
  quality/scripts/ccr_runtime/verification.py \
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

echo "[smoke] static analysis helper"
python3 quality/scripts/llm-proxy/static_analysis.py \
  --project-dir tests/fixtures/go_repo \
  --dry-run \
  --output-file "$TMP_DIR/static_analysis.json" > "$TMP_DIR/static_analysis.stdout.json"
python3 - <<'PY' "$TMP_DIR/static_analysis.stdout.json" "$TMP_DIR/static_analysis.json"
import json, sys
from pathlib import Path
stdout_payload = json.loads(Path(sys.argv[1]).read_text())
written_payload = json.loads(Path(sys.argv[2]).read_text())
assert stdout_payload["contract_version"] == "ccr.static_analysis.v1"
assert stdout_payload["tools_available"]["go_vet"] is True
assert written_payload == stdout_payload
PY

echo "[smoke] shuffle diff"
python3 quality/scripts/llm-proxy/shuffle_diff.py \
  --input-file tests/fixtures/go_repo/review_artifact.txt \
  --output-file "$TMP_DIR/shuffled_review_artifact.txt" \
  --seed 7
python3 - <<'PY' "$TMP_DIR/shuffled_review_artifact.txt"
import sys
from pathlib import Path
text = Path(sys.argv[1]).read_text(encoding="utf-8")
assert "diff --git" in text
assert text.endswith("\n")
PY

echo "[smoke] llm proxy dry-run"
python3 quality/scripts/llm-proxy/llm_proxy.py \
  --provider codex \
  --prompt "Smoke prompt" \
  --dry-run \
  --output-file "$TMP_DIR/llm_proxy.json" > "$TMP_DIR/llm_proxy.stdout.json"
python3 - <<'PY' "$TMP_DIR/llm_proxy.stdout.json" "$TMP_DIR/llm_proxy.json"
import json, sys
from pathlib import Path
stdout_payload = json.loads(Path(sys.argv[1]).read_text())
written_payload = json.loads(Path(sys.argv[2]).read_text())
assert stdout_payload["exit_code"] == 0
assert stdout_payload["schema_valid"] is True
assert "[dry-run]" in stdout_payload["response"]
assert written_payload == stdout_payload
PY

echo "[smoke] code review artifact-only"
python3 quality/scripts/llm-proxy/code_review.py \
  --scope package:tests/fixtures/go_repo/internal/auth \
  --artifact-only \
  --artifact-output "$TMP_DIR/code_review_artifact.txt" > /dev/null
python3 - <<'PY' "$TMP_DIR/code_review_artifact.txt"
import sys
from pathlib import Path
text = Path(sys.argv[1]).read_text(encoding="utf-8")
assert "NOTE: This is a synthetic full-code review artifact." in text
assert "diff --git a/tests/fixtures/go_repo/internal/auth/jwt.go b/tests/fixtures/go_repo/internal/auth/jwt.go" in text
PY

echo "[smoke] code review dry-run"
python3 quality/scripts/llm-proxy/code_review.py \
  --diff-file tests/fixtures/go_repo/review_artifact.txt \
  --provider gemini \
  --persona security \
  --dry-run \
  --output-file "$TMP_DIR/code_review_result.json" > "$TMP_DIR/code_review_result.stdout.json"
python3 - <<'PY' "$TMP_DIR/code_review_result.stdout.json" "$TMP_DIR/code_review_result.json"
import json, sys
from pathlib import Path
stdout_payload = json.loads(Path(sys.argv[1]).read_text())
written_payload = json.loads(Path(sys.argv[2]).read_text())
assert stdout_payload["contract_version"] == "ccr.reviewer_result.v1"
assert stdout_payload["llm_invocation"]["provider"] == "gemini"
assert stdout_payload["llm_invocation"]["schema_retries"] == 0
assert written_payload == stdout_payload
PY

echo "[smoke] code review verify dry-run"
python3 - <<'PY' "$TMP_DIR/verify_input.json"
import json, sys
from pathlib import Path
Path(sys.argv[1]).write_text(json.dumps({
    "file": "internal/auth/jwt.go",
    "diff_hunk": "@@ -1,1 +1,2 @@",
    "file_context": "return &TokenClaims{Subject: trimmed}, nil",
    "requirements": "ValidateToken must reject expired tokens.",
    "candidates": [
        {
            "candidate_id": "F1",
            "file": "internal/auth/jwt.go",
            "line": 24,
            "message": "ValidateToken returns claims without expiry validation."
        }
    ]
}, indent=2) + "\n", encoding="utf-8")
PY
python3 quality/scripts/llm-proxy/code_review_verify.py \
  --input-file "$TMP_DIR/verify_input.json" \
  --provider codex \
  --dry-run \
  --output-file "$TMP_DIR/verify_result.json" > "$TMP_DIR/verify_result.stdout.json"
python3 - <<'PY' "$TMP_DIR/verify_result.stdout.json" "$TMP_DIR/verify_result.json"
import json, sys
from pathlib import Path
stdout_payload = json.loads(Path(sys.argv[1]).read_text())
written_payload = json.loads(Path(sys.argv[2]).read_text())
assert stdout_payload["contract_version"] == "ccr.verification_result.v1"
assert stdout_payload["verified_findings"][0]["verdict"] == "uncertain"
assert stdout_payload["llm_invocation"]["provider"] == "codex"
assert stdout_payload["llm_invocation"]["schema_retries"] == 0
assert written_payload == stdout_payload
PY

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

echo "[smoke] verification prepare helper"
printf '' > "$TMP_DIR/requirements.txt"
python3 quality/scripts/ccr_verify_prepare.py \
  --candidates-file "$TMP_DIR/candidates.json" \
  --artifact-file tests/fixtures/go_repo/review_artifact.txt \
  --project-dir tests/fixtures/go_repo \
  --requirements-file "$TMP_DIR/requirements.txt" \
  --verify-batch-dir "$TMP_DIR/verify_batches" \
  --output-file "$TMP_DIR/verification_prepare.json" > "$TMP_DIR/verification_prepare.stdout.json"
python3 - <<'PY' "$TMP_DIR/verification_prepare.stdout.json" "$TMP_DIR/verification_prepare.json"
import json, sys
from pathlib import Path
stdout_payload = json.loads(Path(sys.argv[1]).read_text())
written_payload = json.loads(Path(sys.argv[2]).read_text())
assert stdout_payload["contract_version"] == "ccr.verification_prepare.v1"
assert written_payload["summary"]["ready_count"] == 1
assert written_payload["summary"]["batch_count"] == 1
assert written_payload["ready_candidates"][0]["anchor_status"] == "file_context"
assert Path(written_payload["batches"][0]["batch_file"]).is_file()
PY

echo "[smoke] requirements gating"
if python3 quality/scripts/ccr_run.py \
  package:internal/auth \
  --project-dir tests/fixtures/go_repo \
  --dry-run \
  --base-dir "$TMP_DIR/phase1-missing" > "$TMP_DIR/ccr_run_missing.stdout.json" 2> "$TMP_DIR/ccr_run_missing.stderr.json"; then
  echo "expected ccr_run.py to reject missing requirements" >&2
  exit 1
fi
python3 - <<'PY' "$TMP_DIR/ccr_run_missing.stderr.json"
import json, sys
from pathlib import Path
payload = json.loads(Path(sys.argv[1]).read_text())
assert "requires non-empty requirements/spec input before launch" in payload["error"]
PY

echo "[smoke] deterministic harness"
python3 quality/scripts/ccr_run.py \
  package:internal/auth \
  --project-dir tests/fixtures/go_repo \
  --requirements-text "ValidateToken must reject malformed input and preserve auth invariants." \
  --dry-run \
  --base-dir "$TMP_DIR/phase1" > "$TMP_DIR/ccr_run_summary.json"

python3 - <<'PY' "$TMP_DIR/ccr_run_summary.json"
import json, sys
from pathlib import Path
summary = json.loads(Path(sys.argv[1]).read_text())
status = json.loads(Path(summary["status_file"]).read_text())
run_metrics = json.loads(Path(summary["run_metrics_file"]).read_text())
reviewers = json.loads(Path(summary["reviewers_file"]).read_text())
trace_lines = [json.loads(line) for line in Path(summary["trace_file"]).read_text().splitlines() if line.strip()]
assert summary["contract_version"] == "ccr.run_summary.v1"
assert status["contract_version"] == "ccr.run_status.v1"
assert run_metrics["contract_version"] == "ccr.run_metrics.v1"
assert status["state"] == "completed"
assert Path(summary["trace_file"]).is_file()
verification_prepare = json.loads(Path(summary["verification_prepare_file"]).read_text())
assert verification_prepare["contract_version"] == "ccr.verification_prepare.v1"
assert verification_prepare["summary"]["batch_count"] == 0
assert run_metrics["route"]["total_passes"] == 14
assert reviewers["summary"]["llm_call_count"] == 14
assert reviewers["passes"][0]["llm_invocation"]["provider"] in {"gemini", "codex", "claude"}
assert run_metrics["llm"]["total_calls"] == 14
assert run_metrics["reviewers"]["provider_breakdown"]["gemini"]["call_count"] == 5
reviewer_events = [entry for entry in trace_lines if entry["event"] == "reviewer_completed"]
assert reviewer_events
assert reviewer_events[0]["data"]["llm_invocation"]["schema_retries"] == 0
PY

echo "[smoke] run report"
python3 quality/scripts/ccr_report.py \
  --summary-file "$TMP_DIR/ccr_run_summary.json" > "$TMP_DIR/ccr_report.txt"
python3 - <<'PY' "$TMP_DIR/ccr_report.txt"
import sys
from pathlib import Path
text = Path(sys.argv[1]).read_text(encoding='utf-8')
assert "Plan: Review plan:" in text
assert "Funnel: reviewers 14/14" in text
assert "Anomalies: none" in text
PY

echo "[smoke] detached harness + watch"
python3 quality/scripts/ccr_run.py \
  package:internal/auth \
  --project-dir tests/fixtures/go_repo \
  --requirements-text "ValidateToken must reject malformed input and preserve auth invariants." \
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
run_metrics = json.loads(Path(launch["run_metrics_file"]).read_text())
verification_prepare = json.loads(Path(launch["verification_prepare_file"]).read_text())
assert summary["contract_version"] == "ccr.run_summary.v1"
assert status["contract_version"] == "ccr.run_status.v1"
assert run_metrics["contract_version"] == "ccr.run_metrics.v1"
assert run_metrics["llm"]["total_calls"] == 14
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
    "approved_finding_numbers": [1],
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
approval = json.loads(Path(manifest["posting_approval_file"]).read_text())
assert prepared["contract_version"] == "ccr.posting_manifest.v1"
assert prepared["summary"]["ready_count"] == 1
assert prepared["summary"]["status_counts"]["ready"] == 1
assert approval["project"] == "group/project"
assert approval["mr_iid"] == 200
assert approval["approved_all"] is False
assert approval["source"] == "user_selection"
assert approval["approved_at"]
assert Path(manifest["posting_manifest_file"]).is_file()
PY
python3 - <<'PY' "$TMP_DIR/fake_glab"
import sys
from pathlib import Path
script = Path(sys.argv[1])
script.write_text(
    "#!/usr/bin/env python3\n"
    "import json\n"
    "import sys\n"
    "is_post = '-X' in sys.argv and sys.argv[sys.argv.index('-X') + 1] == 'POST'\n"
    "if is_post:\n"
    "    sys.stdout.write(json.dumps({'id': 'discussion-1', 'notes': [{'id': 42, 'type': 'DiffNote', 'body': 'Posted.'}] }))\n"
    "else:\n"
    "    sys.stdout.write('[]')\n",
    encoding="utf-8",
)
script.chmod(0o755)
PY
python3 quality/scripts/ccr_post_comments.py \
  --manifest-file "$TMP_DIR/phase2_manifest.json" \
  --glab-bin "$TMP_DIR/fake_glab" \
  --apply > "$TMP_DIR/posting_apply.json"
python3 - <<'PY' "$TMP_DIR/posting_apply.json"
import json, sys
from pathlib import Path
payload = json.loads(Path(sys.argv[1]).read_text())
assert payload["contract_version"] == "ccr.posting_result.v1"
assert payload["posted_count"] == 1
assert payload["summary"]["ready_resolution_rate"] == 1.0
assert payload["summary"]["status_counts"]["posted"] == 1
assert payload["summary"]["total_attempts"] == 1
PY

echo "[smoke] local eval runner"
./scripts/evals.sh --suite routing --case small --output-dir "$TMP_DIR/evals" > "$TMP_DIR/evals.stdout.json"
python3 - <<'PY' "$TMP_DIR/evals.stdout.json" "$TMP_DIR/evals/summary.json"
import json, sys
from pathlib import Path
stdout_payload = json.loads(Path(sys.argv[1]).read_text())
written_payload = json.loads(Path(sys.argv[2]).read_text())
assert stdout_payload["contract_version"] == "ccr.eval_summary.v1"
assert stdout_payload["passed_count"] == 1
assert stdout_payload["failed_count"] == 0
assert stdout_payload == written_payload
PY

echo "[smoke] eval scaffold from run"
RUN_DIR="$(python3 - <<'PY' "$TMP_DIR/ccr_run_summary.json"
import json, sys
from pathlib import Path
summary = json.loads(Path(sys.argv[1]).read_text())
print(summary['run_dir'])
PY
)"
python3 quality/scripts/ccr_eval.py \
  --from-run "$RUN_DIR" \
  --suite all \
  --case-name smoke-scaffold \
  --scaffold-dir "$TMP_DIR/eval_scaffold" > "$TMP_DIR/eval_scaffold.stdout.json"
python3 - <<'PY' "$TMP_DIR/eval_scaffold.stdout.json" "$TMP_DIR/eval_scaffold"
import json, sys
from pathlib import Path
payload = json.loads(Path(sys.argv[1]).read_text())
root = Path(sys.argv[2])
assert payload["contract_version"] == "ccr.eval_scaffold.v1"
assert payload["case_count"] == 3
assert (root / "routing_cases/smoke-scaffold/case.json").is_file()
assert (root / "consolidation_cases/smoke-scaffold/case.json").is_file()
assert (root / "verification_prepare_cases/smoke-scaffold/case.json").is_file()
PY

echo "[smoke] ok"
