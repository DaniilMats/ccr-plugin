#!/usr/bin/env python3
"""Poll-friendly CCR run watcher.

Reads a run's status.json and trace.jsonl and returns only the new deltas plus a
compact human-readable progress summary. Intended for agents that launch
`ccr_run.py --detach` and then poll for meaningful updates.

Examples:
    python3 ccr_watch.py --status-file /tmp/ccr/<run>/status.json --trace-file /tmp/ccr/<run>/trace.jsonl
    python3 ccr_watch.py --status-file ... --trace-file ... --since-seq 12 --wait-seconds 15 --emit-heartbeat
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _is_process_alive(pid: int | None) -> bool:
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _read_trace_since(trace_file: Path, since_seq: int) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    try:
        lines = trace_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return events
    for raw_line in lines:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            payload = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        seq = int(payload.get("seq") or 0)
        if seq > since_seq:
            events.append(payload)
    return events


def _format_seconds_short(total_seconds: int | None) -> str:
    if total_seconds is None:
        return "n/a"
    if total_seconds < 60:
        return f"{total_seconds}s"
    minutes, seconds = divmod(total_seconds, 60)
    if minutes < 60:
        return f"{minutes}m" if seconds == 0 else f"{minutes}m{seconds}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h" if minutes == 0 and seconds == 0 else f"{hours}h{minutes}m{seconds}s"


def _format_milliseconds_short(total_ms: int | None) -> str:
    if total_ms is None:
        return "n/a"
    if total_ms < 1000:
        return f"{total_ms}ms"
    seconds = total_ms / 1000.0
    if seconds < 60:
        return f"{seconds:.1f}s"
    return _format_seconds_short(int(round(seconds)))


def _stage_label(current_stage: dict[str, Any] | None) -> str:
    if not current_stage:
        return "(no current stage)"
    name = current_stage.get("name") or "unknown"
    index = current_stage.get("index")
    total = current_stage.get("total")
    if isinstance(index, int) and isinstance(total, int) and total > 0:
        return f"[{index}/{total}] {name}"
    return str(name)


def _summarize_status(status: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    run_id = status.get("run_id") or "unknown"
    state = status.get("state") or "unknown"
    current_stage = status.get("current_stage") if isinstance(status.get("current_stage"), dict) else None
    lines.append(f"Run {run_id}: state={state}, stage={_stage_label(current_stage)}")

    reviewers = status.get("reviewers") if isinstance(status.get("reviewers"), dict) else {}
    planned_reviewers = int(reviewers.get("planned") or 0)
    if planned_reviewers:
        lines.append(
            "Reviewers: {completed}/{planned} completed, {running} running, workers={workers}, est<={estimate}".format(
                completed=int(reviewers.get("completed") or 0),
                planned=planned_reviewers,
                running=int(reviewers.get("running") or 0),
                workers=int(reviewers.get("workers") or 0),
                estimate=_format_seconds_short(int(reviewers.get("estimated_max_duration_sec") or 0)),
            )
        )

    verification = status.get("verification") if isinstance(status.get("verification"), dict) else {}
    planned_batches = int(verification.get("planned_batches") or 0)
    if planned_batches:
        lines.append(
            "Verification: {completed}/{planned} completed, {running} running, workers={workers}, est<={estimate}".format(
                completed=int(verification.get("completed_batches") or 0),
                planned=planned_batches,
                running=int(verification.get("running_batches") or 0),
                workers=int(verification.get("workers") or 0),
                estimate=_format_seconds_short(int(verification.get("estimated_max_duration_sec") or 0)),
            )
        )
    return lines


def _render_event(event: dict[str, Any]) -> str:
    stage = event.get("stage") or "run"
    message = str(event.get("message") or event.get("event") or "update")
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    detail_parts: list[str] = []
    if "pass_name" in data:
        detail_parts.append(str(data["pass_name"]))
    if "batch_id" in data:
        detail_parts.append(str(data["batch_id"]))
    if "provider" in data:
        detail_parts.append(str(data["provider"]))
    if "completed" in data and "planned" in data:
        detail_parts.append(f"{data['completed']}/{data['planned']}")
    if "finding_count" in data:
        detail_parts.append(f"findings={data['finding_count']}")
    if "verified_count" in data:
        detail_parts.append(f"verified={data['verified_count']}")
    if "duration_ms" in data:
        detail_parts.append(_format_milliseconds_short(int(data["duration_ms"])))
    suffix = f" ({', '.join(detail_parts)})" if detail_parts else ""
    return f"[{stage}] {message}{suffix}"


def watch_run(
    *,
    status_file: Path,
    trace_file: Path | None,
    since_seq: int,
    pid: int | None,
    wait_seconds: float,
    poll_interval: float,
    emit_heartbeat: bool,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(wait_seconds, 0)
    last_status: dict[str, Any] | None = None
    last_events: list[dict[str, Any]] = []

    while True:
        status = _read_json(status_file)
        if status is not None:
            last_status = status
            trace_path_from_status = str(status.get("artifacts", {}).get("trace_file") or "").strip()
            effective_trace = trace_file or (Path(trace_path_from_status) if trace_path_from_status else None)
            events = _read_trace_since(effective_trace, since_seq) if effective_trace else []
            last_events = events
            state = str(status.get("state") or "unknown")
            done = state in {"completed", "failed"}
            changed = bool(events)
            if changed or done or time.monotonic() >= deadline:
                display_lines = []
                if emit_heartbeat or changed or done:
                    display_lines.extend(_summarize_status(status))
                for event in events:
                    display_lines.append(_render_event(event))
                current_stage = status.get("current_stage") if isinstance(status.get("current_stage"), dict) else None
                return {
                    "contract_version": "ccr.watch_result.v1",
                    "run_id": status.get("run_id") or status_file.parent.name,
                    "state": state,
                    "done": done,
                    "changed": changed,
                    "pid": int(status.get("pid") or 0) or pid,
                    "revision": int(status.get("revision") or 0),
                    "since_seq": since_seq,
                    "last_seq": int(status.get("event_seq") or 0),
                    "current_stage": current_stage,
                    "reviewers": status.get("reviewers", {}),
                    "verification": status.get("verification", {}),
                    "summary": status.get("summary", {}),
                    "artifacts": status.get("artifacts", {}),
                    "new_events": events,
                    "display_lines": display_lines,
                    "next_poll_sec": 10 if (current_stage or {}).get("name") in {"reviewers", "verification"} else 3,
                }
        else:
            alive = _is_process_alive(pid)
            if time.monotonic() >= deadline or not alive:
                state = "pending" if alive else "failed"
                lines = [
                    "Run status is not available yet." if alive else "Detached CCR process exited before writing status.json."
                ]
                return {
                    "contract_version": "ccr.watch_result.v1",
                    "run_id": status_file.parent.name,
                    "state": state,
                    "done": not alive,
                    "changed": False,
                    "pid": pid,
                    "revision": 0,
                    "since_seq": since_seq,
                    "last_seq": 0,
                    "current_stage": None,
                    "reviewers": {},
                    "verification": {},
                    "summary": {},
                    "artifacts": {
                        "status_file": str(status_file),
                        "trace_file": str(trace_file) if trace_file else "",
                    },
                    "new_events": [],
                    "display_lines": lines,
                    "next_poll_sec": 3,
                }

        if wait_seconds <= 0:
            break
        time.sleep(max(poll_interval, 0.1))
        if time.monotonic() >= deadline and last_status is not None:
            continue

    status = last_status or {}
    return {
        "contract_version": "ccr.watch_result.v1",
        "run_id": status.get("run_id") or status_file.parent.name,
        "state": status.get("state") or "pending",
        "done": (status.get("state") in {"completed", "failed"}) if status else False,
        "changed": bool(last_events),
        "pid": int(status.get("pid") or 0) or pid,
        "revision": int(status.get("revision") or 0),
        "since_seq": since_seq,
        "last_seq": int(status.get("event_seq") or 0),
        "current_stage": status.get("current_stage") if isinstance(status.get("current_stage"), dict) else None,
        "reviewers": status.get("reviewers", {}),
        "verification": status.get("verification", {}),
        "summary": status.get("summary", {}),
        "artifacts": status.get("artifacts", {}),
        "new_events": last_events,
        "display_lines": _summarize_status(status) if emit_heartbeat and status else [],
        "next_poll_sec": 3,
    }


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ccr-watch",
        description="Read CCR status/trace artifacts and return only new progress deltas.",
    )
    parser.add_argument("--status-file", required=True, help="Path to CCR status.json.")
    parser.add_argument("--trace-file", default=None, help="Optional path to CCR trace.jsonl. Auto-detected from status.json when omitted.")
    parser.add_argument("--since-seq", type=int, default=0, help="Only return trace events with seq > since-seq.")
    parser.add_argument("--pid", type=int, default=None, help="Optional detached harness pid. Used when status.json is not ready yet.")
    parser.add_argument("--wait-seconds", type=float, default=0, help="Wait up to this many seconds for a status change or new trace event.")
    parser.add_argument("--poll-interval", type=float, default=1.0, help="Polling interval while waiting (default: 1.0s).")
    parser.add_argument("--emit-heartbeat", action="store_true", help="Always include a snapshot summary line even when there are no new events.")
    return parser


def main() -> None:
    args = _build_arg_parser().parse_args()
    payload = watch_run(
        status_file=Path(args.status_file).expanduser().resolve(),
        trace_file=Path(args.trace_file).expanduser().resolve() if args.trace_file else None,
        since_seq=args.since_seq,
        pid=args.pid,
        wait_seconds=args.wait_seconds,
        poll_interval=args.poll_interval,
        emit_heartbeat=args.emit_heartbeat,
    )
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
