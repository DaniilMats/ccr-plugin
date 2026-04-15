from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_MISSING = object()


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def duration_ms(started_at: float) -> int:
    return int((time.monotonic() - started_at) * 1000)


def estimate_parallel_stage_duration(total_items: int, worker_count: int, timeout_sec: int) -> int:
    if total_items <= 0 or worker_count <= 0:
        return 0
    waves = (total_items + worker_count - 1) // worker_count
    return waves * max(timeout_sec, 0)


def resolve_worker_count(requested_workers: int, total_items: int, *, auto_cap: int) -> int:
    if total_items <= 0:
        return 0
    if requested_workers > 0:
        return max(1, min(total_items, requested_workers))
    return max(1, min(total_items, auto_cap))


def format_seconds_short(total_seconds: int | None) -> str:
    if total_seconds is None:
        return "n/a"
    if total_seconds < 60:
        return f"{total_seconds}s"
    minutes, seconds = divmod(total_seconds, 60)
    if minutes < 60:
        return f"{minutes}m" if seconds == 0 else f"{minutes}m{seconds}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h" if minutes == 0 and seconds == 0 else f"{hours}h{minutes}m{seconds}s"


def format_milliseconds_short(total_ms: int | None) -> str:
    if total_ms is None:
        return "n/a"
    if total_ms < 1000:
        return f"{total_ms}ms"
    seconds = total_ms / 1000.0
    if seconds < 60:
        return f"{seconds:.1f}s"
    return format_seconds_short(int(round(seconds)))


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(read_text(path))


def load_json_file(path: Path, *, default: Any = _MISSING) -> Any:
    if not path.is_file():
        if default is _MISSING:
            raise FileNotFoundError(f"JSON file not found: {path}")
        return default
    try:
        return read_json(path)
    except json.JSONDecodeError as exc:
        if default is not _MISSING:
            return default
        raise ValueError(f"invalid JSON in {path}: {exc}") from exc


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp_path, path)


def dedupe_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered


def ratio(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 4)


def display_path(path: Path, *, relative_to: Path) -> str:
    try:
        return str(path.resolve().relative_to(relative_to.resolve()))
    except ValueError:
        return str(path.resolve())


def run_command(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
