# CCR Phase 3 Implementation Plan

## Goal

Make candidate consolidation and verification preparation deterministic, evidence-based, and unit-testable so only anchored, evidence-backed candidates reach the verifier and every accepted finding carries a machine-readable evidence bundle.

Phase 3 should do for consolidation/verification preparation what Phase 2 did for MR posting: move the critical logic into explicit helpers, explicit contracts, and explicit artifacts.

---

## Current state

Today the core Phase 3 logic already lives in Python, but it is still relatively thin and heuristic-heavy inside `quality/scripts/ccr_run.py`:

- `_build_candidates()` flattens reviewer findings and clusters them mostly by `(persona, file)` with `line ±3`
- candidate evidence is limited to nearby static-analysis tool names from `_find_static_analysis_evidence(...)`
- `_write_verification_batches()` sends the verifier only `diff_hunk`, `file_context`, `requirements`, and a minimal per-candidate tuple of `candidate_id/file/line/message`
- `_merge_verified_findings()` accepts `confirmed` findings and allows `uncertain` findings only when reviewer consensus support is at least `2`

This is already better than prompt-only behavior, but it still leaves several production gaps:

1. `ccr.candidates_manifest.v1` is emitted without a schema file
2. `ccr.verified_findings.v1` is emitted without a schema file
3. candidates do not yet carry a full evidence bundle or explicit prefilter status
4. dedupe is still too close to `same file + nearby line`
5. cross-persona corroboration is not modeled explicitly
6. verifier batches are still evidence-light compared with what the runtime already knows
7. rejected or dropped candidates are not explained in a dedicated deterministic artifact

---

## Constraints and design goals

1. **Keep `ccr_run.py` as the top-level orchestrator.**
   - Phase 3 should extract logic into helpers without undoing the Phase 1 deterministic harness.

2. **Prefer conservative dropping over weak verification input.**
   - If a candidate lacks an anchor, evidence hunk, or concrete source, it should be dropped before verifier execution.

3. **Preserve report and posting compatibility.**
   - `verified_findings.json` must remain compatible with Phase 2 posting, including stable `finding_number` behavior.

4. **Keep persona-oriented reporting stable.**
   - The report can continue to show a primary persona, but the richer data model should allow supporting personas/reviewers to be recorded.

5. **Use deterministic data, not prompt inference, whenever possible.**
   - Anchor status, evidence presence, candidate IDs, batch creation, and acceptance gates should be coded rules.

6. **Make intermediate decisions inspectable.**
   - If a candidate is merged, retained, dropped, or skipped, the reason should be recoverable from artifacts rather than reconstructed from logs.

---

## Proposed deliverables

### 1. New consolidation helper

Add:

- `quality/scripts/ccr_consolidate.py`

Responsibilities:

1. normalize raw reviewer findings
2. infer or preserve a primary persona label
3. record supporting reviewers/personas
4. assign a deterministic normalized category for dedupe
5. extract symbol/function context when available
6. merge duplicates using explicit rules
7. attach concrete evidence sources
8. assign stable candidate IDs after deterministic sorting
9. write an enriched candidates manifest

### 2. New verification-preparation helper

Add:

- `quality/scripts/ccr_verify_prepare.py`

Responsibilities:

1. load enriched candidates
2. compute anchor status from diff/file context
3. build an explicit evidence bundle per candidate
4. apply deterministic prefilters before verifier execution
5. group ready candidates into verifier batches
6. write a structured verification-prepare artifact
7. write richer batch payloads for `code_review_verify.py`

### 3. New and expanded contracts

Add or expand contracts so the artifacts emitted by Phase 3 are first-class and testable.

#### New schema files

- `quality/contracts/v1/candidates_manifest.schema.json`
- `quality/contracts/v1/verification_prepare.schema.json`
- `quality/contracts/v1/verified_findings.schema.json`

#### Expanded existing schema files

- `quality/contracts/v1/consolidated_candidate.schema.json`
- `quality/contracts/v1/verification_batch.schema.json`
- `quality/contracts/v1/verification_result.schema.json`
- `quality/contracts/v1/run_manifest.schema.json` (if a new artifact path is surfaced)
- `quality/contracts/v1/run_summary.schema.json` (if a new artifact path is surfaced)
- `quality/contracts/v1/run_launch.schema.json` (if a new artifact path is surfaced)

### 4. New run artifact

Add a dedicated artifact for verification preparation:

- `verification_prepare_file` → `verification_prepare.json`

Recommended contents:

- candidate counts before/after prefilters
- dropped candidates with machine-readable reasons
- batch list / batch files
- per-candidate readiness state

This makes Phase 3 decisions inspectable without forcing users to reverse-engineer the verifier inputs.

---

## Proposed data-model evolution

Phase 3 should keep the current required fields and expand them with richer evidence and decision metadata.

### Consolidated candidate shape

Example target shape:

```json
{
  "contract_version": "ccr.consolidated_candidate.v1",
  "candidate_id": "F12",
  "persona": "security",
  "supporting_personas": ["logic"],
  "severity": "warning",
  "file": "internal/auth/jwt.go",
  "line": 42,
  "symbol": "ValidateToken",
  "normalized_category": "jwt-validation-missing-expiry-check",
  "message": "The token validation path accepts tokens without enforcing expiry checks.",
  "reviewers": ["security_p1", "logic_p2"],
  "consensus": "2/3",
  "support_count": 2,
  "available_pass_count": 3,
  "anchor_status": "diff",
  "evidence_sources": ["reviewer", "diff_hunk", "gosec"],
  "source_findings": [
    {
      "pass_name": "security_p1",
      "provider": "gemini",
      "persona": "security",
      "file": "internal/auth/jwt.go",
      "line": 42,
      "severity": "warning",
      "message": "..."
    }
  ],
  "prefilter": {
    "ready_for_verification": true,
    "drop_reasons": []
  },
  "evidence_bundle": {
    "diff_hunk": "@@ ...",
    "file_context": "...",
    "static_analysis": [
      {
        "tool": "gosec",
        "file": "internal/auth/jwt.go",
        "line": 41,
        "message": "..."
      }
    ],
    "requirements_excerpt": "..."
  }
}
```

### Verification-prepare shape

Recommended `verification_prepare.json` summary fields:

- `contract_version`
- `ready_candidates[]`
- `dropped_candidates[]`
- `batches[]`
- `summary`
  - `candidate_count`
  - `ready_count`
  - `dropped_count`
  - `batch_count`

### Verified findings shape

`verified_findings.json` should continue to carry Phase 2 fields and add evidence-grounding fields such as:

- `support_count`
- `available_pass_count`
- `anchor_status`
- `evidence_sources`
- `evidence_bundle` (or a compact reference/subset)
- `verification_decision` / `verifier_verdict`
- `prefilter_status`

---

## Key design decisions

### A. Keep a primary persona, but record corroboration explicitly

To avoid destabilizing report formatting and Phase 2 posting, each candidate should continue to have a primary `persona` field.

Add optional corroboration fields such as:

- `supporting_personas`
- `reviewers`
- `support_count`
- `available_pass_count`

This lets Phase 3 model multi-persona support without forcing a full report-format redesign.

### B. Replace near-line-only dedupe with a deterministic category/symbol key

Current clustering is too dependent on `line ±3`.

Phase 3 should move toward a deterministic merge key closer to:

- `file`
- `normalized_category`
- `symbol` when available
- near-line fallback only when symbol/category are weak or missing

Rules of thumb:

- same file + same symbol + same normalized root cause -> merge
- same line but clearly different categories -> keep separate
- different personas may corroborate the same candidate rather than spawning duplicates

### C. Prefilters should be explicit and conservative

Before the verifier runs, each candidate should be checked for:

1. valid file path
2. line >= 1
3. anchorable diff or file context
4. non-empty evidence hunk or file context
5. at least one concrete source (`reviewer`, `static_analysis`, `requirements`, etc.)

If a candidate fails prefiltering, it should be dropped with a machine-readable reason such as:

- `missing_file`
- `missing_anchor`
- `missing_evidence`
- `missing_concrete_source`
- `invalid_line`

### D. Verification batches should contain structured evidence, not only bare findings

Phase 3 should expand `verification_batch.v1` so each candidate sent to the verifier includes richer context such as:

- `persona`
- `severity`
- `reviewers`
- `consensus`
- `symbol`
- `anchor_status`
- `evidence_sources`
- `source_findings`
- `evidence_bundle`

This lets the verifier judge evidence instead of reconstructing it from a short message.

### E. Post-verifier acceptance should use deterministic gates

Keep the final acceptance policy coded rather than prompt-driven.

Recommended initial rules:

1. `confirmed` + prefilter-ready -> accept
2. `uncertain` + support_count >= 2 + anchor_status != `missing` -> accept as tentative
3. `rejected` -> drop
4. verifier output that contradicts candidate identity too aggressively (for example, mismatched file/line outside a valid anchor window) should be treated conservatively

### F. Phase 3 should add schemas for artifacts that already exist implicitly

Two current contract versions deserve first-class schema coverage:

- `ccr.candidates_manifest.v1`
- `ccr.verified_findings.v1`

That closes a real validation gap and makes future refactors safer.

---

## Files to change

### New files

- `quality/scripts/ccr_consolidate.py`
- `quality/scripts/ccr_verify_prepare.py`
- `quality/contracts/v1/candidates_manifest.schema.json`
- `quality/contracts/v1/verification_prepare.schema.json`
- `quality/contracts/v1/verified_findings.schema.json`
- `tests/test_ccr_consolidate.py`
- `tests/test_ccr_verify_prepare.py`
- `tests/fixtures/consolidation/...`
- `tests/fixtures/verification/...`

### Files to update

- `quality/scripts/ccr_run_init.py` (if `verification_prepare_file` is added)
- `quality/scripts/ccr_run.py`
- `quality/contracts/v1/consolidated_candidate.schema.json`
- `quality/contracts/v1/verification_batch.schema.json`
- `quality/contracts/v1/verification_result.schema.json`
- `quality/contracts/v1/run_manifest.schema.json`
- `quality/contracts/v1/run_summary.schema.json`
- `quality/contracts/v1/run_launch.schema.json`
- `quality/contracts/v1/README.md`
- `quality/agents/ccr.md`
- `README.md`
- `scripts/smoke.sh`
- `tests/test_contracts.py`
- `tests/test_ccr_run.py`

---

## Suggested implementation order

### Step 1 — Contracts, artifacts, and fixtures

1. Add schema coverage for `candidates_manifest`, `verification_prepare`, and `verified_findings`
2. Expand candidate / verification contracts for richer evidence fields
3. Add `verification_prepare_file` to the run manifest if we want an explicit preparation artifact
4. Add golden fixtures for reviewer findings -> candidates -> verification batches
5. Add contract tests before deeper refactors

### Step 2 — Deterministic consolidation helper

1. Extract current candidate-building logic from `ccr_run.py` into `ccr_consolidate.py`
2. Add finding normalization helpers
3. Add normalized-category and symbol extraction helpers
4. Implement deterministic merge/dedupe rules
5. Preserve stable candidate ordering and candidate IDs
6. Emit enriched `candidates_file`
7. Add focused unit tests

### Step 3 — Verification preparation helper

1. Extract diff-block / file-context / batching logic into `ccr_verify_prepare.py`
2. Compute `anchor_status` per candidate
3. Pack structured evidence bundles
4. Drop non-verifiable candidates with explicit reasons
5. Write `verification_prepare.json`
6. Write richer `verification_batch` payloads
7. Add focused unit tests

### Step 4 — Wire Phase 3 into `ccr_run.py`

1. Replace in-file consolidation helpers with calls into the new modules
2. Update runtime summaries/status payloads if the new artifact is surfaced
3. Upgrade `_merge_verified_findings()` to include richer evidence / decision metadata
4. Keep `finding_number` stable and compatible with Phase 2 posting
5. Update end-to-end dry-run tests

### Step 5 — Docs and smoke coverage

1. Update `quality/agents/ccr.md` to describe the richer evidence-based middle stages
2. Update `README.md` and `quality/contracts/v1/README.md`
3. Extend `scripts/smoke.sh` with deterministic helper invocations
4. Ensure the smoke path still works without live providers

### Step 6 — Release

1. run `python3 -m py_compile ...`
2. run `python3 -m unittest discover -s tests -v`
3. run `./scripts/smoke.sh`
4. bump plugin/marketplace versions
5. commit the Phase 3 changes
6. tag and push the release

---

## Test plan

### Unit tests

#### `tests/test_ccr_consolidate.py`

Cover at least:

1. same root cause across nearby lines merges deterministically
2. same line but different categories stays separate
3. cross-persona corroboration records supporting personas instead of duplicating candidates
4. missing/invalid reviewer findings are dropped
5. candidate IDs remain stable across repeated runs
6. static-analysis evidence is attached when nearby findings exist

#### `tests/test_ccr_verify_prepare.py`

Cover at least:

1. `anchor_status` resolves to `diff`, `file_context`, or `missing`
2. missing file / invalid line candidates are dropped deterministically
3. evidence bundles contain diff/file context and concrete source metadata
4. batch chunking stays deterministic and bounded
5. `verification_prepare.json` summarizes ready/dropped counts correctly

#### `tests/test_ccr_run.py`

Extend end-to-end dry-run coverage for:

1. richer `candidates_file` shape
2. `verification_prepare_file` existence/content if added
3. richer `verified_findings.json` evidence metadata
4. stable `finding_number` behavior after the refactor

### Contract tests

Update `tests/test_contracts.py` for:

- `candidates_manifest.schema.json`
- `verification_prepare.schema.json`
- `verified_findings.schema.json`
- expanded `consolidated_candidate.schema.json`
- expanded `verification_batch.schema.json`
- expanded `verification_result.schema.json`
- updated run-manifest / run-summary / run-launch schemas if a new artifact path is exposed

### Smoke coverage

Extend `scripts/smoke.sh` with deterministic invocations for:

- `quality/scripts/ccr_consolidate.py`
- `quality/scripts/ccr_verify_prepare.py`
- `quality/scripts/ccr_run.py --dry-run`

The smoke path should stay local and deterministic; no live provider/network dependency should be introduced.

---

## Out of scope for the initial Phase 3 delivery

To keep the phase bounded, the first implementation should **not** include:

- learning-based routing or policy tuning from historical runs
- live GitLab or issue-tracker feedback loops
- automatic targeted `go test` execution for every candidate
- complex semantic clustering beyond deterministic normalization heuristics
- report-format redesign beyond small evidence-grounding improvements
- changes to Phase 2 posting semantics beyond consuming richer verified-finding metadata safely

Those belong either to follow-up hardening or to the later eval/feedback phases.

---

## Expected release shape

Recommended release target:

- `v0.5.0`

Reasoning:

- `v0.3.x` covered deterministic execution + observability + watcher UX
- `v0.4.0` covered deterministic MR posting side effects
- `v0.5.0` is a clean boundary for deterministic evidence-based consolidation and verification preparation
