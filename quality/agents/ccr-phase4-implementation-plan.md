# CCR Phase 4 Implementation Plan

## Goal

Make CCR runs measurable, inspectable, and regression-testable **without depending on CI as part of the initial rollout**.

Phase 4 should take the observability foundation already added in Phases 1.x and turn it into a first-class telemetry and eval layer:

- persist LLM/provider telemetry that already exists in `llm_proxy.py`
- aggregate run-level metrics into explicit artifacts
- enrich traces/status with enough structured detail to debug bad runs
- add repo-local eval fixtures and a deterministic eval runner

This plan intentionally **excludes CI integration** for now. CI wiring can be added later once the artifacts, fixtures, and local runner are stable.

Suggested release target after the Phase 4 non-CI work: **`v0.6.0`**.

---

## Current state

CCR already has a good Phase 1/2/3 base:

- `quality/scripts/ccr_run.py` is the deterministic top-level orchestrator
- `status.json`, `trace.jsonl`, `run_summary.json`, and detached watch flows already exist
- reviewer pass status already records some execution metadata such as:
  - `provider`
  - `exit_code`
  - `timed_out`
  - `duration_ms`
- verifier batch status already records:
  - `provider`
  - `attempted_providers`
  - `exit_code`
  - `timed_out`
  - `duration_ms`
- `llm_proxy.py` and the adapters already know more than CCR currently persists:
  - `tokens`
  - `duration_ms`
  - `exit_code`
  - `timed_out`
  - `schema_valid`
  - `schema_retries`
  - `schema_violations`
  - `thread_id`

However, several production-grade observability gaps remain:

1. reviewer and verifier wrapper outputs currently drop most `llm_proxy.py` telemetry
2. there is no dedicated run-scoped metrics artifact such as `run_metrics.json`
3. `reviewers.json` is emitted but still is not treated as a first-class contract schema
4. `trace.jsonl` contains useful events, but the event payloads do not yet capture the full provider/schema-repair story
5. there is no repo-local eval corpus or runner for routing/consolidation/verification-prep/posting regressions
6. some planned metrics (for example publish rate by persona) exist only implicitly across multiple artifacts rather than in one explicit summary
7. CI is mentioned in the main roadmap, but the local eval substrate needed before CI still does not exist

---

## Explicit scope for this phase

### In scope

1. Persist LLM telemetry from `llm_proxy.py` into reviewer/verifier artifacts
2. Enrich run artifacts with aggregate metrics and provider-level summaries
3. Add missing contracts for observability artifacts that CCR already emits or should emit
4. Add deterministic repo-local eval fixtures and a local eval runner
5. Update docs to explain the new telemetry/eval artifacts and local workflows

### Out of scope

1. CI integration / GitHub Actions / pipeline gating
2. Feedback-driven route tuning (that belongs to Phase 5)
3. Broad routing policy changes unrelated to observability/evals
4. Replacing the current watcher UX with a new transport or UI layer

---

## Constraints and design goals

1. **Keep `ccr_run.py` as the top-level orchestrator.**
   - Phase 4 should enrich artifacts and metrics, not undo the deterministic harness.

2. **Prefer additive contracts over breaking changes.**
   - Existing Phase 2/3 posting and report flows should keep working.

3. **Do not make local validation depend on live providers.**
   - The default eval runner should be deterministic and runnable offline.

4. **Keep monitor/watch output compact.**
   - Telemetry should land in artifacts and trace payloads; watcher text should stay concise.

5. **Make every important runtime decision reconstructable from artifacts.**
   - Reviewer failures, verifier rejections, anchor drops, and schema retries should be inspectable after the run.

6. **Separate run metrics from post-run side effects.**
   - Metrics available only after posting should be written by posting helpers rather than silently stuffed into pre-post run summaries.

---

## Proposed deliverables

### 1. Persist first-class LLM invocation telemetry

Introduce a reusable telemetry shape, for example:

```json
{
  "provider": "codex",
  "thread_id": "thread-123",
  "tokens": 1842,
  "duration_ms": 41234,
  "exit_code": 0,
  "timed_out": false,
  "schema_valid": true,
  "schema_retries": 1,
  "schema_violations": []
}
```

This should be persisted anywhere CCR currently stores reviewer/verifier structured outputs.

#### Likely file changes

- `quality/scripts/llm-proxy/code_review.py`
- `quality/scripts/llm-proxy/code_review_verify.py`
- `quality/contracts/v1/reviewer_result.schema.json`
- `quality/contracts/v1/verification_result.schema.json`
- new reusable schema:
  - `quality/contracts/v1/llm_invocation.schema.json`

### 2. Add explicit run metrics artifact

Add a new run-scoped artifact:

- `run_metrics_file` → `run_metrics.json`

This should summarize the run at a machine-readable level without forcing consumers to scan `trace.jsonl`.

#### Recommended contents

- run identity + target metadata
- route metrics
- reviewer metrics
- candidate/consolidation metrics
- verification metrics
- posting metrics when available
- provider/token/latency aggregates
- schema-repair aggregates

#### Likely file changes

- `quality/scripts/ccr_run_init.py`
- `quality/scripts/ccr_run.py`
- `quality/contracts/v1/run_manifest.schema.json`
- `quality/contracts/v1/run_launch.schema.json`
- `quality/contracts/v1/run_summary.schema.json`
- new schema:
  - `quality/contracts/v1/run_metrics.schema.json`

### 3. Promote reviewer/verifier manifests to first-class contracts

Phase 4 is a good point to stop treating these artifacts as semi-internal JSON blobs.

#### Recommended additions

- `quality/contracts/v1/reviewers_manifest.schema.json`
- optionally `quality/contracts/v1/trace_event.schema.json` if event stabilization is worth the complexity

At minimum, `reviewers.json` should become schema-backed because it is already a central debugging artifact.

### 4. Enrich trace and status payloads

Do not spam the watcher text, but make sure the structured event payloads contain enough data for postmortem debugging.

#### Recommended enrichment

- reviewer completion events include:
  - `provider`
  - `tokens`
  - `schema_retries`
  - `schema_valid`
  - `timed_out`
  - `exit_code`
- verifier batch completion events include:
  - same telemetry fields
  - candidate count / accepted / uncertain / rejected counts
- stage-complete events include summary counters for:
  - duplicate merge rate
  - anchor failure count
  - dropped candidate count

### 5. Add repo-local eval fixtures and eval runner

Add a deterministic local eval substrate so we can regression-test CCR behavior without CI.

#### Recommended structure

- `evals/ccr/routing_cases/`
- `evals/ccr/consolidation_cases/`
- `evals/ccr/verification_prepare_cases/`
- `evals/ccr/posting_cases/`
- optional later:
  - `evals/ccr/reviewer_wrapper_cases/`
  - `evals/ccr/verifier_wrapper_cases/`

#### Recommended runner(s)

- `quality/scripts/ccr_eval.py`
- optional convenience wrapper:
  - `scripts/evals.sh`

The default runner should:

- run deterministic suites locally
- emit a structured summary, for example:
  - `evals/ccr/results/<timestamp>/summary.json`
- return non-zero on regression failures
- avoid live network/provider dependence by default

### 6. Add metrics that match the roadmap and current CCR architecture

Phase 4 should compute metrics that are actually actionable for CCR.

#### Required metrics

1. **Reviewer availability rate**
   - `succeeded_passes / planned_passes`

2. **Verifier rejection rate**
   - `rejected_candidates / ready_candidates`

3. **Schema retry rate**
   - `sum(schema_retries) / total_llm_calls`

4. **Duplicate merge rate**
   - `merged_duplicates / raw_source_findings`

5. **Anchor failure rate**
   - `missing_anchor_candidates / candidate_count`

6. **Latency and token usage**
   - total runtime
   - reviewer/verifier per-pass duration
   - token totals by provider

7. **Posting approval/publish metrics**
   - approved finding count
   - posted / already_posted / failed counts
   - optional persona/severity breakdowns

#### Important note on posting metrics

Posting happens after the run summary is written, so Phase 4 should treat posting metrics as a **posting artifact concern** first, not as a pre-post `run_summary.json` concern.

That suggests expanding `posting_results.json` with richer metrics and optionally writing a small post-run summary extension artifact later if needed.

---

## Proposed contract evolution

### New schema files

Add:

- `quality/contracts/v1/llm_invocation.schema.json`
- `quality/contracts/v1/run_metrics.schema.json`
- `quality/contracts/v1/reviewers_manifest.schema.json`
- optional:
  - `quality/contracts/v1/eval_result.schema.json`
  - `quality/contracts/v1/eval_summary.schema.json`

### Expanded schema files

Expand:

- `quality/contracts/v1/reviewer_result.schema.json`
- `quality/contracts/v1/verification_result.schema.json`
- `quality/contracts/v1/posting_result.schema.json`
- `quality/contracts/v1/run_manifest.schema.json`
- `quality/contracts/v1/run_launch.schema.json`
- `quality/contracts/v1/run_summary.schema.json`
- `quality/contracts/v1/run_status.schema.json`
- `quality/contracts/v1/README.md`

---

## Concrete implementation steps

### Step 1 — Contracts and artifact paths

#### Goals

- add new schema files
- surface `run_metrics_file` in manifest/launch/summary
- make `reviewers.json` a first-class schema-backed artifact

#### Deliverables

- `quality/contracts/v1/llm_invocation.schema.json`
- `quality/contracts/v1/run_metrics.schema.json`
- `quality/contracts/v1/reviewers_manifest.schema.json`
- expanded run schemas
- `quality/scripts/ccr_run_init.py` updated with `run_metrics_file`
- contract tests expanded

#### Validation

- `tests/test_contracts.py`
- `tests/test_ccr_run_init.py`

### Step 2 — Wrapper telemetry persistence

#### Goals

- preserve `llm_proxy.py` telemetry in reviewer and verifier outputs
- keep dry-run outputs shape-compatible with live outputs

#### Deliverables

- `code_review.py` includes a stable telemetry object in its output JSON
- `code_review_verify.py` does the same
- wrapper tests cover:
  - successful response
  - provider failure
  - dry-run shape
  - schema retry visibility

#### Validation

- add/expand tests such as:
  - `tests/test_code_review.py`
  - `tests/test_code_review_verify.py`
  - `tests/test_llm_proxy.py`

### Step 3 — Harness aggregation and run metrics

#### Goals

- carry reviewer/verifier telemetry through the harness
- write `run_metrics.json`
- enrich `status.json` pass/batch entries and `trace.jsonl` payloads

#### Deliverables

- reviewer pass entries include tokens/schema fields
- verifier batch entries include tokens/schema fields
- `run_metrics.json` aggregates:
  - reviewer availability
  - verifier rejection
  - duplicate merge rate
  - anchor failure rate
  - provider latency/tokens
- `run_summary.json` points to `run_metrics_file`

#### Validation

- expand:
  - `tests/test_ccr_run.py`
  - `tests/test_contracts.py`
  - `tests/test_ccr_watch.py` only as needed to ensure watcher compatibility

### Step 4 — Posting metrics and post-run observability

#### Goals

- make posting outcomes measurable, not just human-readable
- close the gap between approval, prepared payloads, and final posting results

#### Deliverables

- expand `posting_results.json` summary fields with richer counts/breakdowns
- add optional severity/persona breakdowns where data is available
- ensure approval/publish metrics are deterministic and testable

#### Validation

- expand:
  - `tests/test_ccr_post_comments.py`
  - `tests/test_contracts.py`

### Step 5 — Local eval fixtures and runner

#### Goals

- add repo-local eval data and a deterministic runner
- make it possible to regression-check CCR changes outside CI

#### Deliverables

- `evals/ccr/routing_cases/...`
- `evals/ccr/consolidation_cases/...`
- `evals/ccr/verification_prepare_cases/...`
- `evals/ccr/posting_cases/...`
- `quality/scripts/ccr_eval.py`
- optional `scripts/evals.sh`

#### Runner behavior

- `--suite routing|consolidation|verification_prepare|posting|all`
- deterministic exit codes
- structured JSON summary output
- optional artifact directory for failed case diffs

#### Validation

- add tests such as:
  - `tests/test_ccr_eval.py`
- optionally add one small eval smoke invocation to `scripts/smoke.sh`

### Step 6 — Docs and release

#### Deliverables

- update:
  - `README.md`
  - `quality/agents/ccr-improvement-plan.md`
  - `quality/contracts/v1/README.md`
- document:
  - where telemetry lands
  - how to read `run_metrics.json`
  - how to run local evals
  - what is intentionally deferred to CI later

#### Release target

- bump plugin/marketplace versions
- release as **`v0.6.0`**

---

## Recommended implementation order

If we want a low-risk landing sequence, use this order:

1. **Step 1** — contracts + `run_metrics_file`
2. **Step 2** — wrapper telemetry persistence
3. **Step 3** — harness aggregation + `run_metrics.json`
4. **Step 4** — posting metrics
5. **Step 5** — eval fixtures + runner
6. **Step 6** — docs + release

This keeps contracts/artifacts stable before we wire in aggregation and eval tooling.

---

## Definition of done for the non-CI Phase 4 scope

Phase 4 (non-CI) is complete when:

1. every reviewer/verifier structured output carries machine-readable LLM telemetry
2. every run writes a dedicated metrics artifact summarizing reviewer/verifier/provider behavior
3. reviewer and posting observability artifacts have stable, tested contracts
4. there is a deterministic local eval runner plus fixture suites for routing, consolidation, verification-prep, and posting
5. docs explain how to inspect telemetry and run evals locally
6. CI remains explicitly deferred, but the local substrate needed for future CI is already in place

---

## Recommended immediate next move

Start with **Step 1 — contracts and artifact paths**.

That gives us a stable place to put telemetry and metrics before we thread new data through `code_review.py`, `code_review_verify.py`, and `ccr_run.py`.
