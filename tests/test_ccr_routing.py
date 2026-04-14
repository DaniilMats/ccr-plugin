from __future__ import annotations

import json
import unittest

from util import FIXTURES_DIR, REPO_ROOT, load_module, read_fixture


class TestCCRRouting(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.routing = load_module("ccr_routing_module", "quality/scripts/ccr_routing.py")
        cls.validator = load_module("validator_module_routing", "quality/scripts/llm-proxy/validator.py")
        cls.route_input_schema = str(REPO_ROOT / "quality" / "contracts" / "v1" / "route_input.schema.json")
        cls.route_plan_schema = str(REPO_ROOT / "quality" / "contracts" / "v1" / "route_plan.schema.json")

    def _assert_plan_matches_fixture(self, input_name: str, output_name: str) -> None:
        payload = json.loads(read_fixture(input_name))
        is_valid, violations = self.validator.validate_response(json.dumps(payload), self.route_input_schema)
        self.assertTrue(is_valid, violations)

        request = self.routing.RoutingInput.model_validate(payload)
        plan = self.routing.build_routing_plan(request).model_dump()

        is_valid, violations = self.validator.validate_response(json.dumps(plan), self.route_plan_schema)
        self.assertTrue(is_valid, violations)

        expected = json.loads(read_fixture(output_name))
        self.assertEqual(plan, expected)

    def test_small_route_plan_snapshot(self) -> None:
        self._assert_plan_matches_fixture(
            "routing/route_input_small.json",
            "routing/expected_route_plan_small.json",
        )

    def test_full_matrix_route_plan_snapshot(self) -> None:
        self._assert_plan_matches_fixture(
            "routing/route_input_full_matrix.json",
            "routing/expected_route_plan_full_matrix.json",
        )

    def test_logic_is_not_allowed_in_triggered_personas(self) -> None:
        with self.assertRaises(Exception):
            self.routing.RoutingInput.model_validate(
                {
                    "changed_lines": 10,
                    "triggered_personas": ["logic"],
                }
            )


if __name__ == "__main__":
    unittest.main()
