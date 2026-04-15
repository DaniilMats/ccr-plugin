from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from util import FIXTURES_DIR, load_module


class TestCodeReview(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_module("code_review_module", "quality/scripts/llm-proxy/code_review.py")
        cls.fixture_repo = FIXTURES_DIR / "go_repo"

    def test_resolve_scope_review_files_for_file_and_package(self) -> None:
        project_dir = str(self.fixture_repo)
        file_path = self.fixture_repo / "internal" / "auth" / "jwt.go"
        package_path = self.fixture_repo / "internal" / "auth"

        file_kind, file_paths = self.module._resolve_scope_review_files(f"file:{file_path}", project_dir)
        package_kind, package_files = self.module._resolve_scope_review_files(f"package:{package_path}", project_dir)

        self.assertEqual(file_kind, "file")
        self.assertEqual(file_paths, [str(file_path)])
        self.assertEqual(package_kind, "package")
        self.assertEqual([Path(path).name for path in package_files], ["jwt.go", "jwt_test.go"])

    def test_generate_diff_builds_synthetic_review_artifact_for_file_scope(self) -> None:
        file_path = self.fixture_repo / "internal" / "auth" / "jwt.go"
        with patch.object(self.module.os, "getcwd", return_value=str(self.fixture_repo)):
            diff = self.module._generate_diff(f"file:{file_path}")

        self.assertIn("NOTE: This is a synthetic full-code review artifact.", diff)
        self.assertIn("diff --git a/internal/auth/jwt.go b/internal/auth/jwt.go", diff)
        self.assertIn("+func ValidateToken(raw string) (*TokenClaims, error) {", diff)
        self.assertIn("return &TokenClaims{Subject: trimmed}, nil", diff)

    def test_format_sa_for_prompt_filters_by_persona(self) -> None:
        sa_data = {
            "go_vet": [
                {"tool": "go_vet", "file": "internal/auth/jwt.go", "line": 10, "message": "logic"}
            ],
            "staticcheck": [
                {"tool": "staticcheck", "file": "internal/auth/jwt.go", "line": 12, "message": "all", "code": "SA1000"}
            ],
            "gosec": [
                {"tool": "gosec", "file": "internal/auth/jwt.go", "line": 24, "message": "security", "code": "G101"}
            ],
        }

        security_text = self.module._format_sa_for_prompt(sa_data, "security")
        requirements_text = self.module._format_sa_for_prompt(sa_data, "requirements")
        error_text = self.module._format_sa_for_prompt({"error": "boom"}, "logic")

        self.assertIn("(gosec)", security_text)
        self.assertNotIn("(go_vet)", security_text)
        self.assertEqual(requirements_text, "")
        self.assertEqual(error_text, "(static analysis unavailable: boom)")

    def test_build_semantic_guardrails_for_stateful_visibility_requirements(self) -> None:
        diff = """
--- a/internal/widget/history.go
+++ b/internal/widget/history.go
@@ -22,7 +22,10 @@ func buildHistoryWidget(omitOnEmpty bool, hasTransactions bool, untrusted bool) Widget {
 	if untrusted {
-		return Widget{State: \"untrusted\", Show: true}
+		show := !omitOnEmpty
+		return Widget{State: \"untrusted\", Show: show}
 	}
 }
"""
        requirements = (
            "omitOnEmpty hides the widget only when history is empty.\n"
            "If the user has transactions, keep showing the untrusted placeholder.\n"
        )

        guardrails = self.module._build_semantic_guardrails(diff, requirements, persona="requirements")
        security_guardrails = self.module._build_semantic_guardrails(diff, requirements, persona="security")

        self.assertIn("## Semantic Guardrails", guardrails)
        self.assertIn("omitOnEmpty", guardrails)
        self.assertIn("truth table", guardrails)
        self.assertIn("sibling branches", guardrails)
        self.assertIn("tests added in the same diff", guardrails)
        self.assertEqual(security_guardrails, "")

    def test_build_prompt_includes_semantic_guardrails_for_requirements_and_logic(self) -> None:
        diff = "+show := !omitOnEmpty\n"
        requirements = "omitOnEmpty hides the widget only when history is empty."

        requirements_prompt = self.module._build_prompt(
            diff=diff,
            style_guide_path=str(self.module.DEFAULT_STYLE_GUIDE_PATH),
            persona="requirements",
            static_analysis_text="",
            requirements_text=requirements,
            review_context_text="Nearby tests cover trusted and untrusted branches.",
        )
        logic_prompt = self.module._build_prompt(
            diff=diff,
            style_guide_path=str(self.module.DEFAULT_STYLE_GUIDE_PATH),
            persona="logic",
            static_analysis_text="",
            requirements_text=requirements,
            review_context_text="Nearby tests cover trusted and untrusted branches.",
        )
        security_prompt = self.module._build_prompt(
            diff=diff,
            style_guide_path=str(self.module.DEFAULT_STYLE_GUIDE_PATH),
            persona="security",
            static_analysis_text="",
            requirements_text=requirements,
            review_context_text="Nearby tests cover trusted and untrusted branches.",
        )

        self.assertIn("## Semantic Guardrails", requirements_prompt)
        self.assertIn("## Semantic Guardrails", logic_prompt)
        self.assertIn("omitOnEmpty", requirements_prompt)
        self.assertIn("truth table", logic_prompt)
        self.assertNotIn("## Semantic Guardrails", security_prompt)

    def test_extract_review_output_handles_code_fences_and_invalid_json(self) -> None:
        parsed = self.module._extract_review_output(
            {
                "provider": "codex",
                "response": "```json\n{\"findings\": [], \"summary\": \"ok\"}\n```",
                "exit_code": 0,
                "tokens": 123,
                "schema_valid": True,
                "schema_retries": 0,
            }
        )
        fallback = self.module._extract_review_output(
            {
                "provider": "codex",
                "response": "not-json",
                "exit_code": 0,
                "tokens": 0,
                "schema_valid": False,
                "schema_retries": 2,
                "schema_violations": ["missing required field 'summary'"],
            }
        )

        self.assertEqual(parsed["contract_version"], "ccr.reviewer_result.v1")
        self.assertEqual(parsed["summary"], "ok")
        self.assertEqual(parsed["raw_response"], "```json\n{\"findings\": [], \"summary\": \"ok\"}\n```")
        self.assertEqual(parsed["llm_invocation"]["provider"], "codex")
        self.assertEqual(parsed["llm_invocation"]["tokens"], 123)
        self.assertEqual(fallback["findings"], [])
        self.assertIn("not valid JSON", fallback["summary"])
        self.assertEqual(fallback["llm_invocation"]["schema_retries"], 2)
        self.assertEqual(fallback["llm_invocation"]["schema_violations"], ["missing required field 'summary'"])

    def test_extract_review_output_preserves_provider_failure_telemetry(self) -> None:
        result = self.module._extract_review_output(
            {
                "provider": "gemini",
                "response": "",
                "exit_code": 1,
                "error": "provider crashed",
                "timed_out": False,
                "duration_ms": 250,
                "tokens": 0,
                "schema_valid": True,
                "schema_retries": 0,
            }
        )

        self.assertEqual(result["findings"], [])
        self.assertIn("provider crashed", result["summary"])
        self.assertEqual(result["llm_invocation"]["provider"], "gemini")
        self.assertEqual(result["llm_invocation"]["exit_code"], 1)
        self.assertEqual(result["llm_invocation"]["duration_ms"], 250)

    def test_dry_run_review_output_includes_llm_invocation(self) -> None:
        result = self.module._dry_run_review_output("claude")

        self.assertEqual(result["contract_version"], "ccr.reviewer_result.v1")
        self.assertEqual(result["summary"], "[dry-run] Review skipped.")
        self.assertEqual(result["llm_invocation"]["provider"], "claude")
        self.assertEqual(result["llm_invocation"]["tokens"], 0)
        self.assertEqual(result["llm_invocation"]["schema_retries"], 0)


if __name__ == "__main__":
    unittest.main()
