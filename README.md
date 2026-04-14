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
- **Verification stage** (Codex with Gemini fallback) filters speculative findings before anything is shown to the user
- **Post-once guarantee** in MR mode: inline `DiffNote` comments, never double-posted, only after explicit user approval

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
3. Gathering requirements (optional)
4. Adaptive routing (prints the review plan)
5. Running reviewer passes in parallel
6. Consolidating + verifying findings
7. Printing a numbered report
8. In MR mode: asking which findings to publish, then posting inline

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
└── README.md
```

All paths inside `quality/agents/ccr.md` use `${CLAUDE_PLUGIN_ROOT}` so the plugin works regardless of where it's installed. At runtime Claude Code resolves `${CLAUDE_PLUGIN_ROOT}` to `~/.claude/plugins/cache/ccr-marketplace/quality/<version>/` on each user's machine.

## Critical Rules (from the agent)

1. Never post comments without explicit user approval in MR mode.
2. Always initialize an isolated run workspace first, then run adaptive fanout planning. Logic Pass 1 + Pass 2 + Pass 3 are mandatory; total planned fanout stays within 4-14 passes.
3. All reviewer passes are `Task(general-purpose)` calls with a 15-minute Task deadline (`900000ms`) and a 10-minute inner wrapper timeout.
4. Candidate findings must pass verification before being shown.
5. Local diff / file / package modes are **report-only** — no posting target exists.

See `quality/agents/ccr.md` for the full workflow specification.

## Status

Phase 0 is in progress: CCR now has an isolated run-workspace initializer (`ccr_run_init.py`), versioned contract schemas under `quality/contracts/v1/`, and aligned documentation for the current 4-14-pass / 15-minute-task model. The larger deterministic harness, posting helper, tests, and eval suites are still future work.

## License

No explicit license yet — all rights reserved by the author. Open an issue if you want to use it.
