"""Claude CLI adapter for llm-proxy — Opus reviewer via `claude --print`."""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from adapters.base import BaseAdapter, ProxyResponse


class ClaudeAdapter(BaseAdapter):
    """Adapter for the Anthropic Claude CLI (`claude --print`) in one-shot mode.

    Uses Opus with max effort for review. Falls back to the user's default
    Claude Code authentication (OAuth / Pro subscription via keychain, or
    ANTHROPIC_API_KEY from env). The 1M-context beta is opt-in and only
    requested when an API key is present — beta headers are rejected for
    OAuth users.
    """

    PROVIDER = "claude"
    DEFAULT_MODEL = "opus"
    DEFAULT_EFFORT = "max"
    CONTEXT_1M_BETA = "context-1m-2025-08-07"

    REVIEWER_SYSTEM_PROMPT = (
        "You are a specialized static code reviewer. "
        "Follow the instructions provided in the user message verbatim. "
        "Output ONLY the JSON object requested by the instructions — "
        "no markdown fences, no surrounding commentary, no tool calls."
    )

    def __init__(self, thread_dir: str = ""):
        super().__init__(thread_dir)

    def run(
        self,
        prompt: str,
        scope: Optional[str] = None,
        thread_id: Optional[str] = None,
        timeout: int = 900,
    ) -> ProxyResponse:
        start = time.time()
        scope_ctx = self._build_scope_context(scope)
        full_prompt = scope_ctx + prompt

        cmd = [
            "claude",
            "--print",
            "--model", self.DEFAULT_MODEL,
            "--effort", self.DEFAULT_EFFORT,
            "--output-format", "json",
            "--tools", "",
            "--no-session-persistence",
            "--disable-slash-commands",
            "--exclude-dynamic-system-prompt-sections",
            "--system-prompt", self.REVIEWER_SYSTEM_PROMPT,
        ]

        # Beta headers only work with API key auth, not OAuth/Pro
        if os.environ.get("ANTHROPIC_API_KEY"):
            cmd += ["--betas", self.CONTEXT_1M_BETA]

        stdout, stderr, returncode, timed_out = self._run_subprocess(
            cmd, timeout, input_text=full_prompt
        )

        duration_ms = int((time.time() - start) * 1000)

        if timed_out:
            return ProxyResponse(
                response="",
                thread_id=thread_id,
                tokens=0,
                duration_ms=duration_ms,
                exit_code=-1,
                error="Timed out after {} seconds".format(timeout),
                timed_out=True,
            )

        if returncode == 127 or "not found" in stderr.lower() or "no such file" in stderr.lower():
            return ProxyResponse(
                response="",
                thread_id=thread_id,
                tokens=0,
                duration_ms=duration_ms,
                exit_code=returncode,
                error=(
                    "claude CLI not found. Install Claude Code: "
                    "https://claude.com/claude-code"
                ),
                timed_out=False,
            )

        response_text = ""
        parsed_thread_id = thread_id
        tokens = 0

        raw = stdout.strip()
        if raw:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = None

            if isinstance(data, dict):
                if data.get("is_error"):
                    error_msg = (
                        data.get("error")
                        or data.get("result")
                        or "claude returned error result"
                    )
                    return ProxyResponse(
                        response="",
                        thread_id=thread_id,
                        tokens=0,
                        duration_ms=duration_ms,
                        exit_code=returncode or 1,
                        error=error_msg,
                        timed_out=False,
                    )
                response_text = data.get("result") or data.get("response") or ""
                parsed_thread_id = data.get("session_id") or thread_id
                usage = data.get("usage") or {}
                tokens = (
                    usage.get("total_tokens")
                    or usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
                    or data.get("total_tokens", 0)
                )
            else:
                response_text = raw

        if returncode != 0 and not response_text:
            error_msg = stderr.strip() or "claude exited with code {}".format(returncode)
            return ProxyResponse(
                response="",
                thread_id=thread_id,
                tokens=0,
                duration_ms=duration_ms,
                exit_code=returncode,
                error=error_msg,
                timed_out=False,
            )

        return ProxyResponse(
            response=response_text,
            thread_id=parsed_thread_id,
            tokens=tokens,
            duration_ms=duration_ms,
            exit_code=returncode,
            error=stderr.strip() if returncode != 0 else None,
            timed_out=False,
        )
