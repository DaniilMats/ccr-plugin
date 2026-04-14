#!/usr/bin/env python3
"""Poll-friendly CCR run watcher.

Reads a run's status.json and trace.jsonl and returns only the new deltas plus a
compact human-readable progress summary. Supports quiet text mode, cursor files,
and follow mode for Monitor/background streaming.

Examples:
    python3 ccr_watch.py --status-file /tmp/ccr/<run>/status.json --trace-file /tmp/ccr/<run>/trace.jsonl
    python3 ccr_watch.py --status-file ... --trace-file ... --cursor-file /tmp/ccr/<run>/watch_cursor.json --format text --quiet-unchanged --follow --wait-seconds 15 --emit-heartbeat
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any


_CURSOR_CONTRACT_VERSION = "ccr.watch_cursor.v1"


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


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    tmp_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp_path, path)


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


def _compact_artifacts(artifacts: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(artifacts, dict):
        return {}
    keys = (
        "status_file",
        "trace_file",
        "summary_file",
        "report_file",
        "reviewers_file",
        "candidates_file",
        "verified_findings_file",
    )
    return {
        key: artifacts[key]
        for key in keys
        if key in artifacts and artifacts[key]
    }


def _compact_progress(progress: dict[str, Any], *, verification: bool = False) -> dict[str, Any]:
    if not isinstance(progress, dict):
        return {}
    if verification:
        keys = (
            "planned_batches",
            "running_batches",
            "completed_batches",
            "succeeded_batches",
            "failed_batches",
            "workers",
            "timeout_sec",
            "estimated_max_duration_sec",
        )
    else:
        keys = (
            "planned",
            "running",
            "completed",
            "succeeded",
            "failed",
            "workers",
            "timeout_sec",
            "estimated_max_duration_sec",
        )
    return {
        key: progress[key]
        for key in keys
        if key in progress
    }


def _compact_event(event: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {
        "seq": int(event.get("seq") or 0),
        "ts": event.get("ts"),
        "event": event.get("event"),
        "stage": event.get("stage"),
        "message": event.get("message"),
    }
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    allowed_data_keys = {
        "mode",
        "target",
        "project_dir",
        "summary",
        "source",
        "has_requirements",
        "changed_file_count",
        "changed_lines",
        "planned",
        "running",
        "completed",
        "succeeded",
        "failed",
        "workers",
        "pass_name",
        "batch_id",
        "provider",
        "finding_count",
        "verified_count",
        "batch_count",
        "status",
        "full_matrix",
        "duration_ms",
        "report_file",
    }
    compact_data = {
        key: value
        for key, value in data.items()
        if key in allowed_data_keys and value not in (None, "", [], {})
    }
    if compact_data:
        compact["data"] = compact_data
    return compact


def _compact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "contract_version": payload.get("contract_version"),
        "run_id": payload.get("run_id"),
        "state": payload.get("state"),
        "done": payload.get("done"),
        "changed": payload.get("changed"),
        "pid": payload.get("pid"),
        "revision": payload.get("revision"),
        "since_seq": payload.get("since_seq"),
        "last_seq": payload.get("last_seq"),
        "current_stage": payload.get("current_stage"),
        "reviewers": _compact_progress(payload.get("reviewers", {}), verification=False),
        "verification": _compact_progress(payload.get("verification", {}), verification=True),
        "summary": payload.get("summary", {}),
        "artifacts": _compact_artifacts(payload.get("artifacts", {})),
        "new_events": [_compact_event(event) for event in payload.get("new_events", [])],
        "display_lines": payload.get("display_lines", []),
        "next_poll_sec": payload.get("next_poll_sec"),
    }


def _load_cursor(cursor_file: Path | None) -> dict[str, Any]:
    if cursor_file is None:
        return {}
    payload = _read_json(cursor_file)
    return payload if payload else {}


def _write_cursor(cursor_file: Path | None, payload: dict[str, Any]) -> None:
    if cursor_file is None:
        return
    cursor_payload = {
        "contract_version": _CURSOR_CONTRACT_VERSION,
        "run_id": payload.get("run_id"),
        "last_seq": int(payload.get("last_seq") or 0),
        "revision": int(payload.get("revision") or 0),
        "state": payload.get("state"),
        "done": bool(payload.get("done")),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _write_json_atomic(cursor_file, cursor_payload)


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
                display_lines: list[str] = []
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


def _render_payload(
    payload: dict[str, Any],
    *,
    output_format: str,
    quiet_unchanged: bool,
    cursor_before: dict[str, Any] | None = None,
) -> str:
    previous_last_seq = int((cursor_before or {}).get("last_seq") or 0)
    previous_done = bool((cursor_before or {}).get("done"))
    current_last_seq = int(payload.get("last_seq") or 0)
    already_consumed_done = previous_done and current_last_seq <= previous_last_seq

    if quiet_unchanged and not payload.get("changed") and (not payload.get("done") or already_consumed_done):
        return ""

    if output_format == "text":
        lines = payload.get("display_lines") if isinstance(payload.get("display_lines"), list) else []
        return "\n".join(str(line) for line in lines if str(line).strip())

    if output_format == "compact-json":
        return json.dumps(_compact_payload(payload), indent=2)

    return json.dumps(payload, indent=2)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ccr-watch",
        description="Read CCR status/trace artifacts and return only new progress deltas.",
    )
    parser.add_argument("--status-file", required=True, help="Path to CCR status.json.")
    parser.add_argument("--trace-file", default=None, help="Optional path to CCR trace.jsonl. Auto-detected from status.json when omitted.")
    parser.add_argument("--since-seq", type=int, default=None, help="Only return trace events with seq > since-seq. If omitted, --cursor-file is used when present.")
    parser.add_argument("--cursor-file", default=None, help="Optional file that stores the last consumed trace seq for quiet repeated polling.")
    parser.add_argument("--pid", type=int, default=None, help="Optional detached harness pid. Used when status.json is not ready yet.")
    parser.add_argument("--wait-seconds", type=float, default=0, help="Wait up to this many seconds for a status change or new trace event.")
    parser.add_argument("--poll-interval", type=float, default=1.0, help="Polling interval while waiting (default: 1.0s).")
    parser.add_argument("--emit-heartbeat", action="store_true", help="Include a snapshot summary line when there are no new events.")
    parser.add_argument("--format", choices=["compact-json", "json", "text"], default="compact-json", help="Output format (default: compact-json).")
    parser.add_argument("--quiet-unchanged", action="store_true", help="Print nothing when there is no new progress, including already-consumed terminal states via --cursor-file.")
    parser.add_argument("--follow", action="store_true", help="Keep polling until the run finishes, emitting deltas as they arrive.")
    return parser


def main() -> None:
    args = _build_arg_parser().parse_args()
    status_file = Path(args.status_file).expanduser().resolve()
    trace_file = Path(args.trace_file).expanduser().resolve() if args.trace_file else None
    cursor_file = Path(args.cursor_file).expanduser().resolve() if args.cursor_file else None

    cursor_before = _load_cursor(cursor_file)
    current_since = args.since_seq if args.since_seq is not None else int(cursor_before.get("last_seq") or 0)

    while True:
        payload = watch_run(
            status_file=status_file,
            trace_file=trace_file,
            since_seq=current_since,
            pid=args.pid,
            wait_seconds=args.wait_seconds,
            poll_interval=args.poll_interval,
            emit_heartbeat=args.emit_heartbeat,
        )
        _write_cursor(cursor_file, payload)
        rendered = _render_payload(
            payload,
            output_format=args.format,
            quiet_unchanged=args.quiet_unchanged,
            cursor_before=cursor_before,
        )
        if rendered:
            print(rendered)
            sys.stdout.flush()

        current_since = int(payload.get("last_seq") or current_since)
        cursor_before = _load_cursor(cursor_file)
        if not args.follow or payload.get("done"):
            break
        if args.wait_seconds <= 0:
            time.sleep(max(float(payload.get("next_poll_sec") or 1), args.poll_interval, 0.1))


if __name__ == "__main__":
    main()
