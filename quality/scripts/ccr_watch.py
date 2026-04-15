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

_STAGE_LABELS = {
    "bootstrap": "Bootstrap",
    "artifact_preparation": "Artifact",
    "requirements": "Requirements",
    "routing": "Routing",
    "review_context": "Context",
    "static_analysis": "Static analysis",
    "shuffle_diff": "Shuffled diff",
    "reviewers": "Reviewers",
    "candidates": "Candidates",
    "verification": "Verification",
    "report": "Report",
    "completed": "Completed",
    "failed": "Failed",
}

_STATE_ICONS = {
    "pending": "⏳",
    "running": "⏳",
    "completed": "✅",
    "failed": "✖",
}

_STAGE_ICONS = {
    "artifact_preparation": "📦",
    "requirements": "📋",
    "routing": "🧭",
    "review_context": "📚",
    "static_analysis": "🔎",
    "shuffle_diff": "🔀",
    "reviewers": "▶",
    "candidates": "🧩",
    "verification": "✓",
    "report": "📝",
    "completed": "✅",
    "failed": "✖",
}


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


def _short_run_id(run_id: Any) -> str:
    text = str(run_id or "unknown")
    if "-" in text:
        tail = text.rsplit("-", 1)[-1]
        if tail:
            return tail
    return text


def _stage_name(stage: str | None) -> str:
    key = str(stage or "")
    return _STAGE_LABELS.get(key, key.replace("_", " ").title() if key else "Unknown")


def _stage_label(current_stage: dict[str, Any] | None) -> str:
    if not current_stage:
        return "(no current stage)"
    name = _stage_name(str(current_stage.get("name") or "unknown"))
    index = current_stage.get("index")
    total = current_stage.get("total")
    if isinstance(index, int) and isinstance(total, int) and total > 0:
        return f"{name} [{index}/{total}]"
    return str(name)


def _format_snapshot_line(kind: str, progress: dict[str, Any], *, verification: bool = False) -> str | None:
    if not isinstance(progress, dict):
        return None
    if verification:
        planned = int(progress.get("planned_batches") or 0)
        completed = int(progress.get("completed_batches") or 0)
        running = int(progress.get("running_batches") or 0)
    else:
        planned = int(progress.get("planned") or 0)
        completed = int(progress.get("completed") or 0)
        running = int(progress.get("running") or 0)
    if planned <= 0:
        return None
    workers = int(progress.get("workers") or 0)
    estimate = _format_seconds_short(int(progress.get("estimated_max_duration_sec") or 0))
    return f"{kind} {completed}/{planned} complete · {running} running · workers={workers} · est≤{estimate}"


def _summarize_status(status: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    run_id = _short_run_id(status.get("run_id"))
    state = str(status.get("state") or "unknown")
    current_stage = status.get("current_stage") if isinstance(status.get("current_stage"), dict) else None
    icon = _STATE_ICONS.get(state, "ℹ")
    lines.append(f"{icon} CCR {run_id} · {state} · {_stage_label(current_stage)}")

    reviewer_line = _format_snapshot_line("▶ Reviewers", status.get("reviewers", {}), verification=False)
    if reviewer_line:
        lines.append(reviewer_line)

    verification_line = _format_snapshot_line("✓ Verification", status.get("verification", {}), verification=True)
    if verification_line:
        lines.append(verification_line)
    return lines


def _basename(path_value: Any) -> str:
    text = str(path_value or "").strip()
    return Path(text).name if text else ""


def _format_stage_event(event: dict[str, Any]) -> str | None:
    stage = str(event.get("stage") or "")
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    icon = _STAGE_ICONS.get(stage, "ℹ")
    if event.get("event") == "stage_started":
        if stage in {"reviewers", "verification"}:
            return None
        return f"{icon} {_stage_name(stage)} started"
    if event.get("event") != "stage_completed":
        return None
    if stage == "artifact_preparation":
        return f"📦 Artifact ready · files={int(data.get('changed_file_count') or 0)} · lines={int(data.get('changed_lines') or 0)}"
    if stage == "requirements":
        source = str(data.get("source") or "none")
        has_requirements = bool(data.get("has_requirements"))
        return f"📋 Requirements ready · source={source} · provided={str(has_requirements).lower()}"
    if stage == "routing":
        return f"🧭 Routing ready · planned={int(data.get('planned') or 0)} · full_matrix={str(bool(data.get('full_matrix'))).lower()}"
    if stage == "review_context":
        context_status = str(data.get("context_status") or "unknown")
        return f"📚 Context ready · status={context_status}"
    if stage == "static_analysis":
        return f"🔎 Static analysis ready · findings={int(data.get('total_findings') or 0)}"
    if stage == "shuffle_diff":
        return "🔀 Shuffled diff ready"
    if stage == "reviewers":
        return f"▶ Reviewers finished · succeeded={int(data.get('succeeded') or 0)} · failed={int(data.get('failed') or 0)} · findings={int(data.get('finding_count') or 0)}"
    if stage == "candidates":
        return f"🧩 Candidates ready · candidates={int(data.get('candidate_count') or 0)} · source_findings={int(data.get('source_finding_count') or 0)}"
    if stage == "verification":
        return f"✓ Verification finished · verified={int(data.get('verified_count') or 0)} · batches={int(data.get('batch_count') or 0)}"
    if stage == "report":
        report_file = _basename(data.get("report_file"))
        report_suffix = f" · report={report_file}" if report_file else ""
        return f"📝 Report ready · verified={int(data.get('verified_count') or 0)}{report_suffix}"
    duration_ms = data.get("duration_ms")
    duration_suffix = f" · {_format_milliseconds_short(int(duration_ms))}" if duration_ms is not None else ""
    return f"{icon} {_stage_name(stage)} finished{duration_suffix}"


def _aggregate_reviewer_events(events: list[dict[str, Any]], reviewers: dict[str, Any]) -> list[str]:
    completed_events = [event for event in events if event.get("event") == "reviewer_completed"]
    if not completed_events:
        return []
    last_data = completed_events[-1].get("data") if isinstance(completed_events[-1].get("data"), dict) else {}
    completed = int(last_data.get("completed") or reviewers.get("completed") or 0)
    planned = int(last_data.get("planned") or reviewers.get("planned") or 0)
    running = int(last_data.get("running") or reviewers.get("running") or 0)
    delta = len(completed_events)
    lines = [f"▶ Reviewers +{delta} ⇒ {completed}/{planned} complete · {running} running"]

    failed = [event for event in completed_events if str((event.get("data") or {}).get("status") or "") != "succeeded"]
    if failed:
        names = ", ".join(str((event.get("data") or {}).get("pass_name") or "unknown") for event in failed[:3])
        extra = "" if len(failed) <= 3 else f" +{len(failed) - 3} more"
        lines.append(f"✖ Reviewer failures: {len(failed)} pass(es) · {names}{extra}")

    finding_events = [
        event
        for event in completed_events
        if int(((event.get("data") or {}).get("finding_count") or 0)) > 0
    ]
    if finding_events:
        total_findings = sum(int(((event.get("data") or {}).get("finding_count") or 0)) for event in finding_events)
        names = ", ".join(str((event.get("data") or {}).get("pass_name") or "unknown") for event in finding_events[:3])
        extra = "" if len(finding_events) <= 3 else f" +{len(finding_events) - 3} more"
        lines.append(f"⚠ Reviewer signals: {total_findings} finding(s) · {names}{extra}")
    return lines


def _aggregate_verification_events(events: list[dict[str, Any]], verification: dict[str, Any]) -> list[str]:
    completed_events = [event for event in events if event.get("event") == "verification_batch_completed"]
    if not completed_events:
        return []
    last_data = completed_events[-1].get("data") if isinstance(completed_events[-1].get("data"), dict) else {}
    completed = int(last_data.get("completed") or verification.get("completed_batches") or 0)
    planned = int(last_data.get("planned") or verification.get("planned_batches") or 0)
    running = int(last_data.get("running") or verification.get("running_batches") or 0)
    delta = len(completed_events)
    lines = [f"✓ Verification +{delta} ⇒ {completed}/{planned} complete · {running} running"]

    failed = [event for event in completed_events if str((event.get("data") or {}).get("status") or "") != "succeeded"]
    if failed:
        names = ", ".join(str((event.get("data") or {}).get("batch_id") or "unknown") for event in failed[:3])
        extra = "" if len(failed) <= 3 else f" +{len(failed) - 3} more"
        lines.append(f"✖ Verification failures: {len(failed)} batch(es) · {names}{extra}")
    return lines


def _format_misc_event(event: dict[str, Any]) -> str | None:
    event_name = str(event.get("event") or "")
    stage = str(event.get("stage") or "")
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    if event_name in {"reviewer_started", "verification_batch_started"}:
        return None
    if event_name == "reviewers_started":
        return "▶ Reviewers launched · planned={planned} · workers={workers} · est≤{estimate}".format(
            planned=int(data.get("planned") or 0),
            workers=int(data.get("workers") or 0),
            estimate=_format_seconds_short(int(data.get("estimated_max_duration_sec") or 0)),
        )
    if event_name == "verification_started":
        return "✓ Verification launched · batches={planned} · workers={workers} · est≤{estimate}".format(
            planned=int(data.get("planned") or 0),
            workers=int(data.get("workers") or 0),
            estimate=_format_seconds_short(int(data.get("estimated_max_duration_sec") or 0)),
        )
    if event_name == "run_completed":
        report_file = _basename(data.get("report_file"))
        report_suffix = f" · report={report_file}" if report_file else ""
        return f"✅ CCR complete · verified={int(data.get('verified_count') or 0)}{report_suffix}"
    if event_name == "run_failed":
        return f"✖ CCR failed · {str(event.get('message') or 'unknown error')}"
    if event_name in {"stage_started", "stage_completed"}:
        return _format_stage_event(event)
    message = str(event.get("message") or event_name or "update")
    icon = _STAGE_ICONS.get(stage, "ℹ")
    return f"{icon} {message}"


def _dedupe_preserve_order(lines: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for line in lines:
        if not line or line in seen:
            continue
        seen.add(line)
        result.append(line)
    return result


def _render_display_lines(status: dict[str, Any], events: list[dict[str, Any]], *, emit_heartbeat: bool, done: bool) -> list[str]:
    lines: list[str] = []
    if emit_heartbeat or events or done:
        lines.append(_summarize_status(status)[0])

    reviewer_progress_lines = _aggregate_reviewer_events(events, status.get("reviewers", {}))
    verification_progress_lines = _aggregate_verification_events(events, status.get("verification", {}))

    misc_lines: list[str] = []
    for event in events:
        if event.get("event") in {"reviewer_completed", "verification_batch_completed"}:
            continue
        rendered = _format_misc_event(event)
        if rendered:
            misc_lines.append(rendered)

    current_stage = status.get("current_stage") if isinstance(status.get("current_stage"), dict) else None
    current_stage_name = str((current_stage or {}).get("name") or "")
    if (emit_heartbeat or done) and current_stage_name == "reviewers" and not reviewer_progress_lines:
        reviewer_line = _format_snapshot_line("▶ Reviewers", status.get("reviewers", {}), verification=False)
        if reviewer_line:
            lines.append(reviewer_line)
    if (emit_heartbeat or done) and current_stage_name == "verification" and not verification_progress_lines:
        verification_line = _format_snapshot_line("✓ Verification", status.get("verification", {}), verification=True)
        if verification_line:
            lines.append(verification_line)

    lines.extend(misc_lines)
    lines.extend(reviewer_progress_lines)
    lines.extend(verification_progress_lines)
    return _dedupe_preserve_order(lines)


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
        "verification_prepare_file",
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
                display_lines = _render_display_lines(status, events, emit_heartbeat=emit_heartbeat, done=done)
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
                    "⏳ Waiting for CCR status..." if alive else "✖ Detached CCR process exited before writing status.json."
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
        "display_lines": _render_display_lines(status, last_events, emit_heartbeat=emit_heartbeat, done=(status.get("state") in {"completed", "failed"}) if status else False) if status else [],
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
