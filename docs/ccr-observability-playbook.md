# CCR Observability Playbook

Use this playbook to debug a single CCR run, compare runs across releases, and turn real incidents into deterministic eval cases.

## Where the artifacts live

Each CCR run gets an isolated run directory under `/tmp/ccr/<run_id>/`.

Useful ways to discover it:
- detached launches print a `ccr.run_launch.v1` payload with `run_dir`
- the run workspace contains `run_manifest.json` with artifact paths
- terminal summaries and watcher output can be traced back to the same run directory

Most observability and debugging starts from files in that run directory.

## Quick triage order

When a run looks wrong, inspect artifacts in this order:

1. **`run_summary.json`** — how the run ended
2. **`run_metrics.json`** — whether the run was healthy, noisy, slow, or expensive
3. **stage-specific artifacts** — why a finding survived, dropped, or failed to post
4. **`trace.jsonl`** — exact event order and stage transitions
5. **`status.json`** — current live state when the run is still in progress

This keeps debugging fast: start from the summary, then drill into the relevant stage instead of scanning raw traces first.

## Artifact map

| Artifact | When to use it | Primary question |
|---|---|---|
| `status.json` | during a live run | Where is CCR right now? |
| `trace.jsonl` | after or during a run | What happened, in what order? |
| `run_summary.json` | first stop after a run | How did the run finish? |
| `run_metrics.json` | first stop after a run | Was the run healthy, noisy, slow, or expensive? |
| `route_input.json` | routing diagnosis | What inputs drove persona selection and fanout? |
| `route_plan.json` | routing diagnosis | What plan did CCR choose? |
| `reviewers.json` | reviewer/provider diagnosis | How did each reviewer pass and provider behave? |
| `candidates.json` | consolidation diagnosis | What candidate findings were synthesized before verifier prep? |
| `verification_prepare.json` | verification diagnosis | Why did candidates become ready, drop, or split into batches? |
| `verified_findings.json` | final review diagnosis | What survived verification and with what evidence/status? |
| `posting_manifest.json` | posting diagnosis | What did CCR prepare to post? |
| `posting_results.json` | posting diagnosis | What actually posted, skipped, or failed? |
| `watch_cursor.json` | watcher behavior only | Why did the watcher suppress or skip already-consumed output? |

Notes:
- `watch_cursor.json` is watcher adapter state, not a primary review/debug artifact.
- Eval outputs under `evals/ccr/results/...` are local regression artifacts, not stable harness contracts.

## The CCR funnel

Most run debugging becomes easier if you think in one funnel:

```text
route_input/route_plan
  -> reviewers.json
  -> candidates.json
  -> verification_prepare.json
  -> verified_findings.json
  -> posting_manifest.json / posting_results.json
```

High-level interpretation:
- **reviewers** found raw signals
- **candidates** merged and normalized them
- **verification_prepare** decided which candidates were ready for verifier execution
- **verified_findings** captured verifier outcomes and final numbered findings
- **posting artifacts** show what was prepared and what side effects actually happened

If a finding "disappears", it usually disappeared at one of those transitions.

## How to read the main artifacts

### `status.json`
Use it for live progress only.

Look for:
- current stage
- stage counters
- reviewer completion counts
- verification batch progress
- terminal state

Best fit:
- watcher / polling UX
- checking whether a run is stalled or still moving

### `trace.jsonl`
Use it when you need the exact timeline.

Look for:
- stage start/finish events
- reviewer started/completed events
- verification batch events
- failure transitions
- timestamps and ordered payloads

Best fit:
- reconstructing a flaky run
- confirming whether progress actually stalled
- understanding event order without guessing from transcript snippets

### `run_summary.json`
This is the shortest post-run answer.

Look for:
- overall status
- target/mode
- verified finding count
- report location
- pointers to other artifacts

Best fit:
- answering "what happened?" before deeper diagnosis

### `run_metrics.json`
This is the top-level health summary.

Look for:
- provider breakdowns
- total LLM calls / tokens / LLM duration
- schema retry and schema violation counts
- failed or timed-out calls
- duplicate merge count/rate
- anchor failure count/rate
- ready / dropped / confirmed / uncertain / rejected counts
- rejection rate
- posting-related counters when available

Best fit:
- release-to-release comparison
- deciding whether a run was noisy or genuinely broken
- identifying where to tune routing, prompting, or verification policy

### `reviewers.json`
Use this to inspect pass-level behavior.

Look for:
- each pass name and persona
- provider and duration
- timed out / failed status
- finding counts
- normalized `llm_invocation` telemetry
- provider aggregates in the manifest summary

Best fit:
- identifying weak or expensive passes
- checking whether a specific provider is flaky
- seeing whether a pass produced signal before blaming consolidation or verification

### `candidates.json`
Use this to inspect deterministic consolidation output.

Look for:
- merged candidates
- primary persona
- supporting personas and support counts
- dedupe behavior
- evidence bundles and merged context

Best fit:
- understanding whether duplicate reviewer findings merged correctly
- confirming whether corroboration was preserved before verifier prep

### `verification_prepare.json`
This explains verifier intake.

Look for:
- `ready_candidates`
- dropped candidates and `drop_reason`
- `anchor_status`
- batch membership and deterministic chunking
- prefilter decisions

Best fit:
- explaining why a candidate never reached the verifier
- spotting anchor-quality problems versus reviewer-quality problems

### `verified_findings.json`
Use this for final evidence-backed outcomes.

Look for:
- confirmed / uncertain / rejected outcomes
- stable `finding_number`
- verifier-adjusted file/line/message
- evidence/support metadata
- verification batch metadata

Best fit:
- understanding what actually survived verification
- explaining why the final report differs from raw reviewer output

### `posting_manifest.json` and `posting_results.json`
Use these only for MR-mode publish behavior.

Look for:
- prepared payload counts
- approved finding numbers
- invalid approvals
- posted / already_posted / skipped / failed counts
- missing anchor counts
- persona and severity breakdowns
- per-finding prepared status and final posting result

Best fit:
- confirming side-effect safety
- proving idempotency worked
- diagnosing failed or skipped MR comments

## Symptom-based playbook

### 1. The run is slow or flaky

Start with:
- `run_metrics.json`
- `reviewers.json`
- `trace.jsonl`

Check for:
- high total `duration_ms`
- timed-out reviewer or verifier calls
- a provider with repeated failures
- high `schema_retries` or schema violations
- one pass or provider dominating latency without producing useful findings

Typical interpretation:
- many retries or schema violations -> prompt/schema friction
- repeated provider failures -> transport/provider instability
- one consistently slow persona/pass -> routing or provider mix may need tuning

### 2. A reviewer found something, but it disappeared later

Follow the funnel:
1. `reviewers.json` — did any pass actually emit the finding?
2. `candidates.json` — was it merged, deduped, or normalized away?
3. `verification_prepare.json` — was it dropped before verifier execution, and why?
4. `verified_findings.json` — did the verifier reject or downgrade it?
5. `posting_results.json` — if MR mode, did it survive but fail to publish?

Most common causes:
- duplicate merge into another candidate
- `drop_reason` during verification prep
- missing or weak anchors
- verifier rejection or `uncertain`
- posting skipped because the finding was not `ready` or already posted

### 3. The verifier rejects too much

Start with:
- `run_metrics.json`
- `verification_prepare.json`
- `verified_findings.json`

Check for:
- high `rejection_rate`
- many `uncertain` outcomes
- many dropped candidates before verifier execution
- high `anchor_failure_rate`

Typical interpretation:
- high rejection with healthy anchors -> reviewer noise or overly broad routing
- high anchor failure -> anchoring/mapping issue, not necessarily model quality
- many dropped candidates -> prefilters may be too aggressive, or reviewer evidence is too weak

### 4. Posting did not happen or looked unsafe

Start with:
- `run_summary.json`
- `posting_manifest.json`
- `posting_results.json`

Check for:
- was this an MR run?
- which findings were approved?
- was `prepared_status` equal to `ready`?
- did idempotency mark findings as `already_posted`?
- were anchors missing?
- did any `glab` calls fail?

Typical interpretation:
- `already_posted_count` rising -> idempotency is protecting you from duplicates
- `missing_anchor_count` rising -> posting safety is working, but anchor quality needs work earlier in the funnel
- `failed_count` rising -> inspect posting transport or prepared payload validity

### 5. Routing looks wrong for the size or risk of the change

Start with:
- `route_input.json`
- `route_plan.json`
- `run_metrics.json`
- `reviewers.json`

Check for:
- changed file count and changed line count
- triggered personas and critical surfaces
- whether `full_matrix` was selected
- planned versus completed pass counts
- whether expensive passes actually produced useful findings

Typical interpretation:
- too much fanout on small/simple changes -> routing thresholds may be too aggressive
- too little fanout on risky/auth/concurrency changes -> routing inputs may miss critical surfaces
- a persona that rarely contributes confirmed findings -> candidate for later tuning in Phase 5 work

## Comparing runs between releases

`run_metrics.json` is the best starting point for release comparison.

Useful fields to compare across a small sample of similar runs:
- total duration
- total reviewer/verifier calls
- provider failure rate
- schema retry / schema violation counts
- duplicate merge rate
- anchor failure rate
- rejection rate
- confirmed findings per run
- posted / failed publish counts

A few common interpretations:
- lower duration with stable confirmed findings -> likely improvement
- lower duplicate merge rate -> consolidation likely improved
- higher anchor failure rate -> artifact mapping likely regressed
- higher rejection rate -> reviewers may have become noisier, or verifier policy may have tightened

## Turning a real incident into a deterministic eval

Use observability artifacts to turn production pain into local regression coverage.

Recommended loop:
1. observe the bad behavior on a real run
2. identify the failing stage from the run artifacts
3. extract the minimal deterministic inputs for that stage
4. create a fixture under the matching suite in `evals/ccr/`
5. write `expected.json`
6. run `./scripts/evals.sh --suite <suite>`
7. keep the fixture after the bug is fixed

Suggested mapping:
- routing issue -> `evals/ccr/routing_cases/`
- consolidation issue -> `evals/ccr/consolidation_cases/`
- verifier-prep issue -> `evals/ccr/verification_prepare_cases/`
- posting issue -> `evals/ccr/posting_cases/`

This is the main long-term value of CCR observability: each confusing run can become a stable offline regression case.

## What this layer does not do yet

CCR now has strong JSON-first observability, but not a dashboard.

That means:
- the artifacts are already good enough for debugging and regression work
- they are intentionally machine-readable and deterministic
- a separate human-summary helper or dashboard could still be added later on top of them

## Recommended team habit

For any suspicious run, save at least:
- `run_summary.json`
- `run_metrics.json`
- `trace.jsonl`
- the stage-specific artifact that explains the issue

For any real bug, prefer converting it into an eval fixture instead of relying on memory or transcript snippets.
