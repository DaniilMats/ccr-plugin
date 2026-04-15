from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from util import REPO_ROOT


class TestCCRWatch(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.script = REPO_ROOT / "quality" / "scripts" / "ccr_watch.py"

    def test_text_mode_uses_cursor_and_stays_quiet_when_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            status_file = tmp_path / "status.json"
            trace_file = tmp_path / "trace.jsonl"
            cursor_file = tmp_path / "watch_cursor.json"

            trace_events = [
                {
                    "seq": 1,
                    "ts": "2026-04-14T22:55:00Z",
                    "level": "info",
                    "event": "reviewer_completed",
                    "stage": "reviewers",
                    "message": "Reviewer 4/14 finished: security_p1",
                    "data": {
                        "completed": 4,
                        "planned": 14,
                        "pass_name": "security_p1",
                        "provider": "gemini",
                        "finding_count": 1,
                        "status": "succeeded",
                        "duration_ms": 28000,
                    },
                }
            ]
            trace_file.write_text("\n".join(json.dumps(event) for event in trace_events) + "\n", encoding="utf-8")

            status_payload = {
                "contract_version": "ccr.run_status.v1",
                "run_id": "20260414T225500Z-1234-abcd1234",
                "pid": 12345,
                "detached": True,
                "revision": 2,
                "event_seq": 1,
                "state": "running",
                "started_at": "2026-04-14T22:55:00Z",
                "updated_at": "2026-04-14T22:55:10Z",
                "heartbeat_at": "2026-04-14T22:55:10Z",
                "finished_at": None,
                "duration_ms": 10000,
                "current_stage": {
                    "name": "reviewers",
                    "status": "running",
                    "message": "Running reviewer passes",
                    "started_at": "2026-04-14T22:55:00Z",
                    "ended_at": None,
                    "duration_ms": None,
                    "index": 7,
                    "total": 10,
                },
                "stages": {},
                "target": {},
                "route_plan": {
                    "summary": "Review plan: medium-risk MR → Logic x3, Security x1",
                    "total_passes": 4,
                    "full_matrix": False,
                    "pass_counts": {
                        "logic": 3,
                        "security": 1,
                        "concurrency": 0,
                        "performance": 0,
                        "requirements": 0,
                    },
                },
                "reviewers": {
                    "planned": 14,
                    "workers": 14,
                    "timeout_sec": 600,
                    "running": 10,
                    "completed": 4,
                    "succeeded": 4,
                    "failed": 0,
                    "estimated_max_duration_sec": 600,
                    "passes": {},
                },
                "verification": {
                    "planned_batches": 0,
                    "workers": 0,
                    "timeout_sec": 300,
                    "running_batches": 0,
                    "completed_batches": 0,
                    "succeeded_batches": 0,
                    "failed_batches": 0,
                    "estimated_max_duration_sec": 0,
                    "batches": {},
                },
                "artifacts": {
                    "status_file": str(status_file),
                    "trace_file": str(trace_file),
                    "summary_file": str(tmp_path / "run_summary.json"),
                    "report_file": str(tmp_path / "report.md"),
                },
                "summary": {},
                "last_event": trace_events[0],
                "error": None,
            }
            status_file.write_text(json.dumps(status_payload, indent=2) + "\n", encoding="utf-8")

            first = subprocess.run(
                [
                    "python3",
                    str(self.script),
                    "--status-file",
                    str(status_file),
                    "--trace-file",
                    str(trace_file),
                    "--cursor-file",
                    str(cursor_file),
                    "--format",
                    "text",
                    "--emit-heartbeat",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertIn("⏳ CCR abcd1234 · running · Reviewers [7/10]", first.stdout)
            self.assertIn("▶ Reviewers +1 ⇒ 4/14 complete · 10 running · Logic x3, Security x1", first.stdout)
            self.assertIn("⚠ Reviewer signals: 1 finding(s) · security_p1", first.stdout)

            cursor_payload = json.loads(cursor_file.read_text(encoding="utf-8"))
            self.assertEqual(cursor_payload["last_seq"], 1)

            second = subprocess.run(
                [
                    "python3",
                    str(self.script),
                    "--status-file",
                    str(status_file),
                    "--trace-file",
                    str(trace_file),
                    "--cursor-file",
                    str(cursor_file),
                    "--format",
                    "text",
                    "--quiet-unchanged",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(second.stdout.strip(), "")

    def test_text_mode_coalesces_multiple_reviewer_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            status_file = tmp_path / "status.json"
            trace_file = tmp_path / "trace.jsonl"

            trace_events = [
                {
                    "seq": 1,
                    "ts": "2026-04-14T23:30:00Z",
                    "level": "info",
                    "event": "reviewer_completed",
                    "stage": "reviewers",
                    "message": "Reviewer 4/14 finished: requirements_p1",
                    "data": {
                        "completed": 4,
                        "planned": 14,
                        "running": 10,
                        "pass_name": "requirements_p1",
                        "provider": "gemini",
                        "finding_count": 1,
                        "status": "succeeded",
                        "duration_ms": 21000,
                    },
                },
                {
                    "seq": 2,
                    "ts": "2026-04-14T23:30:01Z",
                    "level": "info",
                    "event": "reviewer_completed",
                    "stage": "reviewers",
                    "message": "Reviewer 5/14 finished: security_p2",
                    "data": {
                        "completed": 5,
                        "planned": 14,
                        "running": 9,
                        "pass_name": "security_p2",
                        "provider": "codex",
                        "finding_count": 2,
                        "status": "succeeded",
                        "duration_ms": 22000,
                    },
                },
            ]
            trace_file.write_text("\n".join(json.dumps(event) for event in trace_events) + "\n", encoding="utf-8")

            status_payload = {
                "contract_version": "ccr.run_status.v1",
                "run_id": "20260414T233000Z-5555-ef901234",
                "pid": 12345,
                "detached": True,
                "revision": 3,
                "event_seq": 2,
                "state": "running",
                "started_at": "2026-04-14T23:30:00Z",
                "updated_at": "2026-04-14T23:30:02Z",
                "heartbeat_at": "2026-04-14T23:30:02Z",
                "finished_at": None,
                "duration_ms": 12000,
                "current_stage": {
                    "name": "reviewers",
                    "status": "running",
                    "message": "Running reviewer passes",
                    "started_at": "2026-04-14T23:30:00Z",
                    "ended_at": None,
                    "duration_ms": None,
                    "index": 7,
                    "total": 10,
                },
                "stages": {},
                "target": {},
                "route_plan": {
                    "summary": "Review plan: high-risk MR → Logic x3, Security x2",
                    "total_passes": 5,
                    "full_matrix": False,
                    "pass_counts": {
                        "logic": 3,
                        "security": 2,
                        "concurrency": 0,
                        "performance": 0,
                        "requirements": 0,
                    },
                },
                "reviewers": {
                    "planned": 14,
                    "workers": 14,
                    "timeout_sec": 600,
                    "running": 9,
                    "completed": 5,
                    "succeeded": 5,
                    "failed": 0,
                    "estimated_max_duration_sec": 600,
                    "passes": {},
                },
                "verification": {
                    "planned_batches": 0,
                    "workers": 0,
                    "timeout_sec": 300,
                    "running_batches": 0,
                    "completed_batches": 0,
                    "succeeded_batches": 0,
                    "failed_batches": 0,
                    "estimated_max_duration_sec": 0,
                    "batches": {},
                },
                "artifacts": {
                    "status_file": str(status_file),
                    "trace_file": str(trace_file),
                    "summary_file": str(tmp_path / "run_summary.json"),
                    "report_file": str(tmp_path / "report.md"),
                },
                "summary": {},
                "last_event": trace_events[-1],
                "error": None,
            }
            status_file.write_text(json.dumps(status_payload, indent=2) + "\n", encoding="utf-8")

            result = subprocess.run(
                [
                    "python3",
                    str(self.script),
                    "--status-file",
                    str(status_file),
                    "--trace-file",
                    str(trace_file),
                    "--format",
                    "text",
                    "--emit-heartbeat",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertIn("⏳ CCR ef901234 · running · Reviewers [7/10]", result.stdout)
            self.assertIn("▶ Reviewers +2 ⇒ 5/14 complete · 9 running · Logic x3, Security x2", result.stdout)
            self.assertIn("⚠ Reviewer signals: 3 finding(s) · requirements_p1, security_p2", result.stdout)
            self.assertNotIn("Reviewer 4/14 finished", result.stdout)
            self.assertNotIn("Reviewer 5/14 finished", result.stdout)


if __name__ == "__main__":
    unittest.main()
