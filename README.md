# CCR — Claude Code Reviewer Plugin

Adaptive multi-model code reviewer for [Claude Code](https://claude.com/claude-code). CCR orchestrates 4-10 reviewer passes across targeted personas (Logic, Security, Concurrency, Performance, Requirements), verifies consolidated findings with a second-stage verifier, and — in GitLab MR mode — posts approved inline comments only after user confirmation.

## Features

- **Four review target families**:
  - GitLab MR URL → full MR mode with metadata fetch, numbered findings, optional posting
  - Local diff scope → `uncommitted`, `commit:<SHA>`, `branch:<BASE>`
  - Single Go file → `file:<PATH>` or a raw path to a `.go` file
  - Go package directory → `package:<PATH>` or a raw path to a directory with `.go` files
- **Adaptive fanout routing** (4-10 passes) driven by changed lines, triggered personas, and critical surfaces
- **Dual-model diversity**: Pass 1 uses Gemini, Pass 2 uses Codex on a shuffled diff — different models catch different issues
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

CCR still partially works without some of these — missing tools just cause their passes to be skipped gracefully.

`install.sh` (executed automatically via SessionStart hook) checks what's available and prints install hints for anything missing. It never blocks Claude Code startup.

## Installation

### Via plugin marketplace (recommended)

```bash
# From inside Claude Code:
/plugin marketplace add DaniilMats/ccr-plugin
/plugin install ccr@ccr-plugin
```

### Local development

```bash
git clone https://github.com/DaniilMats/ccr-plugin ~/ccr-plugin
claude --plugin-dir ~/ccr-plugin
```

## Usage

Once installed, invoke the agent from Claude Code:

```
> review this MR: https://gitlab.com/group/project/-/merge_requests/1234
> review uncommitted
> review branch:main
> review file:internal/service/auth.go
> review package:internal/service
```

Or explicitly spawn the agent via `Task(ccr, "...")`.

CCR walks the user through:
1. Fetching the MR / preparing the local artifact
2. Gathering requirements (optional)
3. Adaptive routing (prints the review plan)
4. Running reviewer passes in parallel
5. Consolidating + verifying findings
6. Printing a numbered report
7. In MR mode: asking which findings to publish, then posting inline

## Structure

```
ccr-plugin/
├── .claude-plugin/plugin.json    # manifest
├── agents/ccr.md                 # agent definition (uses ${CLAUDE_PLUGIN_ROOT})
├── hooks/hooks.json              # SessionStart hook → install.sh --check-only
├── install.sh                    # dependency checker
├── scripts/
│   ├── ccr_routing.py            # adaptive fanout planner
│   └── llm-proxy/
│       ├── sisyphus_code_review.py         # main reviewer wrapper
│       ├── sisyphus_code_review_verify.py  # verifier
│       ├── review_context.py               # repo context builder
│       ├── static_analysis.py              # go vet / staticcheck integration
│       ├── shuffle_diff.py                 # diff shuffler for Pass 2 diversity
│       ├── adapters/                       # gemini + codex adapters
│       ├── prompts/                        # reviewer prompts per persona
│       └── schemas/                        # JSON schemas for reviewer/verifier output
└── README.md
```

All paths inside `agents/ccr.md` use `${CLAUDE_PLUGIN_ROOT}` so the plugin works regardless of where it's installed.

## Critical Rules (from the agent)

1. Never post comments without explicit user approval in MR mode.
2. Always run adaptive fanout planning before reviewer spawn — Logic Pass 1 + Pass 2 are mandatory; total planned fanout must stay within 4-10 passes.
3. All reviewer passes are `Task(general-purpose)` calls with 10-minute timeout for failure isolation.
4. Candidate findings must pass verification before being shown.
5. Local diff / file / package modes are **report-only** — no posting target exists.

See `agents/ccr.md` for the full workflow specification.

## License

Internal use.
