from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from util import FIXTURES_DIR, REPO_ROOT, load_module, read_fixture


class TestContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.validator = load_module("validator_module_contracts", "quality/scripts/llm-proxy/validator.py")
        cls.run_init = load_module("ccr_run_init_module", "quality/scripts/ccr_run_init.py")
        cls.routing = load_module("ccr_routing_contracts", "quality/scripts/ccr_routing.py")
        cls.static_analysis = load_module("static_analysis_contracts", "quality/scripts/llm-proxy/static_analysis.py")
        cls.schemas_dir = REPO_ROOT / "quality" / "contracts" / "v1"

    def _assert_valid(self, payload: dict, schema_name: str) -> None:
        schema_path = self.schemas_dir / schema_name
        is_valid, violations = self.validator.validate_response(json.dumps(payload), str(schema_path))
        self.assertTrue(is_valid, f"{schema_name}: {violations}")

    def test_all_v1_schemas_exist(self) -> None:
        expected = {
            "run_manifest.schema.json",
            "route_input.schema.json",
            "route_plan.schema.json",
            "static_analysis.schema.json",
            "reviewer_result.schema.json",
            "consolidated_candidate.schema.json",
            "verification_batch.schema.json",
            "verification_result.schema.json",
            "posting_manifest.schema.json",
        }
        existing = {path.name for path in self.schemas_dir.glob("*.json")}
        self.assertTrue(expected.issubset(existing))

    def test_run_manifest_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = self.run_init._build_manifest(Path(tmp), "test-run")
            self._assert_valid(manifest, "run_manifest.schema.json")

    def test_route_input_and_plan_contracts(self) -> None:
        route_input = json.loads(read_fixture("routing/route_input_small.json"))
        self._assert_valid(route_input, "route_input.schema.json")
        plan = self.routing.build_routing_plan(self.routing.RoutingInput.model_validate(route_input)).model_dump()
        self._assert_valid(plan, "route_plan.schema.json")

    def test_static_analysis_contract(self) -> None:
        payload = self.static_analysis.empty_result()
        self._assert_valid(payload, "static_analysis.schema.json")

    def test_reviewer_result_contract(self) -> None:
        payload = {
            "contract_version": "ccr.reviewer_result.v1",
            "findings": [
                {
                    "severity": "warning",
                    "file": "internal/auth/jwt.go",
                    "line": 12,
                    "message": "Example reviewer message.",
                }
            ],
            "summary": "Example summary.",
            "raw_response": "{}",
        }
        self._assert_valid(payload, "reviewer_result.schema.json")

    def test_consolidated_candidate_contract(self) -> None:
        payload = {
            "contract_version": "ccr.consolidated_candidate.v1",
            "candidate_id": "F1",
            "persona": "security",
            "file": "internal/auth/jwt.go",
            "line": 12,
            "message": "Example candidate.",
            "severity": "bug",
            "reviewers": ["security_p1", "security_p2"],
            "consensus": "2/2",
            "evidence_sources": ["diff_hunk", "gosec"],
        }
        self._assert_valid(payload, "consolidated_candidate.schema.json")

    def test_verification_batch_contract(self) -> None:
        payload = {
            "contract_version": "ccr.verification_batch.v1",
            "file": "internal/auth/jwt.go",
            "diff_hunk": "@@ -1,1 +1,2 @@",
            "file_context": "package auth",
            "requirements": "",
            "candidates": [
                {
                    "candidate_id": "F1",
                    "file": "internal/auth/jwt.go",
                    "line": 12,
                    "message": "Example candidate.",
                }
            ],
        }
        self._assert_valid(payload, "verification_batch.schema.json")

    def test_verification_result_contract(self) -> None:
        payload = {
            "contract_version": "ccr.verification_result.v1",
            "verified_findings": [
                {
                    "candidate_id": "F1",
                    "verdict": "confirmed",
                    "file": "internal/auth/jwt.go",
                    "line": 12,
                    "revised_message": "Tightened message.",
                    "evidence": "Supported by the provided diff.",
                }
            ],
            "summary": "One finding confirmed.",
            "raw_response": "{}",
        }
        self._assert_valid(payload, "verification_result.schema.json")

    def test_posting_manifest_contract(self) -> None:
        payload = {
            "contract_version": "ccr.posting_manifest.v1",
            "project": "group/project",
            "mr_iid": 123,
            "approved_findings": [
                {
                    "finding_number": 1,
                    "file": "internal/auth/jwt.go",
                    "line": 12,
                    "message": "Comment body.",
                    "fingerprint": "abc123",
                }
            ],
        }
        self._assert_valid(payload, "posting_manifest.schema.json")


if __name__ == "__main__":
    unittest.main()
