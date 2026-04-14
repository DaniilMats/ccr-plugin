"""Gemini CLI adapter for llm-proxy."""
from __future__ import annotations

import os
import sys
import time
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from adapters.base import BaseAdapter, ProxyResponse


class GeminiAdapter(BaseAdapter):
    """Adapter for the Google Gemini CLI (`gemini` command)."""

    PROVIDER = "gemini"

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

        # Build gemini command
        cmd = ["gemini", "-m", "gemini-3.1-pro-preview", "-p", full_prompt, "-s", "-o", "text"]

        # Thread resume: gemini uses session IDs; pass via env var if supported
        env_extras: dict = {}
        if thread_id:
            env_extras["GEMINI_SESSION_ID"] = thread_id

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

        if returncode != 0 and not stdout.strip():
            error_msg = stderr.strip() or "gemini exited with code {}".format(returncode)
            if returncode == 127 or "not found" in stderr.lower() or "no such file" in stderr.lower():
                error_msg = "gemini CLI not found. Install via: pip install google-generativeai or npm install -g @google-ai/gemini-cli"
            return ProxyResponse(
                response=stdout.strip(),
                thread_id=thread_id,
                tokens=0,
                duration_ms=duration_ms,
                exit_code=returncode,
                error=error_msg,
                timed_out=False,
            )

        response_text = stdout.strip()

        # Attempt to extract token count from stderr (some CLI versions emit usage there)
        tokens = 0
        for line in stderr.splitlines():
            line_lower = line.lower()
            if "token" in line_lower:
                parts = line.split()
                for i, p in enumerate(parts):
                    if "token" in p.lower() and i > 0:
                        try:
                            tokens = int(parts[i - 1])
                            break
                        except ValueError:
                            pass

        return ProxyResponse(
            response=response_text,
            thread_id=thread_id,
            tokens=tokens,
            duration_ms=duration_ms,
            exit_code=returncode,
            error=stderr.strip() if returncode != 0 else None,
            timed_out=False,
        )
