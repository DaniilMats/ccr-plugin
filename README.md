# CCR — Claude Code Reviewer Plugin

Adaptive multi-model code reviewer for [Claude Code](https://claude.com/claude-code). CCR orchestrates 4-14 reviewer passes across targeted personas (Logic, Security, Concurrency, Performance, Requirements), verifies consolidated findings with a second-stage verifier, and — in GitLab MR mode — posts approved inline comments only after user confirmation.

## Features

- **Four review target families**:
  - GitLab MR URL → full MR mode with metadata fetch, numbered findings, optional posting
  - Local diff scope → `uncommitted`, `commit:<SHA>`, `branch:<BASE>`
  - Single Go file → `file:<PATH>` or a raw path to a `.go` file
  - Go package directory → `package:<PATH>` or a raw path to a directory with `.go` files
- **Adaptive fanout routing** (4-14 passes) driven by changed lines, triggered personas, and critical surfaces
- **Triple-model diversity**: Pass 1 Gemini, Pass 2 Codex on a shuffled diff, Pass 3 Claude Opus with `--effort max`. Three independent models catch three different classes of issues. Logic always runs all three; specialty personas get Pass 3 only in the full matrix.
- **Evidence-based consolidation + verification prep**: deterministic `ccr_consolidate.py` and `ccr_verify_prepare.py` attach corroboration, evidence bundles, anchor status, and prefilter decisions before verifier execution
- **Verification stage** (Codex with Gemini fallback) filters speculative findings before anything is shown to the user
- **Post-once guarantee** in MR mode: approved inline `DiffNote` comments now go through a deterministic posting helper with explicit approval artifacts, fingerprint-based idempotency, and structured posting results

## Requirements

CCR delegates reviewing to external CLI tools. You need:

| Tool | Purpose | Install |
|------|---------|---------|
| `python3` | Runtime for helper scripts | `brew install python@3.12` |
| `glab` | GitLab MR fetch + posting | `brew install glab && glab auth login` |
| `gemini` | Pass 1 reviewers | `npm install -g @google/gemini-cli && gemini auth` |
| `codex` | Pass 2 reviewers + verifier (default) | `npm install -g @openai/codex && codex login` |
| `claude` | Pass 3 reviewers (Opus, max effort) | [Install Claude Code](https://claude.com/claude-code) — you already have it to run this plugin |

CCR still partially works without some of these — missing tools just cause their passes to be skipped gracefully.

`install.sh` (executed automatically via SessionStart hook) checks what's available and prints install hints for anything missing. It never blocks Claude Code startup.

## Installation

### Via plugin marketplace (recommended)

From inside Claude Code:

```
/plugin marketplace add DaniilMats/ccr-plugin
/plugin install quality@ccr-marketplace
/reload-plugins
```

Then verify with `/plugin` — `quality` should be listed as installed.

### Local development

```bash
git clone https://github.com/DaniilMats/ccr-plugin ~/ccr-plugin
# From inside Claude Code:
/plugin marketplace add ~/ccr-plugin
/plugin install quality@ccr-marketplace
```

## Usage

Once installed, invoke the agent from Claude Code by asking any of:

```
> use ccr to review this MR: https://gitlab.com/group/project/-/merge_requests/1234
> use ccr to review uncommitted
> use ccr to review branch:main
> use ccr to review file:internal/service/auth.go
> use ccr to review package:internal/service
```

Or explicitly spawn the agent via `Task(quality:ccr, "...")` — the plugin registers it under the namespaced id `quality:ccr` (plugin name `quality` + agent file `ccr.md`).

CCR walks the user through:
1. Initializing an isolated per-run workspace under `/tmp/ccr/<run_id>/`
2. Fetching the MR / preparing the local artifact
3. Gathering non-empty requirements/spec input before launch
4. Adaptive routing (prints the review plan)
5. Running reviewer passes in parallel
6. Evidence-based candidate consolidation + verification preparation
7. Consolidating verifier outcomes into numbered findings
8. Printing a numbered report
9. In MR mode: asking which findings to publish, materializing `posting_approval.json`, and posting through the deterministic `ccr_post_comments.py` helper

`ccr_run.py` now refuses to launch without explicit non-empty requirements/spec input. For MR runs, `--use-mr-description-as-requirements` is allowed only when the MR description is non-empty.

## Local safety checks

Before larger refactors, run the deterministic local safety harness:

```bash
./scripts/smoke.sh
```

This runs:
- `py_compile` on the CCR Python entrypoints
- `python3 -m unittest discover -s tests -v`
- smoke invocations for `ccr_run_init.py`, `ccr_routing.py`, `repomap.py`, `review_context.py`, `ccr_consolidate.py`, `ccr_verify_prepare.py`, the deterministic `ccr_run.py` harness in `--dry-run` mode, the detached `ccr_run.py --detach` + `ccr_watch.py` watch flow, and `ccr_post_comments.py --prepare-only`
- validation that `ccr_run.py` writes a live `status.json`, append-only `trace.jsonl`, final `run_summary.json`, a run-scoped `watch_cursor.json`, and a background-launch `run_launch` payload

You can also run the unit tests directly:

```bash
python3 -m unittest discover -s tests -v
```

Optional dogfooding signal:

```text
use ccr to review uncommitted
```

That self-review is useful as an additional signal, but it should not replace the deterministic smoke/tests above.

## Structure

The repo is a Claude Code marketplace hosting a single plugin in the `quality/` subdirectory:

```
ccr-plugin/
├── .claude-plugin/
│   └── marketplace.json            # marketplace manifest (plugins[].source = "./quality")
├── quality/                        # the plugin — ${CLAUDE_PLUGIN_ROOT} resolves here
│   ├── .claude-plugin/
│   │   └── plugin.json             # plugin manifest
│   ├── agents/
│   │   └── ccr.md                  # agent definition (uses ${CLAUDE_PLUGIN_ROOT})
│   ├── hooks/
│   │   └── hooks.json              # SessionStart hook → install.sh --check-only
│   ├── install.sh                  # dependency checker
│   ├── contracts/
│   │   └── v1/                     # versioned JSON contract schemas for CCR runtime artifacts
│   └── scripts/
│       ├── ccr_run_init.py         # isolated run workspace + manifest initializer
│       ├── ccr_run.py              # deterministic harness orchestrator with sync and detached execution modes
│       ├── ccr_watch.py            # compact/quiet watcher over status.json + trace.jsonl with cursor + follow modes
│       ├── ccr_consolidate.py      # deterministic candidate consolidation with corroboration + dedupe rules
│       ├── ccr_verify_prepare.py   # deterministic verification prep with anchors, prefilters, and batch writing
│       ├── ccr_post_comments.py    # deterministic MR posting helper with approval, idempotency, and result manifests
│       ├── ccr_routing.py          # adaptive fanout planner (4-14 passes)
│       ├── repomap.py              # lightweight focused repo map helper for review_context.py
│       └── llm-proxy/
│           ├── code_review.py          # main reviewer wrapper
│           ├── code_review_verify.py   # verifier (Codex default, Gemini fallback)
│           ├── review_context.py       # repo + focus-file context builder
│           ├── static_analysis.py      # go vet / staticcheck / gosec integration
│           ├── shuffle_diff.py         # diff shuffler for Pass 2 diversity
│           ├── llm_proxy.py            # internal provider dispatcher
│           ├── validator.py            # JSON schema validator
│           ├── adapters/
│           │   ├── base.py             # provider adapter interface
│           │   ├── gemini.py           # Gemini CLI adapter (Pass 1)
│           │   ├── codex.py            # Codex CLI adapter (Pass 2)
│           │   └── claude.py           # Claude CLI adapter (Pass 3 — Opus, max effort)
│           ├── prompts/
│           │   ├── code_review.txt         # base code review prompt
│           │   ├── go_style_guide.txt      # embedded Go style guide
│           │   ├── review_logic.txt        # Logic persona prompt
│           │   ├── review_security.txt     # Security persona prompt
│           │   ├── review_concurrency.txt  # Concurrency persona prompt
│           │   ├── review_performance.txt  # Performance persona prompt
│           │   ├── review_requirements.txt # Spec compliance prompt
│           │   └── review_verify.txt       # Verifier prompt
│           └── schemas/
│               ├── code_review_response.schema.json              # reviewer output
│               └── code_review_verification_response.schema.json # verifier output
├── scripts/
│   └── smoke.sh                    # deterministic local safety harness
├── tests/
│   ├── fixtures/                   # golden fixtures for routing/context smoke cases
│   └── test_*.py                   # stdlib unittest safety net
└── README.md
```

All paths inside `quality/agents/ccr.md` use `${CLAUDE_PLUGIN_ROOT}` so the plugin works regardless of where it's installed. At runtime Claude Code resolves `${CLAUDE_PLUGIN_ROOT}` to `~/.claude/plugins/cache/ccr-marketplace/quality/<version>/` on each user's machine.

## Critical Rules (from the agent)

1. Never post comments without explicit user approval in MR mode.
2. Always initialize an isolated run workspace first, then run adaptive fanout planning. Logic Pass 1 + Pass 2 + Pass 3 are mandatory; total planned fanout stays within 4-14 passes.
3. Phase 1 orchestration now runs through the deterministic `quality/scripts/ccr_run.py` harness, which owns artifact prep, routing, reviewer subprocess execution, consolidation, verification, and report generation.
4. The harness now supports detached/background execution and writes machine-readable observability artifacts: `status.json`, `trace.jsonl`, `run_summary.json`, plus launch metadata for watching.
5. `quality/scripts/ccr_watch.py` now supports compact JSON, quiet icon-prefixed text mode, cursor files, and follow mode so progress updates do not flood the conversation.
6. Phase 2 MR posting now runs through `quality/scripts/ccr_post_comments.py` with explicit `posting_approval.json`, prepared payloads, fingerprint-based idempotency, `posting_results.json`, and normalization/backfill for incomplete approval files produced by older agent templates.
7. Phase 3 candidate consolidation and verification preparation now run through `quality/scripts/ccr_consolidate.py` and `quality/scripts/ccr_verify_prepare.py` with corroboration metadata, evidence bundles, anchor status, deterministic prefilters, and structured verification-prep artifacts.
8. In Claude Code, `Monitor` is the preferred live UX layer for long reviews; session-scoped scheduled tasks (`/loop` / `CronCreate`) remain a coarse 1-minute fallback.
9. Candidate findings must pass verification before being shown.
10. Local diff / file / package modes are **report-only** — no posting target exists.

See `quality/agents/ccr.md` for the full workflow specification.

## Status

Phase 3 is complete: CCR has an isolated run-workspace initializer (`ccr_run_init.py`), a deterministic orchestration harness (`ccr_run.py`) with synchronous and detached execution modes, live observability artifacts (`status.json`, `trace.jsonl`, `run_summary.json`, `watch_cursor.json`), a compact watcher (`ccr_watch.py`) with quiet/follow modes, a deterministic MR posting helper (`ccr_post_comments.py`), and deterministic evidence-based middle-lane helpers (`ccr_consolidate.py`, `ccr_verify_prepare.py`) that add corroboration metadata, evidence bundles, anchor status, deterministic prefilters, structured verification-prep artifacts, and richer verified findings. Versioned contract schemas live under `quality/contracts/v1/`, stdlib unit tests under `tests/`, golden fixtures cover routing/context behavior, and the deterministic local smoke harness lives at `./scripts/smoke.sh`.

## License

No explicit license yet — all rights reserved by the author. Open an issue if you want to use it.
