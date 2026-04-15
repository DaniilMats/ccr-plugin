from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from util import load_module


class TestCCRPostComments(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_module("ccr_post_comments_module", "quality/scripts/ccr_post_comments.py")
        cls.run_init = load_module("ccr_run_init_posting_module", "quality/scripts/ccr_run_init.py")

    def _write_json(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def _setup_mr_run(
        self,
        tmp_path: Path,
        *,
        verified_findings: list[dict],
        approval_numbers: list[int],
        diff_text: str,
        approved_all: bool = False,
        summary_mode: str = "mr",
        target: str = "https://gitlab.com/group/project/-/merge_requests/200",
    ) -> tuple[dict, Path]:
        manifest = self.run_init._build_manifest(tmp_path, "posting-run")
        manifest_file = Path(manifest["manifest_file"])
        self._write_json(manifest_file, manifest)

        summary_payload = {
            "contract_version": "ccr.run_summary.v1",
            "run_id": manifest["run_id"],
            "mode": summary_mode,
            "target": target,
        }
        self._write_json(Path(manifest["summary_file"]), summary_payload)

        mr_metadata = {
            "iid": 200,
            "diff_refs": {
                "base_sha": "base-sha",
                "start_sha": "start-sha",
                "head_sha": "head-sha",
            },
        }
        self._write_json(Path(manifest["mr_metadata_file"]), mr_metadata)
        Path(manifest["diff_file"]).write_text(diff_text, encoding="utf-8")
        self._write_json(
            Path(manifest["verified_findings_file"]),
            {
                "contract_version": "ccr.verified_findings.v1",
                "verified_findings": verified_findings,
                "summary": {
                    "verified_count": len(verified_findings),
                },
            },
        )
        self._write_json(
            Path(manifest["posting_approval_file"]),
            {
                "contract_version": "ccr.posting_approval.v1",
                "run_id": manifest["run_id"],
                "project": "group/project",
                "mr_iid": 200,
                "approved_finding_numbers": approval_numbers,
                "approved_all": approved_all,
                "approved_at": "2026-04-15T00:00:00Z",
                "source": "user_selection",
            },
        )
        return manifest, manifest_file

    def _write_fake_glab(
        self,
        tmp_path: Path,
        *,
        get_payload: object,
        post_payload: object | None = None,
        fail_on_post: bool = False,
    ) -> tuple[Path, Path]:
        log_path = tmp_path / "fake_glab.log"
        script_path = tmp_path / "fake_glab"
        get_json = json.dumps(get_payload)
        post_json = json.dumps(post_payload) if post_payload is not None else "{}"
        script_path.write_text(
            "#!/usr/bin/env python3\n"
            "import json\n"
            "import sys\n"
            "from pathlib import Path\n"
            f"log_path = Path({str(log_path)!r})\n"
            "with log_path.open('a', encoding='utf-8') as handle:\n"
            "    handle.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "api_path = sys.argv[2] if len(sys.argv) > 2 else ''\n"
            "is_post = '-X' in sys.argv and sys.argv[sys.argv.index('-X') + 1] == 'POST'\n"
            "if is_post:\n"
            + (
                "    sys.stderr.write('unexpected POST')\n"
                "    sys.exit(91)\n"
                if fail_on_post
                else f"    sys.stdout.write({post_json!r})\n"
            )
            + "else:\n"
            f"    sys.stdout.write({get_json!r})\n",
            encoding="utf-8",
        )
        script_path.chmod(0o755)
        return script_path, log_path

    def test_prepare_only_writes_manifest_and_payloads(self) -> None:
        diff_text = "\n".join(
            [
                "diff --git a/internal/auth/jwt.go b/internal/auth/jwt.go",
                "index 1111111..2222222 100644",
                "--- a/internal/auth/jwt.go",
                "+++ b/internal/auth/jwt.go",
                "@@ -1,1 +1,2 @@",
                " package auth",
                "+func Validate() {}",
                "",
            ]
        )
        verified_findings = [
            {
                "finding_number": 1,
                "candidate_id": "F1",
                "file": "internal/auth/jwt.go",
                "line": 2,
                "message": "Validate the token before returning.",
            },
            {
                "finding_number": 2,
                "candidate_id": "F2",
                "file": "internal/auth/jwt.go",
                "line": 99,
                "message": "This line is not in the diff.",
            },
        ]

        with tempfile.TemporaryDirectory() as tmp:
            manifest, manifest_file = self._setup_mr_run(
                Path(tmp),
                verified_findings=verified_findings,
                approval_numbers=[1, 2],
                diff_text=diff_text,
            )
            prepared = self.module.prepare_posting_manifest(manifest_file)
            self.assertEqual(prepared["contract_version"], "ccr.posting_manifest.v1")
            self.assertEqual(prepared["summary"]["ready_count"], 1)
            self.assertEqual(prepared["summary"]["missing_anchor_count"], 1)
            self.assertEqual(prepared["summary"]["invalid_count"], 0)
            self.assertEqual(prepared["approved_findings"][0]["status"], "ready")
            self.assertEqual(prepared["approved_findings"][0]["anchor"]["new_line"], 2)
            self.assertEqual(prepared["approved_findings"][1]["status"], "missing_anchor")

            request_file = Path(prepared["approved_findings"][0]["payload_file"])
            self.assertTrue(request_file.is_file())
            request_payload = json.loads(request_file.read_text(encoding="utf-8"))
            self.assertIn("<!-- ccr:fingerprint=", request_payload["body"])
            self.assertEqual(request_payload["position"]["new_line"], 2)
            self.assertTrue(Path(manifest["posting_manifest_file"]).is_file())

    def test_apply_skips_already_posted_fingerprint(self) -> None:
        diff_text = "\n".join(
            [
                "diff --git a/internal/auth/jwt.go b/internal/auth/jwt.go",
                "index 1111111..2222222 100644",
                "--- a/internal/auth/jwt.go",
                "+++ b/internal/auth/jwt.go",
                "@@ -1,1 +1,2 @@",
                " package auth",
                "+func Validate() {}",
                "",
            ]
        )
        verified_findings = [
            {
                "finding_number": 1,
                "candidate_id": "F1",
                "file": "internal/auth/jwt.go",
                "line": 2,
                "message": "Validate the token before returning.",
            }
        ]

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _manifest, manifest_file = self._setup_mr_run(
                tmp_path,
                verified_findings=verified_findings,
                approval_numbers=[1],
                diff_text=diff_text,
            )
            prepared = self.module.prepare_posting_manifest(manifest_file)
            fingerprint = prepared["approved_findings"][0]["fingerprint"]
            fake_glab, log_path = self._write_fake_glab(
                tmp_path,
                get_payload=[
                    {
                        "id": "discussion-1",
                        "notes": [
                            {
                                "id": 42,
                                "type": "DiffNote",
                                "body": f"Already posted.\n\n<!-- ccr:fingerprint={fingerprint} run_id=posting-run finding=1 candidate_id=F1 -->",
                            }
                        ],
                    }
                ],
                fail_on_post=True,
            )
            result = self.module.apply_posting_plan(manifest_file, glab_bin=str(fake_glab))
            self.assertEqual(result["posted_count"], 0)
            self.assertEqual(result["already_posted_count"], 1)
            self.assertEqual(result["failed_count"], 0)
            self.assertEqual(result["results"][0]["status"], "already_posted")
            calls = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertTrue(all("POST" not in call for call in calls))

    def test_apply_posts_ready_finding(self) -> None:
        diff_text = "\n".join(
            [
                "diff --git a/internal/auth/jwt.go b/internal/auth/jwt.go",
                "index 1111111..2222222 100644",
                "--- a/internal/auth/jwt.go",
                "+++ b/internal/auth/jwt.go",
                "@@ -1,1 +1,2 @@",
                " package auth",
                "+func Validate() {}",
                "",
            ]
        )
        verified_findings = [
            {
                "finding_number": 1,
                "candidate_id": "F1",
                "file": "internal/auth/jwt.go",
                "line": 2,
                "message": "Validate the token before returning.",
            }
        ]

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _manifest, manifest_file = self._setup_mr_run(
                tmp_path,
                verified_findings=verified_findings,
                approval_numbers=[1],
                diff_text=diff_text,
            )
            fake_glab, log_path = self._write_fake_glab(
                tmp_path,
                get_payload=[],
                post_payload={
                    "id": "discussion-2",
                    "notes": [
                        {
                            "id": 99,
                            "type": "DiffNote",
                            "body": "Posted.",
                        }
                    ],
                },
            )
            result = self.module.apply_posting_plan(manifest_file, glab_bin=str(fake_glab))
            self.assertEqual(result["posted_count"], 1)
            self.assertEqual(result["results"][0]["status"], "posted")
            self.assertEqual(result["results"][0]["discussion_id"], "discussion-2")
            self.assertTrue(Path(result["results"][0]["response_file"]).is_file())
            calls = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertTrue(any("POST" in call for call in calls))

    def test_apply_rejects_invalid_response_type(self) -> None:
        diff_text = "\n".join(
            [
                "diff --git a/internal/auth/jwt.go b/internal/auth/jwt.go",
                "index 1111111..2222222 100644",
                "--- a/internal/auth/jwt.go",
                "+++ b/internal/auth/jwt.go",
                "@@ -1,1 +1,2 @@",
                " package auth",
                "+func Validate() {}",
                "",
            ]
        )
        verified_findings = [
            {
                "finding_number": 1,
                "candidate_id": "F1",
                "file": "internal/auth/jwt.go",
                "line": 2,
                "message": "Validate the token before returning.",
            }
        ]

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _manifest, manifest_file = self._setup_mr_run(
                tmp_path,
                verified_findings=verified_findings,
                approval_numbers=[1],
                diff_text=diff_text,
            )
            fake_glab, _log_path = self._write_fake_glab(
                tmp_path,
                get_payload=[],
                post_payload={
                    "id": "discussion-3",
                    "notes": [
                        {
                            "id": 77,
                            "type": "DiscussionNote",
                            "body": "Wrong note type.",
                        }
                    ],
                },
            )
            result = self.module.apply_posting_plan(manifest_file, glab_bin=str(fake_glab))
            self.assertEqual(result["posted_count"], 0)
            self.assertEqual(result["failed_count"], 1)
            self.assertEqual(result["results"][0]["status"], "invalid_response")

    def test_prepare_rejects_non_mr_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _manifest, manifest_file = self._setup_mr_run(
                Path(tmp),
                verified_findings=[],
                approval_numbers=[],
                diff_text="",
                summary_mode="local",
                target="package:internal/auth",
            )
            with self.assertRaisesRegex(ValueError, "only supported for MR runs"):
                self.module.prepare_posting_manifest(manifest_file)


if __name__ == "__main__":
    unittest.main()
