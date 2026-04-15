from __future__ import annotations

import unittest

from util import load_module


class TestCodeReviewVerify(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_module("code_review_verify_module", "quality/scripts/llm-proxy/code_review_verify.py")

    def test_sanitize_input_payload_drops_eval_only_metadata(self) -> None:
        payload = {
            "file": "internal/auth/jwt.go",
            "diff_hunk": "@@ -1 +1 @@",
            "file_context": "context",
            "requirements": "requirements",
            "candidates": [{"candidate_id": "F1"}],
            "eval_only": {"should": "drop"},
        }

        sanitized = self.module._sanitize_input_payload(payload)
        self.assertEqual(
            sanitized,
            {
                "file": "internal/auth/jwt.go",
                "diff_hunk": "@@ -1 +1 @@",
                "file_context": "context",
                "requirements": "requirements",
                "candidates": [{"candidate_id": "F1"}],
            },
        )

    def test_parse_llm_response_handles_json_fences_embedded_json_and_fallback(self) -> None:
        direct = self.module._parse_llm_response('{"verified_findings": [], "summary": "direct"}')
        fenced = self.module._parse_llm_response("```json\n{\"verified_findings\": [], \"summary\": \"fenced\"}\n```")
        embedded = self.module._parse_llm_response("prefix\n{\"verified_findings\": [], \"summary\": \"embedded\"}\nSuffix")
        fallback = self.module._parse_llm_response("not-json")

        self.assertEqual(direct["summary"], "direct")
        self.assertEqual(fenced["summary"], "fenced")
        self.assertEqual(embedded["summary"], "embedded")
        self.assertEqual(fallback["verified_findings"], [])
        self.assertIn("could not be parsed as JSON", fallback["summary"])

    def test_dry_run_result_marks_all_candidates_uncertain(self) -> None:
        payload = {
            "file": "internal/auth/jwt.go",
            "candidates": [
                {"candidate_id": "F1", "file": "internal/auth/jwt.go", "line": 24, "message": "First"},
                {"candidate_id": "F2", "file": "internal/auth/jwt.go", "line": 28, "message": "Second"},
            ],
        }

        result = self.module._dry_run_result(payload, "gemini")
        self.assertEqual(result["contract_version"], "ccr.verification_result.v1")
        self.assertEqual([item["candidate_id"] for item in result["verified_findings"]], ["F1", "F2"])
        self.assertTrue(all(item["verdict"] == "uncertain" for item in result["verified_findings"]))
        self.assertIn("Provider would be 'gemini'", result["verified_findings"][0]["evidence"])


if __name__ == "__main__":
    unittest.main()
