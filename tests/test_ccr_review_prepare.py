from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from util import REPO_ROOT, load_module, read_fixture


class TestCCRReviewPrepare(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_module("ccr_review_prepare_module", "quality/scripts/ccr_review_prepare.py")
        cls.script = REPO_ROOT / "quality" / "scripts" / "ccr_review_prepare.py"

    def test_build_review_prepare_payload_builds_case_matrix_without_verdicts(self) -> None:
        diff = """
diff --git a/internal/layout/widgetmanager/builder/widgets/shortwidget/widget.go b/internal/layout/widgetmanager/builder/widgets/shortwidget/widget.go
--- a/internal/layout/widgetmanager/builder/widgets/shortwidget/widget.go
+++ b/internal/layout/widgetmanager/builder/widgets/shortwidget/widget.go
@@ -82,7 +82,10 @@ func (wb *widgetBuilder) build(ctx context.Context, params buildersdk.FunctionWidgetBuilderParams) (widgetmanagersdk.Widget, error) {
 	if wb.shouldReturnUntrustedState(rp, customerExtraParams) {
-		return widgetmanagersdk.Widget{Show: true}, nil
+		show := !omitOnEmpty
+		return widgetmanagersdk.Widget{Show: show}, nil
 	}
 }
"""
        requirements = (
            "omitOnEmpty hides the widget only when history is empty.\n"
            "If the user has transactions, keep showing the untrusted placeholder.\n"
        )
        review_context = (
            "- internal/layout/widgetmanager/builder/widgets/shortwidget/widget.go\n"
            "- TestShortWidgetBuild_UntrustedDevice_OmitOnEmpty_WithTransactions_ShowsUntrusted\n"
        )

        payload = self.module.build_review_prepare_payload(
            diff,
            requirements_text=requirements,
            review_context_text=review_context,
            route_input={"triggered_personas": ["security", "requirements"]},
            route_plan={"summary": "Review plan: high-risk MR → Logic x3, Security x3, Requirements x2"},
        )

        self.assertEqual(payload["contract_version"], "ccr.review_prepare.v1")
        self.assertEqual(payload["summary"]["changed_file_count"], 1)
        self.assertGreaterEqual(payload["summary"]["requirement_clause_count"], 2)
        self.assertGreaterEqual(payload["summary"]["conditional_clause_count"], 1)
        self.assertIn("omitOnEmpty", payload["changed"]["symbols"])
        self.assertIn("untrusted", payload["changed"]["state_terms"])
        self.assertTrue(payload["scenario_matrix"]["dimensions"])
        self.assertTrue(payload["scenario_matrix"]["cases"])
        self.assertTrue(payload["questions_to_verify"])
        self.assertNotIn("bug", json.dumps(payload, ensure_ascii=False).lower())
        self.assertEqual(payload["route_context"]["triggered_personas"], ["security", "requirements"])

    def test_cli_writes_review_prepare_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            artifact_file = tmp_path / "artifact.txt"
            requirements_file = tmp_path / "requirements.txt"
            review_context_file = tmp_path / "review_context.md"
            output_file = tmp_path / "review_prepare.json"

            artifact_file.write_text(read_fixture("go_repo/review_artifact.txt"), encoding="utf-8")
            requirements_file.write_text(
                "ValidateToken must reject malformed input and preserve auth invariants.\n",
                encoding="utf-8",
            )
            review_context_file.write_text(
                "- internal/auth/jwt.go\n- TestValidateTokenRejectsMalformedInput\n",
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    "python3",
                    str(self.script),
                    "--artifact-file",
                    str(artifact_file),
                    "--requirements-file",
                    str(requirements_file),
                    "--review-context-file",
                    str(review_context_file),
                    "--output-file",
                    str(output_file),
                ],
                capture_output=True,
                text=True,
                check=True,
            )

            stdout_payload = json.loads(result.stdout)
            written_payload = json.loads(output_file.read_text(encoding="utf-8"))
            self.assertEqual(stdout_payload["contract_version"], "ccr.review_prepare.v1")
            self.assertEqual(stdout_payload, written_payload)
            self.assertTrue(stdout_payload["summary_text"])
            self.assertGreaterEqual(stdout_payload["summary"]["requirement_clause_count"], 1)


if __name__ == "__main__":
    unittest.main()
