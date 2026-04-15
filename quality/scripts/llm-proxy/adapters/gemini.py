"""Gemini CLI adapter for llm-proxy."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from adapters.base import BaseAdapter, ProxyResponse


class GeminiAdapter(BaseAdapter):
    """Adapter for the Google Gemini CLI (`gemini` command)."""

    PROVIDER = "gemini"
    DEFAULT_MODEL = "gemini-3.1-pro-preview"
    DEFAULT_MODEL_ALIAS = "ccr-gemini-3.1-pro-thinking-high"
    _RUNTIME_HOME_SKIP_DIRS = frozenset({"history", "tmp"})

    def __init__(self, thread_dir: str = ""):
        super().__init__(thread_dir)

    def _source_home_root(self) -> Path:
        configured_home = str(os.environ.get("GEMINI_CLI_HOME") or "").strip()
        if configured_home:
            return Path(configured_home).expanduser()
        return Path.home()

    def _source_settings_dir(self) -> Path:
        return self._source_home_root() / ".gemini"

    def _copy_runtime_home(self, runtime_root: Path) -> None:
        source_dir = self._source_settings_dir()
        destination_dir = runtime_root / ".gemini"
        destination_dir.mkdir(parents=True, exist_ok=True)
        if not source_dir.is_dir():
            return
        for child in source_dir.iterdir():
            if child.name in self._RUNTIME_HOME_SKIP_DIRS:
                continue
            target = destination_dir / child.name
            if child.is_dir():
                shutil.copytree(child, target, dirs_exist_ok=True)
            elif child.is_file():
                shutil.copy2(child, target)

    def _load_settings_payload(self, settings_path: Path) -> dict:
        if not settings_path.is_file():
            return {}
        try:
            payload = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _thinking_alias_payload(self) -> dict:
        return {
            "modelConfig": {
                "model": self.DEFAULT_MODEL,
                "generateContentConfig": {
                    "temperature": 1,
                    "topP": 0.95,
                    "topK": 64,
                    "thinkingConfig": {
                        "thinkingLevel": "HIGH",
                    },
                },
            }
        }

    def _prepare_runtime_home(self):
        runtime_home = None
        try:
            runtime_home = tempfile.TemporaryDirectory(prefix="gemini-cli-home-")
            runtime_root = Path(runtime_home.name)
            self._copy_runtime_home(runtime_root)

            settings_path = runtime_root / ".gemini" / "settings.json"
            payload = self._load_settings_payload(settings_path)
            model_configs = payload.get("modelConfigs")
            if not isinstance(model_configs, dict):
                model_configs = {}
                payload["modelConfigs"] = model_configs
            custom_aliases = model_configs.get("customAliases")
            if not isinstance(custom_aliases, dict):
                custom_aliases = {}
                model_configs["customAliases"] = custom_aliases
            custom_aliases[self.DEFAULT_MODEL_ALIAS] = self._thinking_alias_payload()
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            settings_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            return runtime_home, self.DEFAULT_MODEL_ALIAS
        except (OSError, shutil.Error):
            if runtime_home is not None:
                runtime_home.cleanup()
            return None, self.DEFAULT_MODEL

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
        env_extras: dict = {}
        runtime_home, effective_model = self._prepare_runtime_home()
        if runtime_home is not None:
            env_extras["GEMINI_CLI_HOME"] = runtime_home.name
        cmd = ["gemini", "-m", effective_model, "-p", full_prompt, "-s", "-o", "text"]

        # Thread resume: gemini uses session IDs; pass via env var if supported
        if thread_id:
            env_extras["GEMINI_SESSION_ID"] = thread_id

        try:
            stdout, stderr, returncode, timed_out = self._run_subprocess(
                cmd, timeout, env=env_extras if env_extras else None
            )
        finally:
            if runtime_home is not None:
                runtime_home.cleanup()

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
