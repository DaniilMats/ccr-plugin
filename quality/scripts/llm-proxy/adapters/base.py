"""Abstract base adapter and shared data structures for llm-proxy."""
from __future__ import annotations

import os
import signal
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class ProxyResponse:
    """Unified response structure returned by all adapters."""
    response: str = ""
    thread_id: Optional[str] = None
    tokens: int = 0
    duration_ms: int = 0
    exit_code: int = 0
    error: Optional[str] = None
    timed_out: bool = False
    schema_valid: bool = True
    schema_retries: int = 0
    schema_violations: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = {
            "response": self.response,
            "thread_id": self.thread_id,
            "tokens": self.tokens,
            "duration_ms": self.duration_ms,
            "exit_code": self.exit_code,
            "error": self.error,
            "timed_out": self.timed_out,
            "schema_valid": self.schema_valid,
            "schema_retries": self.schema_retries,
        }
        if self.schema_violations:
            d["schema_violations"] = self.schema_violations
        return d


class BaseAdapter(ABC):
    """Abstract base class for LLM CLI adapters."""

    def __init__(self, thread_dir: str):
        self.thread_dir = thread_dir

    @abstractmethod
    def run(
        self,
        prompt: str,
        scope: Optional[str] = None,
        thread_id: Optional[str] = None,
        timeout: int = 300,
    ) -> ProxyResponse:
        """Execute the LLM with the given prompt and return a ProxyResponse."""

    def _load_thread_id(self, session: str, slug: str, provider: str) -> Optional[str]:
        """Load persisted thread ID from disk."""
        path = self._thread_path(session, slug, provider)
        if os.path.exists(path):
            with open(path) as f:
                tid = f.read().strip()
                return tid if tid else None
        return None

    def _save_thread_id(self, session: str, slug: str, provider: str, thread_id: str) -> None:
        """Persist thread ID to disk."""
        path = self._thread_path(session, slug, provider)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(thread_id)

    def _thread_path(self, session: str, slug: str, provider: str) -> str:
        base = os.path.expanduser("~/.claude/llm-proxy/threads")
        return os.path.join(base, session, slug, provider, "thread_id")

    def _build_scope_context(self, scope: Optional[str]) -> str:
        """Generate scope context prefix for the prompt."""
        if not scope:
            return ""
        if scope.startswith("commit:"):
            sha = scope[len("commit:"):]
            return f"[Review scope: git commit {sha}]\n\n"
        elif scope.startswith("branch:"):
            base = scope[len("branch:"):]
            return f"[Review scope: git diff from branch base {base}]\n\n"
        elif scope == "uncommitted":
            return "[Review scope: uncommitted changes (git diff HEAD)]\n\n"
        elif scope.startswith("file:"):
            path = scope[len("file:"):]
            return f"[Review scope: file {path}]\n\n"
        return ""

    def _run_subprocess(
        self,
        cmd: List[str],
        timeout: int,
        env: Optional[dict] = None,
        input_text: Optional[str] = None,
    ) -> tuple:
        """
        Run a subprocess with timeout using Popen + start_new_session=True.
        Returns (stdout, stderr, returncode, timed_out).
        NEVER uses asyncio.create_subprocess_exec.
        """
        start = time.time()
        proc_env = os.environ.copy()
        if env:
            proc_env.update(env)

        stdin_pipe = subprocess.PIPE if input_text is not None else None

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=stdin_pipe,
                env=proc_env,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            elapsed = int((time.time() - start) * 1000)
            return "", str(exc), 127, False

        try:
            stdin_bytes = input_text.encode() if input_text is not None else None
            stdout_bytes, stderr_bytes = proc.communicate(input=stdin_bytes, timeout=timeout)
            elapsed = int((time.time() - start) * 1000)
            return (
                stdout_bytes.decode(errors="replace"),
                stderr_bytes.decode(errors="replace"),
                proc.returncode,
                False,
            )
        except subprocess.TimeoutExpired:
            # Kill entire process group
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                proc.kill()
            proc.communicate()
            elapsed = int((time.time() - start) * 1000)
            return "", "Timed out after {} seconds".format(timeout), -1, True
