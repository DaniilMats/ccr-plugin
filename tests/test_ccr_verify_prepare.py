from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from util import FIXTURES_DIR, REPO_ROOT, load_module


class TestCCRVerifyPrepare(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_module("ccr_verify_prepare_module", "quality/scripts/ccr_verify_prepare.py")
        cls.consolidate = load_module("ccr_consolidate_for_verify_prepare", "quality/scripts/ccr_consolidate.py")
        cls.script = REPO_ROOT / "quality" / "scripts" / "ccr_verify_prepare.py"
        cls.fixture_repo = FIXTURES_DIR / "go_repo"

    def _candidate(self, candidate_id: str, *, file: str, line: int, message: str) -> object:
        return self.consolidate.CandidateRecord(
            candidate_id=candidate_id,
            persona="security",
            severity="bug",
            file=file,
            line=line,
            message=message,
            reviewers=["security_p1"],
            consensus="1/3",
            evidence_sources=["reviewer"],
            support_count=1,
            available_pass_count=3,
            symbol="ValidateToken",
            normalized_category="token-validation",
            source_findings=[
                {
                    "pass_name": "security_p1",
                    "provider": "codex",
                    "persona": "security",
                    "file": file,
                    "line": line,
                    "severity": "bug",
                    "message": message,
                }
            ],
            evidence_bundle={
                "diff_hunk": None,
                "file_context": None,
                "requirements_excerpt": None,
                "static_analysis": [],
            },
        )

    def test_prepare_verification_assigns_anchor_statuses_and_drop_reasons(self) -> None:
        diff_text = "\n".join(
            [
                "diff --git a/internal/auth/jwt.go b/internal/auth/jwt.go",
                "index 1111111..2222222 100644",
                "--- a/internal/auth/jwt.go",
                "+++ b/internal/auth/jwt.go",
                "@@ -1,3 +1,4 @@",
                " // Package auth validates JWTs for incoming requests.",
                " package auth",
                "+// ValidateToken now records request scope.",
                "",
            ]
        )
        candidates = [
            self._candidate(
                "F1",
                file="internal/auth/jwt.go",
                line=3,
                message="ValidateToken now records request scope without validating expiry.",
            ),
            self._candidate(
                "F2",
                file="internal/auth/jwt.go",
                line=24,
                message="ValidateToken still returns claims without checking expiry.",
            ),
            self._candidate(
                "F3",
                file="internal/missing/file.go",
                line=10,
                message="Missing file candidate should be dropped.",
            ),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            result = self.module.prepare_verification_artifacts(
                candidates,
                artifact_text=diff_text,
                project_dir=self.fixture_repo,
                requirements_text="ValidateToken must reject expired tokens before returning claims.",
                verify_batch_dir=Path(tmp) / "verify_batches",
                output_file=Path(tmp) / "verification_prepare.json",
            )

            payload = result["payload"]
            self.assertEqual(payload["contract_version"], "ccr.verification_prepare.v1")
            self.assertEqual(payload["summary"]["candidate_count"], 3)
            self.assertEqual(payload["summary"]["ready_count"], 2)
            self.assertEqual(payload["summary"]["dropped_count"], 1)
            self.assertEqual(payload["summary"]["batch_count"], 1)

            ready_by_id = {item["candidate_id"]: item for item in payload["ready_candidates"]}
            dropped_by_id = {item["candidate_id"]: item for item in payload["dropped_candidates"]}

            self.assertEqual(ready_by_id["F1"]["anchor_status"], "diff")
            self.assertIn("@@ -1,3 +1,4 @@", ready_by_id["F1"]["evidence_bundle"]["diff_hunk"])
            self.assertIn("requirements", ready_by_id["F1"]["evidence_sources"])
            self.assertTrue(ready_by_id["F1"]["ready_for_verification"])

            self.assertEqual(ready_by_id["F2"]["anchor_status"], "file_context")
            self.assertIsNone(ready_by_id["F2"]["evidence_bundle"]["diff_hunk"])
            self.assertIn("return &TokenClaims", ready_by_id["F2"]["evidence_bundle"]["file_context"])

            self.assertEqual(dropped_by_id["F3"]["anchor_status"], "missing")
            self.assertFalse(dropped_by_id["F3"]["ready_for_verification"])
            self.assertIn("missing_file", dropped_by_id["F3"]["drop_reasons"])
            self.assertIn("missing_anchor", dropped_by_id["F3"]["drop_reasons"])
            self.assertIn("missing_evidence", dropped_by_id["F3"]["drop_reasons"])

            batch = result["batches"][0]
            self.assertEqual(batch["payload"]["file"], "internal/auth/jwt.go")
            self.assertEqual([item["candidate_id"] for item in batch["payload"]["candidates"]], ["F1", "F2"])
            self.assertTrue(Path(batch["batch_file"]).is_file())

    def test_prepare_verification_chunks_batches_deterministically(self) -> None:
        diff_lines = [
            "diff --git a/internal/auth/jwt.go b/internal/auth/jwt.go",
            "index 1111111..2222222 100644",
            "--- a/internal/auth/jwt.go",
            "+++ b/internal/auth/jwt.go",
            "@@ -0,0 +1,8 @@",
        ]
        for index in range(1, 9):
            diff_lines.append(f"+line {index}")
        diff_text = "\n".join(diff_lines) + "\n"
        candidates = [
            self._candidate(
                f"F{index}",
                file="internal/auth/jwt.go",
                line=index,
                message=f"Finding {index}: ValidateToken issue.",
            )
            for index in range(1, 7)
        ]

        with tempfile.TemporaryDirectory() as tmp:
            result = self.module.prepare_verification_artifacts(
                candidates,
                artifact_text=diff_text,
                project_dir=None,
                requirements_text="",
                verify_batch_dir=Path(tmp) / "verify_batches",
                output_file=Path(tmp) / "verification_prepare.json",
            )

            self.assertEqual(result["payload"]["summary"]["ready_count"], 6)
            self.assertEqual(result["payload"]["summary"]["batch_count"], 2)
            self.assertEqual([batch["batch_id"] for batch in result["batches"]], ["B1", "B2"])
            self.assertEqual(result["payload"]["batches"][0]["candidate_count"], 5)
            self.assertEqual(result["payload"]["batches"][1]["candidate_count"], 1)
            self.assertEqual(result["payload"]["batches"][0]["candidate_ids"], ["F1", "F2", "F3", "F4", "F5"])
            self.assertEqual(result["payload"]["batches"][1]["candidate_ids"], ["F6"])

    def test_cli_writes_verification_prepare_and_batches(self) -> None:
        candidate = self._candidate(
            "F1",
            file="internal/auth/jwt.go",
            line=24,
            message="ValidateToken still returns claims without checking expiry.",
        )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            candidates_file = tmp_path / "candidates.json"
            artifact_file = tmp_path / "review_artifact.txt"
            requirements_file = tmp_path / "requirements.txt"
            verify_batch_dir = tmp_path / "verify_batches"
            output_file = tmp_path / "verification_prepare.json"

            candidates_file.write_text(
                json.dumps(
                    {
                        "contract_version": "ccr.candidates_manifest.v1",
                        "candidates": [candidate.to_contract_dict()],
                        "summary": {"candidate_count": 1, "source_finding_count": 1},
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            artifact_file.write_text(
                "\n".join(
                    [
                        "diff --git a/internal/auth/jwt.go b/internal/auth/jwt.go",
                        "index 1111111..2222222 100644",
                        "--- a/internal/auth/jwt.go",
                        "+++ b/internal/auth/jwt.go",
                        "@@ -1,1 +1,2 @@",
                        " package auth",
                        "+// instrumentation",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            requirements_file.write_text("ValidateToken must reject expired tokens.\n", encoding="utf-8")

            result = subprocess.run(
                [
                    "python3",
                    str(self.script),
                    "--candidates-file",
                    str(candidates_file),
                    "--artifact-file",
                    str(artifact_file),
                    "--verify-batch-dir",
                    str(verify_batch_dir),
                    "--output-file",
                    str(output_file),
                    "--project-dir",
                    str(self.fixture_repo),
                    "--requirements-file",
                    str(requirements_file),
                ],
                capture_output=True,
                text=True,
                check=True,
            )

            payload = json.loads(result.stdout)
            written = json.loads(output_file.read_text(encoding="utf-8"))
            self.assertEqual(payload["contract_version"], "ccr.verification_prepare.v1")
            self.assertEqual(written["summary"]["ready_count"], 1)
            self.assertEqual(written["summary"]["batch_count"], 1)
            self.assertTrue((verify_batch_dir / "verify_batch_001.json").is_file())


if __name__ == "__main__":
    unittest.main()
