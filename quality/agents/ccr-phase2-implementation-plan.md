# CCR Phase 2 Implementation Plan

## Goal

Move MR comment posting from prompt-controlled shell steps into a deterministic, replayable helper with explicit approval artifacts, idempotency, anchor validation, and per-comment result manifests.

Phase 2 should make posting as reproducible and auditable as the existing `ccr_run.py` review harness.

---

## Current state

Today MR posting is still described in `quality/agents/ccr.md` as manual prompt logic:

- read `mr_metadata_file`
- read `verified_findings_file`
- clean `comments_dir`
- build JSON payloads in shell/Python
- call `glab api projects/<PROJECT>/merge_requests/<IID>/discussions`
- inspect the response ad hoc

This means the current system still lacks:

1. a deterministic approval artifact
2. a dedicated posting entrypoint
3. reproducible idempotency checks
4. structured posting results
5. coded anchor validation rules

---

## Constraints and design goals

1. **Posting remains a separate stage from review generation.**
   - `ccr_run.py` continues to stop at verified findings + report generation.
   - Side effects happen only after explicit user approval.

2. **Approval must become a file, not agent memory.**
   - The agent may ask the user which findings to publish.
   - The answer must be materialized into a run-scoped JSON file before any posting happens.

3. **Idempotency must be deterministic.**
   - Re-running the same approved publish set must not double-post.

4. **Posting should reuse existing run artifacts.**
   - Use `run_manifest.json`, `mr_metadata.json`, `verified_findings.json`, `report.md`, and `review_artifact.txt`.

5. **Local modes remain report-only.**
   - The posting helper should reject non-MR runs.

6. **Safe failure is more important than aggressive retrying.**
   - If anchors cannot be built or responses are ambiguous, prefer a skipped/failed manifest entry over unsafe posting.

---

## Proposed deliverables

### 1. New deterministic posting helper

Add:

- `quality/scripts/ccr_post_comments.py`

Responsibilities:

1. load the run manifest and validate MR mode prerequisites
2. read the explicit approval artifact
3. resolve approved finding numbers into verified findings
4. build deterministic fingerprints
5. construct GitLab DiffNote payloads
6. write run-scoped payload JSON files into `comments_dir`
7. fetch existing discussions and detect already-posted findings
8. post only missing approved findings
9. validate API responses (`DiffNote`)
10. emit a structured posting result manifest

### 2. New run artifacts

Extend run-scoped artifacts with:

- `posting_approval_file` → explicit user approval input
- `posting_manifest_file` → prepared posting plan with payloads/fingerprints/anchors
- `posting_results_file` → execution results for each approved finding

Keep existing:

- `comments_dir` → per-finding request/response payload files

### 3. New/expanded contracts

#### `ccr.posting_approval.v1`

New schema for the explicit approval boundary.

Example:

```json
{
  "contract_version": "ccr.posting_approval.v1",
  "run_id": "20260414T000000Z-1234-abcd1234",
  "project": "group/project",
  "mr_iid": 200,
  "approved_finding_numbers": [1, 2, 5],
  "approved_all": false,
  "approved_at": "2026-04-14T23:59:00Z",
  "source": "user_selection"
}
```

#### `ccr.posting_manifest.v1`

Expand the existing schema so it becomes a real prepared plan instead of a tiny placeholder.

Recommended fields:

- `run_id`
- `project`
- `mr_iid`
- `diff_refs`
- `approved_finding_numbers`
- `approved_findings[]`
  - `finding_number`
  - `candidate_id`
  - `file`
  - `line`
  - `message`
  - `fingerprint`
  - `payload_file`
  - `anchor`
  - `status` (`ready|already_posted|missing_anchor|invalid_selection`)

#### `ccr.posting_result.v1`

New schema for execution results.

Recommended fields:

- `run_id`
- `project`
- `mr_iid`
- `started_at`
- `finished_at`
- `posted_count`
- `already_posted_count`
- `skipped_count`
- `failed_count`
- `results[]`
  - `finding_number`
  - `candidate_id`
  - `fingerprint`
  - `status` (`posted|already_posted|skipped_missing_anchor|failed|invalid_response`)
  - `payload_file`
  - `response_file`
  - `discussion_id`
  - `note_id`
  - `error`
  - `attempts`

---

## Key design decisions

### A. Stable finding numbers become first-class runtime data

The helper must map user selections like `1,2,5` to exact verified findings.

Current state:
- `report.md` is numbered
- `verified_findings.json` is ordered, but the finding number is not explicitly stored

Implementation decision:
- stamp `finding_number` onto each verified finding when `ccr_run.py` writes `verified_findings.json`
- keep the report numbering derived from the same ordered list

This removes ambiguity and makes posting independent from markdown parsing.

### B. Fingerprints are embedded into comment bodies as hidden metadata

Use a deterministic fingerprint such as:

- project
- MR IID
- file
- line
- normalized message hash

Recommended generated footer:

```text
<!-- ccr:fingerprint=<hash> run_id=<run_id> finding=<n> candidate_id=<id> -->
```

This gives the posting helper a reliable way to detect previously posted comments in GitLab discussions without relying only on fuzzy body matching.

### C. Anchor construction should come from captured review artifacts

Use:

- `mr_metadata.json` for `diff_refs`
- `review_artifact.txt` for file/hunk/line mapping

The helper should parse the captured diff and determine whether a finding maps to:

- a `new_line`
- an `old_line`
- or both (`unchanged` line inside a diff hunk)

If a valid text anchor cannot be derived, the finding should be skipped with a machine-readable reason.

### D. Safe retry policy must re-check idempotency before re-posting

Per finding:

1. check existing discussions before first POST
2. if fingerprint already exists → mark `already_posted`
3. POST once
4. if the result is ambiguous (transport failure / non-parseable response), fetch discussions again
5. if the fingerprint is now present → mark as already posted after retry check
6. otherwise allow at most one more POST attempt
7. never retry after a validated `DiffNote` success
8. never keep retrying invalid-response payloads

This keeps retries conservative and idempotent.

---

## Proposed helper CLI

```bash
python3 quality/scripts/ccr_post_comments.py \
  --manifest-file /tmp/ccr/<run_id>/run_manifest.json \
  --approval-file /tmp/ccr/<run_id>/posting_approval.json \
  --prepare-only
```

```bash
python3 quality/scripts/ccr_post_comments.py \
  --manifest-file /tmp/ccr/<run_id>/run_manifest.json \
  --approval-file /tmp/ccr/<run_id>/posting_approval.json \
  --apply
```

### Mode behavior

#### `--prepare-only`

- validate MR mode
- load approvals
- resolve finding numbers
- build fingerprints
- build anchors
- write payload files
- write `posting_manifest.json`
- do not perform side effects

#### `--apply`

- perform all `prepare` steps if needed
- fetch existing discussions
- skip duplicates
- POST only ready items
- validate responses
- write `posting_results.json`
- print a compact summary JSON payload to stdout

---

## Agent workflow after Phase 2

### MR mode

1. run `ccr_run.py`
2. present verified findings / report
3. ask the user which findings to publish
4. write `posting_approval.json`
5. run `ccr_post_comments.py --prepare-only`
6. if prep is valid, run `ccr_post_comments.py --apply`
7. summarize `posting_results.json`

### Local modes

- never call `ccr_post_comments.py`
- remain report-only

---

## Files to change

### New files

- `quality/scripts/ccr_post_comments.py`
- `quality/contracts/v1/posting_approval.schema.json`
- `quality/contracts/v1/posting_result.schema.json`
- `tests/test_ccr_post_comments.py`
- `tests/fixtures/posting/...`

### Files to update

- `quality/scripts/ccr_run_init.py`
- `quality/contracts/v1/run_manifest.schema.json`
- `quality/contracts/v1/run_summary.schema.json` (artifact references if surfaced)
- `quality/contracts/v1/run_launch.schema.json` (artifact references if surfaced)
- `quality/contracts/v1/posting_manifest.schema.json`
- `quality/contracts/v1/README.md`
- `quality/scripts/ccr_run.py` (persist `finding_number` in verified findings)
- `quality/agents/ccr.md`
- `README.md`
- `scripts/smoke.sh`
- `tests/test_ccr_run_init.py`
- `tests/test_contracts.py`

---

## Suggested implementation order

### Step 1 — Contracts and run artifacts

1. Add `posting_approval_file`, `posting_manifest_file`, `posting_results_file` to `ccr_run_init.py`
2. Update run manifest schema
3. Add `posting_approval.v1` and `posting_result.v1` schemas
4. Expand `posting_manifest.v1`
5. Add contract tests

### Step 2 — Stable finding numbering

1. Update `ccr_run.py` so `verified_findings.json` includes `finding_number`
2. Keep report numbering derived from the same order
3. Add/adjust tests

### Step 3 — Posting helper core

1. Parse approval file
2. Resolve approved finding numbers
3. Compute fingerprints
4. Parse diff for anchors
5. Write `posting_manifest.json`
6. Add unit tests for prepare mode

### Step 4 — Side-effect execution lane

1. Fetch existing discussions
2. Detect duplicate fingerprints
3. POST ready payloads
4. Validate `DiffNote` responses
5. Write `posting_results.json`
6. Add unit tests for apply mode and retry safety

### Step 5 — Agent wiring and docs

1. Replace prompt-built `glab api` posting instructions in `quality/agents/ccr.md`
2. Make the agent write `posting_approval.json` and call the helper
3. Update `README.md`
4. Extend `scripts/smoke.sh`

### Step 6 — Release

1. run `python3 -m unittest discover -s tests -v`
2. run `./scripts/smoke.sh`
3. bump plugin/marketplace version
4. commit Phase 2
5. tag and push release

---

## Test plan

### Unit tests

Add `tests/test_ccr_post_comments.py` covering:

1. approval parsing (`all`, subset, invalid finding numbers)
2. deterministic fingerprint generation
3. diff anchor building for added/removed/unchanged lines
4. prepare mode emits valid `posting_manifest.json`
5. existing-discussion fingerprint detection
6. successful post with valid `DiffNote` response
7. invalid response type is rejected
8. ambiguous failure triggers re-check before final retry
9. non-MR manifest is rejected

### Contract tests

Update `tests/test_contracts.py` for:

- `posting_approval.schema.json`
- expanded `posting_manifest.schema.json`
- `posting_result.schema.json`
- updated run-manifest schema fields

### Smoke coverage

Extend `scripts/smoke.sh` with a deterministic `ccr_post_comments.py --prepare-only` smoke path against fixtures.

If possible, keep `--apply` in unit tests with stubbed `glab` responses rather than a live network dependency.

---

## Out of scope for initial Phase 2 delivery

To keep the phase bounded, the first implementation should **not** include:

- editing or deleting existing CCR comments
- multi-line discussion ranges
- suggestion blocks
- fallback overview comments when a diff anchor is missing
- non-GitLab providers
- automatic re-posting after MR diff refs drift

Those can be follow-up patches after the deterministic posting lane is in place.

---

## Expected release shape

Because this is a new deterministic side-effect lane, the most natural release target is:

- `v0.4.0`

That keeps the versioning aligned with a meaningful feature boundary: Phase 1.x covered deterministic review execution and live UX; Phase 2 adds deterministic publish execution.
