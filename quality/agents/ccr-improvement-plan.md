# CCR Improvement Plan

## Goal

Evolve CCR from a **prompt-orchestrated review workflow** into a **production-grade review system** with:

- deterministic orchestration
- explicit approval boundaries for side effects
- evidence-backed verification
- durable traces and run manifests
- regression evals and CI coverage
- feedback-driven routing and reviewer quality metrics

This plan is based on the current repository state in `~/ccr-plugin` and on production patterns commonly used in agent review systems: **generator/reviewer separation, deterministic verifiers, HITL approval gates, structured traces, and continuous eval loops**.

## Progress note

As of 2026-04-15:
- Phase 0 is complete: isolated run workspaces + versioned contracts
- Phase 0.5 is complete: deterministic tests/fixtures + local smoke harness
- Phase 1 is complete: deterministic harness entrypoint at `quality/scripts/ccr_run.py`
- Phase 1.1 is complete: live stderr progress, `status.json`, `trace.jsonl`, `run_summary.json`, and configurable parallelism/timeout budgeting for the harness
- Phase 1.2 is complete: detached/background harness launch plus polling via `quality/scripts/ccr_watch.py`
- Phase 1.3 is complete: compact/quiet watcher output, cursor-managed progress polling, `/loop`/scheduled-task limits documented, and Monitor-first live UX
- Phase 1.3.1 is complete: clearer icon-prefixed watcher lines and stricter no-filler monitor guidance
- Phase 2 is complete: deterministic `ccr_post_comments.py`, explicit `posting_approval.json` / `posting_manifest.json` / `posting_results.json`, fingerprint-based idempotency, and validated `DiffNote` posting flow
- Phase 3 is complete: deterministic `ccr_consolidate.py` + `ccr_verify_prepare.py`, richer candidate/verified-finding contracts, machine-readable evidence bundles, anchor status, deterministic prefilters, and structured verification-prep artifacts
- Phase 4 Step 1 is complete: `llm_invocation` / `reviewers_manifest` / `run_metrics` contracts plus `run_metrics_file` surfaced across manifest/launch/summary/watch artifacts
- Phase 4 Step 2 is complete: reviewer/verifier wrapper outputs now carry normalized `llm_invocation` telemetry with provider, tokens, duration, thread, and schema-retry metadata
- Phase 4 Step 3 is complete: `ccr_run.py` now propagates reviewer/verifier telemetry into `reviewers.json`, `status.json`, `trace.jsonl`, and richer `run_metrics.json` aggregates (provider breakdowns, schema retry counts, duplicate merge rate, rejection rate, anchor failure rate)
- Phase 4 Step 4 is complete: `ccr_post_comments.py` now writes richer `posting_results.json` summaries with prepared/apply counts, per-status publish metrics, attempt totals, and persona/severity breakdowns for deterministic post-run observability
- Detailed Phase 2 implementation plan: `quality/agents/ccr-phase2-implementation-plan.md`
- Detailed Phase 3 implementation plan: `quality/agents/ccr-phase3-implementation-plan.md`
- Detailed Phase 4 implementation plan (non-CI rollout): `quality/agents/ccr-phase4-implementation-plan.md`

---

## Current CCR architecture snapshot

Today the flow is roughly:

```text
user
  -> quality/agents/ccr.md
  -> quality/scripts/ccr_run.py
  -> quality/scripts/ccr_routing.py
  -> review_context.py + static_analysis.py
  -> N reviewer passes via code_review.py
  -> quality/scripts/ccr_consolidate.py
  -> quality/scripts/ccr_verify_prepare.py
  -> verification + numbered report
  -> AskUserQuestion
  -> quality/scripts/ccr_post_comments.py
```

### What is already strong

CCR already has several good production foundations:

1. **Model diversity**
   - Gemini / Codex / Claude Opus fanout is a real strength.
   - Logic always gets triple coverage.

2. **Reviewer/verifier separation**
   - `code_review.py` produces candidate findings.
   - `code_review_verify.py` provides a second-stage verification pass.

3. **Routing is partially externalized into code**
   - `quality/scripts/ccr_routing.py` is already a concrete step away from pure prompt orchestration.

4. **Schema-validated reviewer outputs**
   - `llm_proxy.py` supports schema retries.
   - `code_review_response.schema.json` and `code_review_verification_response.schema.json` give CCR a structured contract.

5. **Human approval before side effects**
   - MR comment posting is at least conceptually behind an approval gate.

6. **Repository-aware context + static analysis**
   - `review_context.py` and `static_analysis.py` give the reviewers more signal than raw diff-only review.

These are the right building blocks. The main issue is that too much of the critical control flow still lives in the agent prompt instead of deterministic runtime code.

---

## Highest-value gaps vs production-grade review systems

### 1. Core orchestration is prompt-defined, not runtime-enforced

Most important behavior still lives in `quality/agents/ccr.md`:

- review target classification
- route input construction
- reviewer spawning
- consolidation / dedupe / consensus logic
- verification batching
- report construction
- posting flow

This makes CCR harder to:

- test
- replay
- debug
- measure
- safely evolve

**Production-grade direction:** keep the LLM for reviewing code, but move orchestration into deterministic code.

---

### 2. Fixed `/tmp/ccr_*` paths make runs non-isolated

Current flow uses shared fixed filenames such as:

- `/tmp/ccr_mr_diff.txt`
- `/tmp/ccr_review_context.md`
- `/tmp/ccr_static_analysis.json`
- `/tmp/ccr_verify_batch_<N>.json`

This is fragile for:

- concurrent runs
- retries
- resume flows
- auditability
- reproducing a specific review session

**Production-grade direction:** per-run workspaces and manifests.

---

### 3. “Post-once guarantee” is mostly a prompt promise, not a coded invariant

`ccr.md` says comments are posted once and never double-posted, but there is no deterministic poster helper in the repo that enforces:

- idempotency keys
- existing-comment detection
- anchor validation
- retry policy with safe dedupe

**Production-grade direction:** a dedicated posting helper with deterministic idempotency.

---

### 4. Requirements review is an outlier path

Requirements passes are currently prompt-rendered `Task(general-purpose)` calls instead of going through the same wrapper path as the other reviewers.

That means they do **not** get the same:

- CLI wrapper contract
- schema retry behavior
- standardized telemetry
- deterministic input/output path

**Production-grade direction:** unify requirements review under the same executable harness.

---

### 5. Deterministic signals are advisory, not first-class

`static_analysis.py` is useful, but today it mainly feeds text into prompts.

Missing production behaviors:

- deterministic candidate generation from static analysis
- confidence boost from concrete tool evidence
- hard rejection of claims that do not map to a file/line/evidence hunk
- optional compile/test checks in the verification lane

**Production-grade direction:** deterministic verifiers should be a first-class lane, not prompt context only.

---

### 6. No durable traces, no evals, no regression harness in repo

The repo currently references assets that do not exist:

- `evals/ccr/`
- `tests/test_ccr_evals.py`
- `tests/test_ccr_routing.py`
- `quality/agents/ccr-improvement-plan.md` (this file now fills that gap)

Also:

- `review_context.py` references `scripts/repomap.py`, but that file is absent.
- `ccr.md` references `MEMORY.md`, but no such file exists.

This is both a reliability issue and a documentation drift issue.

**Production-grade direction:** add real eval/test assets and align docs with actual code.

---

### 7. Reviewer execution is not least-privilege enough

The review passes are launched as `Task(general-purpose)` background agents. Even though the prompt tells them to call `code_review.py`, the execution model is still broader than necessary.

For a production review system, reviewer workers should be as close as possible to:

- read-only
- deterministic
- non-side-effecting
- scoped to one artifact and one output schema

**Production-grade direction:** run reviewer CLIs via a deterministic subprocess pool where possible; reserve the agent prompt for coordination and human interaction.

---

### 8. Risk routing still depends on prompt-layer interpretation

`ccr_routing.py` is solid, but the construction of `triggered_personas`, `highest_risk_personas`, and `critical_surfaces` is still mostly prompt-level behavior.

That creates avoidable routing drift.

**Production-grade direction:** compute more of the risk profile deterministically from:

- file paths
- diff stats
- package names
- known critical-surface patterns
- static analysis signals
- optional ownership/config rules

---

## Roadmap

---

## Phase 0 — Stabilize the current contract

**Priority:** P0  
**Target:** 1–2 days

### Deliverables

1. Introduce a `run_id` and per-run workspace, e.g.
   - `/tmp/ccr/<run_id>/...`
   - or `~/.claude/ccr/runs/<run_id>/...`

2. Replace all fixed `/tmp/ccr_*` artifacts with run-scoped paths.

3. Resolve current spec drift:
   - normalize **4–10 vs 4–14** pass count references
   - normalize **10-minute vs 15-minute** timeout references
   - either add or remove references to missing assets:
     - `repomap.py`
     - `MEMORY.md`
     - `evals/ccr/`
     - `tests/...`

4. Version the core JSON contracts:
   - route input
   - route plan
   - reviewer result
   - consolidated candidate
   - verifier batch
   - verifier result
   - posting manifest

### Why this phase matters

This phase removes the most immediate operational fragility **without** redesigning the whole system.

### Definition of done

- Two CCR runs can execute concurrently without clobbering each other.
- README and `quality/agents/ccr.md` agree on pass count and timeout rules.
- Every referenced critical asset either exists or is no longer referenced.

---

## Phase 1 — Move orchestration into a deterministic harness

**Priority:** P0 / P1  
**Target:** ~1 week

### Deliverables

Add a new runtime entrypoint, for example:

- `quality/scripts/ccr_run.py`

The harness should own:

1. review target detection
2. MR metadata + diff fetch / local artifact generation
3. run workspace creation
4. risk profile generation
5. route planning via `ccr_routing.py`
6. repository context generation
7. static analysis execution
8. reviewer subprocess pool execution
9. candidate collection
10. verification batch preparation
11. verified output manifest generation
12. final report JSON/markdown artifact generation

### Important design choice

Shrink `quality/agents/ccr.md` so it mostly does:

- collect user input
- call deterministic harness
- present report
- ask for publish approval
- call deterministic poster

### Also do in this phase

Unify **Requirements** review under the same wrapper contract as other reviewers.

Possible additions:

- `quality/scripts/ccr_requirements_review.py`
- or extend `code_review.py` so requirements review is just another wrapped mode

### Why this phase matters

This is the biggest step from “smart prompt” to “review runtime”. It makes CCR:

- testable
- replayable
- easier to debug
- less dependent on agent prompt discipline

### Definition of done

A single deterministic command can run a full CCR review and produce at least:

- `run_manifest.json`
- `route_plan.json`
- `reviewers.json`
- `candidates.json`
- `verified_findings.json`
- `report.md`

---

## Phase 2 — Add real guardrails around side effects

**Priority:** P0 / P1  
**Target:** 4–5 days

### Deliverables

Add a dedicated poster helper, e.g.

- `quality/scripts/ccr_post_comments.py`

It should own:

1. GitLab DiffNote anchor construction
2. payload generation
3. idempotency key / comment fingerprint generation
4. existing comment lookup
5. retry policy
6. response validation (`type == DiffNote`)
7. per-comment posting result manifest

### Recommended idempotency strategy

Fingerprint each approved finding by something like:

- project
- MR IID
- file
- line
- normalized message hash

Store post results in the run manifest and optionally look up existing matching notes before posting.

### Additional guardrails

1. Treat comment posting as a **separate execution stage**.
2. Approval output should become a deterministic input file, not implicit agent memory.
3. Keep reviewer execution read-only and non-side-effecting.
4. Long-term: reduce reliance on `Task(general-purpose)` for reviewer workers.

### Why this phase matters

This creates the approval boundary that production agent systems usually enforce:

**review generation** != **side effect execution**.

### Definition of done

- Rerunning the same approved publish set does not double-post.
- The prompt no longer manually constructs raw `glab ... discussions` posting behavior.
- Posting is fully reproducible from a manifest file.

---

## Phase 3 — Make consolidation and verification evidence-based

**Priority:** P1  
**Target:** ~1 week  
**Status:** Complete in `v0.5.0`

### Deliverables

Add deterministic helpers such as:

- `quality/scripts/ccr_consolidate.py`
- `quality/scripts/ccr_verify_prepare.py`

### Move these responsibilities out of the prompt

1. persona tagging
2. dedupe
3. consensus scoring
4. candidate ID assignment
5. evidence packing
6. verifier batch creation
7. post-verifier acceptance rules

### Improve the data model

Each candidate should carry explicit evidence fields, for example:

```json
{
  "candidate_id": "F12",
  "persona": "security",
  "reviewers": ["security_p1", "security_p2"],
  "consensus": "2/2",
  "file": "internal/auth/jwt.go",
  "line": 42,
  "symbol": "ValidateToken",
  "anchor_status": "diff|file_context|missing",
  "evidence_sources": ["diff_hunk", "gosec"],
  "message": "..."
}
```

### Deterministic prefilters to add

Before the LLM verifier runs:

- file exists
- line maps to diff or local file context
- candidate has an evidence hunk
- candidate has at least one concrete source (reviewer text, static analysis finding, requirement mismatch, etc.)

### Dedupe improvement

Replace pure `file + line ±3` heuristics with something closer to:

- `file + symbol + normalized rule category`

Examples:

- same file + same function + same root cause -> merge
- same line but unrelated performance/security observations -> keep separate

### Deterministic evidence lanes to promote

Make these first-class inputs, not prompt garnish:

- `go vet`
- `staticcheck`
- `gosec`
- optional targeted `go test` / `go test <package>`
- optional diff anchor checks

### Why this phase matters

Production review systems work best when **deterministic evidence is evaluated before or alongside LLM judgment**.

### Definition of done

- Every verified finding has a machine-readable evidence bundle.
- Candidates without anchors/evidence are dropped before human review.
- Dedupe/consolidation behavior is deterministic and unit-testable.

---

## Phase 4 — Add observability, traces, and evals

**Priority:** P1  
**Target:** 1–2 weeks  
**Status:** Planned — local observability/evals first, CI explicitly deferred for the initial rollout

### Deliverables

Persist telemetry already available from `llm_proxy.py` and adapters:

- provider
- tokens
- duration_ms
- exit_code
- timed_out
- schema_valid
- schema_retries
- schema_violations

Add a run trace / manifest, for example:

- `run_manifest.json`
- `trace.jsonl`

### Add real test coverage

Recommended test files:

- `tests/test_ccr_routing.py`
- `tests/test_ccr_consolidation.py`
- `tests/test_ccr_verify_prepare.py`
- `tests/test_ccr_post_comments.py`
- `tests/test_code_review_wrapper.py`

### Add real eval fixtures

Recommended structure:

- `evals/ccr/routing_cases/`
- `evals/ccr/verifier_cases/`
- `evals/ccr/consolidation_cases/`
- `evals/ccr/posting_cases/`

Use real anonymized examples for:

- clear true positives
- clear false positives
- duplicate findings across personas
- wrong line anchors
- verifier over-rejection / under-rejection
- prompt drift regressions

### Metrics to start tracking

1. **Reviewer availability rate**
   - successful passes / planned passes

2. **Verifier rejection rate**
   - rejected candidates / all candidates

3. **Schema retry rate**
   - how often wrappers need schema repair

4. **Duplicate merge rate**
   - merged duplicates / raw findings

5. **Anchor failure rate**
   - candidates that cannot be mapped to valid diff/file positions

6. **Publish rate by persona**
   - approved-for-posting / displayed findings

7. **Latency and cost**
   - total runtime
   - per-pass duration
   - estimated tokens per provider

### Why this phase matters

This creates the trace/eval loop that production agent review systems rely on for continuous improvement.

### Definition of done

- CCR changes can be regression-tested in CI.
- Every run leaves behind enough data to debug routing, reviewer failures, verifier behavior, and posting outcomes.

---

## Phase 5 — Add feedback-driven routing and policy tuning

**Priority:** P2  
**Target:** ongoing

### Deliverables

Use human and runtime feedback to improve routing and review quality.

### Signals to collect

1. Which findings the user approved for posting
2. Which findings were skipped
3. Which posted comments were later edited/deleted manually
4. Which personas/models produce most rejected findings
5. Which route plans produce the best precision/latency tradeoff

### Improvements enabled by these signals

1. **Adaptive specialty routing**
   - allocate Pass 2 / Pass 3 based on actual historical yield

2. **Risk-policy tuning**
   - full matrix only for truly risky changes
   - more aggressive fanout for migrations/auth/public APIs

3. **Reviewer scorecards**
   - precision proxy by persona/model
   - useful for route budgeting

4. **Optional high-risk quorum**
   - use stronger consensus only for:
     - auth/authz
     - payments
     - migrations
     - public API changes
   - do not apply expensive quorum everywhere

5. **Optional UI/browser review lane**
   - only for frontend-heavy MRs
   - separate from core Go reviewer flow

### Why this phase matters

This is how CCR eventually becomes not just a multi-model reviewer, but a **measurable review system**.

### Definition of done

Routing changes are justified by metrics and eval results, not only by intuition.

---

## Recommended file additions

### New runtime / orchestration files

- `quality/scripts/ccr_run.py`
- `quality/scripts/ccr_consolidate.py`
- `quality/scripts/ccr_verify_prepare.py`
- `quality/scripts/ccr_post_comments.py`
- `quality/scripts/ccr_trace.py`
- `quality/scripts/ccr_risk_profile.py`

### New tests / evals

- `tests/test_ccr_routing.py`
- `tests/test_ccr_consolidation.py`
- `tests/test_ccr_verify_prepare.py`
- `tests/test_ccr_post_comments.py`
- `evals/ccr/...`

### Existing files to simplify after the refactor

- `quality/agents/ccr.md`
  - keep it focused on user interaction and high-level control
  - remove deterministic workflow details once those are in Python

---

## Suggested implementation order

If we want the highest impact with the least wasted work, build in this order:

1. **Phase 0** — run isolation + contract cleanup
2. **Phase 1** — deterministic harness
3. **Phase 2** — deterministic posting + approval boundary
4. **Phase 3** — evidence-based consolidation + verification prep
5. **Phase 4** — tests + evals + traces
6. **Phase 5** — metric-driven routing optimization

---

## What not to do yet

1. **Do not add more reviewer personas before Phase 4 exists.**
   - More personas without evals usually increases noise faster than quality.

2. **Do not claim hard guarantees that are still prompt-only.**
   - Especially idempotent posting and strict post-once behavior.

3. **Do not treat static analysis as mere prompt context forever.**
   - Promote it into the deterministic evidence lane.

4. **Do not rely on `MEMORY.md` as the main learning loop.**
   - Use durable traces, eval fixtures, and reviewer scorecards instead.

---

## Mapping to production review-system patterns

| Production pattern | CCR today | Target state |
|---|---|---|
| Evaluator / verifier loop | Present | Keep, but make evidence packaging deterministic |
| Human approval before side effects | Present in prompt | Make approval + posting manifest code-enforced |
| Deterministic verifiers | Partial (`static_analysis.py`) | First-class evidence lane + anchor validation + tests |
| Orchestrator-worker architecture | Mostly prompt-defined | Deterministic harness + structured manifests |
| Trace-based observability | Minimal | Per-run trace + telemetry + CI evals |
| Feedback flywheel | Missing | Publish/skip/delete signals drive routing and prompts |
| Least-privilege execution | Partial | Reviewer workers become read-only deterministic subprocesses |

---

## Bottom line

CCR already has the right **conceptual shape**:

- multi-model reviewers
- persona routing
- a verifier stage
- human approval before posting

The next step is not “more prompts” or “more personas”.

The next step is to turn CCR into a **deterministic review harness around those reviewers**:

- isolate runs
- codify orchestration
- codify side-effect boundaries
- make evidence explicit
- add traces and evals
- tune routing with feedback

That is the path from a strong prototype to a production-grade review agent.
