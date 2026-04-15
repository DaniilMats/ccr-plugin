# CCR Contracts v1

Versioned JSON Schema contracts introduced during Phase 0.

These schemas document the intended stable shapes for CCR runtime artifacts and
are the foundation for moving more orchestration into deterministic Python code.

Current schemas:

- `run_manifest.schema.json`
- `route_input.schema.json`
- `route_plan.schema.json`
- `run_status.schema.json`
- `run_summary.schema.json`
- `run_launch.schema.json`
- `watch_result.schema.json`
- `static_analysis.schema.json`
- `llm_invocation.schema.json`
- `reviewer_result.schema.json`
- `reviewers_manifest.schema.json`
- `consolidated_candidate.schema.json`
- `candidates_manifest.schema.json`
- `verification_prepare.schema.json`
- `verification_batch.schema.json`
- `verification_result.schema.json`
- `verified_findings.schema.json`
- `run_metrics.schema.json`
- `posting_approval.schema.json`
- `posting_manifest.schema.json`
- `posting_result.schema.json`

Notes:
- `watch_cursor.json` is a run-scoped watcher cursor/state file used by `quality/scripts/ccr_watch.py` to suppress already-consumed progress updates during repeated polling or `--follow` sessions.
- `verification_prepare.json` is a run-scoped inspection artifact that summarizes which candidates were prepared for verifier execution and how they were batched.
- `reviewers.json` is a structured reviewers manifest emitted by `quality/scripts/ccr_run.py`.
- `run_metrics.json` is a run-scoped aggregate metrics artifact that summarizes routing/reviewer/candidate/verification/posting counters.
- The cursor file is intentionally lightweight adapter state and is not treated as a harness contract schema.
