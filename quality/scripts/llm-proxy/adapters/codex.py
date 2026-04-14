"""Codex CLI adapter for llm-proxy."""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from adapters.base import BaseAdapter, ProxyResponse


class CodexAdapter(BaseAdapter):
    """Adapter for the OpenAI Codex CLI (`codex` command)."""

    PROVIDER = "codex"

    def __init__(self, thread_dir: str = ""):
        super().__init__(thread_dir)

    def run(
        self,
        prompt: str,
        scope: Optional[str] = None,
        thread_id: Optional[str] = None,
        timeout: int = 300,
    ) -> ProxyResponse:
        start = time.time()
        scope_ctx = self._build_scope_context(scope)
        full_prompt = scope_ctx + prompt

        # Build codex command
        cmd = ["codex", "exec", "-c", "model=gpt-5.4", "--sandbox", "read-only"]

        # Thread resume: pass thread ID via env var if codex supports it
        env_extras: dict = {}
        if thread_id:
            env_extras["CODEX_THREAD_ID"] = thread_id

        # Use a temp output file for structured output
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, prefix="codex_out_") as tmp:
            out_file = tmp.name

        cmd += ["-o", out_file]
        cmd += ["--", full_prompt]

        stdout, stderr, returncode, timed_out = self._run_subprocess(
            cmd, timeout, env=env_extras if env_extras else None
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

        # Try to read output file
        response_text = ""
        parsed_thread_id = thread_id
        tokens = 0

        if returncode == 0 or os.path.exists(out_file):
            try:
                with open(out_file) as f:
                    raw_content = f.read().strip()
            except Exception:
                raw_content = ""

            if raw_content:
                # Try to parse as JSON envelope from codex
                try:
                    data = json.loads(raw_content)
                    if isinstance(data, dict):
                        # Check for codex envelope keys
                        envelope_text = (
                            data.get("output")
                            or data.get("response")
                            or data.get("text")
                        )
                        if envelope_text is not None:
                            response_text = envelope_text
                        else:
                            # Raw LLM response written directly — return as-is
                            response_text = raw_content
                        parsed_thread_id = data.get("thread_id") or thread_id
                        tokens = data.get("tokens") or data.get("usage", {}).get("total_tokens", 0)
                    else:
                        # Non-dict JSON (array, string, etc.) — use raw content
                        response_text = raw_content
                except json.JSONDecodeError:
                    # Not JSON — plain text response
                    response_text = raw_content
            elif stdout.strip():
                # Output file empty but stdout has content
                response_text = stdout.strip()

        # Clean up temp file
        try:
            os.unlink(out_file)
        except OSError:
            pass

        if returncode != 0 and not response_text:
            # codex not installed or failed
            error_msg = stderr.strip() or "codex exited with code {}".format(returncode)
            # If not found (127), provide clear message
            if returncode == 127 or "not found" in stderr.lower() or "no such file" in stderr.lower():
                error_msg = "codex CLI not found. Install via: npm install -g @openai/codex"
            return ProxyResponse(
                response=stdout.strip(),
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
