from __future__ import annotations

import unittest

from util import load_module


class TestFindingFormat(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_module("finding_format_module", "quality/scripts/ccr_runtime/finding_format.py")

    def test_structured_fields_preserve_explicit_sections(self) -> None:
        finding = {
            "severity": "bug",
            "persona": "logic",
            "message": "show is wrong.",
            "title": "Negative days rendered",
            "problem": "CalendarDaysUntil can return a negative value on this path.",
            "impact": "Users can see 'Due in -2 days'.",
            "suggested_fixes": [
                "Change case days == 0 to case days <= 0.",
                "Clamp negative days to zero before the switch.",
            ],
        }

        structured = self.module.structured_finding_fields(finding)
        self.assertEqual(structured["severity_label"], "BUG")
        self.assertEqual(structured["title"], "Negative days rendered")
        self.assertEqual(structured["problem"], "CalendarDaysUntil can return a negative value on this path.")
        self.assertEqual(structured["impact"], "Users can see 'Due in -2 days'.")
        self.assertEqual(
            structured["suggested_fixes"],
            [
                "Change case days == 0 to case days <= 0.",
                "Clamp negative days to zero before the switch.",
            ],
        )

    def test_render_comment_body_derives_fallback_sections(self) -> None:
        finding = {
            "severity": "warning",
            "persona": "performance",
            "message": "fetchData recomputes query_hash before calling GetTransactions. Pass the precomputed hash into fetchTransactions instead of hashing the same inputs twice.",
            "evidence": "This adds avoidable duplicate work on every request through this path.",
        }

        body = self.module.render_comment_body(finding)
        self.assertIn("**WARNING** — fetchData recomputes query_hash before calling GetTransactions.", body)
        self.assertIn("**Problem**: fetchData recomputes query_hash before calling GetTransactions.", body)
        self.assertIn("**Impact**: This adds avoidable duplicate work on every request through this path.", body)
        self.assertIn("**Suggested fixes**:", body)
        self.assertIn("1. **(Recommended)** Pass the precomputed hash into fetchTransactions instead of hashing the same inputs twice.", body)


if __name__ == "__main__":
    unittest.main()
