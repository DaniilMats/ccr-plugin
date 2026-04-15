from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from util import REPO_ROOT, load_module


class TestCCRConsolidate(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_module("ccr_consolidate_module", "quality/scripts/ccr_consolidate.py")
        cls.script = REPO_ROOT / "quality" / "scripts" / "ccr_consolidate.py"

    def _review_result(self, pass_name: str, persona: str, findings: list[dict], provider: str = "codex") -> dict:
        return {
            "pass_name": pass_name,
            "persona": persona,
            "provider": provider,
            "status": "succeeded",
            "result": {
                "contract_version": "ccr.reviewer_result.v1",
                "findings": findings,
                "summary": f"{pass_name} summary",
            },
        }

    def test_build_candidates_merges_cross_persona_root_cause_and_keeps_distinct_category(self) -> None:
        reviewer_results = [
            self._review_result(
                "security_p1",
                "security",
                [
                    {
                        "severity": "bug",
                        "file": "internal/auth/jwt.go",
                        "line": 12,
                        "message": "`ValidateToken` skips JWT token expiry validation.",
                    }
                ],
            ),
            self._review_result(
                "logic_p1",
                "logic",
                [
                    {
                        "severity": "warning",
                        "file": "internal/auth/jwt.go",
                        "line": 13,
                        "message": "JWT token expiry validation is missing in ValidateToken.",
                    }
                ],
                provider="gemini",
            ),
            self._review_result(
                "performance_p1",
                "performance",
                [
                    {
                        "severity": "warning",
                        "file": "internal/auth/jwt.go",
                        "line": 12,
                        "message": "ValidateToken rebuilds JWT claims on every call and allocates unnecessarily.",
                    }
                ],
                provider="claude",
            ),
        ]
        route_plan = {
            "pass_counts": {
                "security": 3,
                "logic": 3,
                "performance": 2,
            }
        }
        static_analysis = {
            "go_vet": [],
            "staticcheck": [],
            "gosec": [
                {
                    "tool": "gosec",
                    "file": "internal/auth/jwt.go",
                    "line": 12,
                    "message": "Token validation path does not enforce expiry checks.",
                    "code": "G999",
                }
            ],
        }

        payload = self.module.build_candidates_manifest(
            reviewer_results,
            route_plan=route_plan,
            static_analysis_payload=static_analysis,
        )

        self.assertEqual(payload["contract_version"], "ccr.candidates_manifest.v1")
        self.assertEqual(payload["summary"]["candidate_count"], 2)
        self.assertEqual(payload["summary"]["source_finding_count"], 3)

        security_candidate = next(
            candidate
            for candidate in payload["candidates"]
            if candidate["persona"] == "security"
        )
        performance_candidate = next(
            candidate
            for candidate in payload["candidates"]
            if candidate["persona"] == "performance"
        )

        self.assertEqual(security_candidate["supporting_personas"], ["logic"])
        self.assertEqual(security_candidate["reviewers"], ["logic_p1", "security_p1"])
        self.assertEqual(security_candidate["support_count"], 2)
        self.assertEqual(security_candidate["available_pass_count"], 3)
        self.assertEqual(security_candidate["consensus"], "2/3")
        self.assertEqual(security_candidate["symbol"], "ValidateToken")
        self.assertIn("reviewer", security_candidate["evidence_sources"])
        self.assertIn("diff_hunk", security_candidate["evidence_sources"])
        self.assertIn("gosec", security_candidate["evidence_sources"])
        self.assertEqual(len(security_candidate["source_findings"]), 2)
        self.assertEqual(len(security_candidate["evidence_bundle"]["static_analysis"]), 1)
        self.assertTrue(security_candidate["normalized_category"])

        self.assertEqual(performance_candidate["reviewers"], ["performance_p1"])
        self.assertEqual(performance_candidate["support_count"], 1)
        self.assertEqual(performance_candidate["available_pass_count"], 2)
        self.assertNotEqual(performance_candidate["candidate_id"], security_candidate["candidate_id"])

    def test_build_candidates_skips_invalid_findings_and_keeps_stable_ids(self) -> None:
        reviewer_results = [
            self._review_result(
                "logic_p1",
                "logic",
                [
                    {
                        "severity": "warning",
                        "file": "internal/http/client.go",
                        "line": 40,
                        "message": "Outbound HTTP request is missing a context timeout.",
                    },
                    {
                        "severity": "warning",
                        "file": "",
                        "line": 0,
                        "message": "Invalid finding should be dropped.",
                    },
                ],
            ),
            self._review_result(
                "logic_p2",
                "logic",
                [
                    {
                        "severity": "warning",
                        "file": "internal/http/client.go",
                        "line": 42,
                        "message": "Outbound HTTP request lacks a context timeout.",
                    }
                ],
                provider="gemini",
            ),
        ]
        route_plan = {"pass_counts": {"logic": 3}}
        static_analysis = {"go_vet": [], "staticcheck": [], "gosec": []}

        first = self.module.build_candidates_manifest(
            reviewer_results,
            route_plan=route_plan,
            static_analysis_payload=static_analysis,
        )
        second = self.module.build_candidates_manifest(
            reviewer_results,
            route_plan=route_plan,
            static_analysis_payload=static_analysis,
        )

        self.assertEqual(first["summary"]["candidate_count"], 1)
        self.assertEqual(first["summary"]["source_finding_count"], 2)
        self.assertEqual(first["summary"]["skipped_invalid_finding_count"], 1)
        self.assertEqual(first["candidates"][0]["candidate_id"], "F1")
        self.assertEqual(second["candidates"][0]["candidate_id"], "F1")
        self.assertEqual(first["candidates"][0]["normalized_category"], second["candidates"][0]["normalized_category"])
        self.assertEqual(first["candidates"][0]["message"], second["candidates"][0]["message"])

    def test_cli_writes_candidates_manifest(self) -> None:
        reviewer_results = [
            self._review_result(
                "security_p1",
                "security",
                [
                    {
                        "severity": "bug",
                        "file": "internal/auth/jwt.go",
                        "line": 10,
                        "message": "`ValidateToken` skips JWT token expiry validation.",
                    }
                ],
            )
        ]
        route_plan = {"pass_counts": {"security": 2}}
        static_analysis = {"go_vet": [], "staticcheck": [], "gosec": []}

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reviewer_results_file = tmp_path / "reviewer_results.json"
            route_plan_file = tmp_path / "route_plan.json"
            static_analysis_file = tmp_path / "static_analysis.json"
            output_file = tmp_path / "candidates.json"
            reviewer_results_file.write_text(json.dumps(reviewer_results) + "\n", encoding="utf-8")
            route_plan_file.write_text(json.dumps(route_plan) + "\n", encoding="utf-8")
            static_analysis_file.write_text(json.dumps(static_analysis) + "\n", encoding="utf-8")

            result = subprocess.run(
                [
                    "python3",
                    str(self.script),
                    "--reviewer-results-file",
                    str(reviewer_results_file),
                    "--route-plan-file",
                    str(route_plan_file),
                    "--static-analysis-file",
                    str(static_analysis_file),
                    "--output-file",
                    str(output_file),
                ],
                capture_output=True,
                text=True,
                check=True,
            )

            payload = json.loads(result.stdout)
            written = json.loads(output_file.read_text(encoding="utf-8"))
            self.assertEqual(payload["contract_version"], "ccr.candidates_manifest.v1")
            self.assertEqual(written["summary"]["candidate_count"], 1)
            self.assertEqual(written["candidates"][0]["candidate_id"], "F1")


if __name__ == "__main__":
    unittest.main()
