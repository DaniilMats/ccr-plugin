from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from util import REPO_ROOT, load_module


class TestValidator(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_module("validator_module", "quality/scripts/llm-proxy/validator.py")

    def _write_text(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def test_validate_response_rejects_missing_schema_file(self) -> None:
        valid, violations = self.module.validate_response("{}", str(REPO_ROOT / "tests" / "fixtures" / "missing.schema.json"))
        self.assertFalse(valid)
        self.assertEqual(len(violations), 1)
        self.assertIn("Schema file not found", violations[0])

    def test_validate_response_rejects_invalid_schema_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            schema_path = Path(tmp) / "schema.json"
            self._write_text(schema_path, "{not-json}\n")
            valid, violations = self.module.validate_response("{}", str(schema_path))

        self.assertFalse(valid)
        self.assertEqual(len(violations), 1)
        self.assertIn("Invalid JSON in schema file", violations[0])

    def test_validate_response_reports_nested_constraint_violations(self) -> None:
        schema = {
            "type": "object",
            "required": ["name", "status", "tags", "meta"],
            "properties": {
                "name": {"type": "string", "minLength": 3, "pattern": "^[a-z]+$"},
                "status": {"enum": ["ok", "warn"]},
                "tags": {
                    "type": "array",
                    "minItems": 2,
                    "uniqueItems": True,
                    "items": {"type": "string"},
                },
                "meta": {
                    "type": "object",
                    "required": ["count"],
                    "properties": {
                        "count": {"type": "integer", "minimum": 1},
                    },
                    "additionalProperties": False,
                },
            },
            "additionalProperties": False,
        }
        response = {
            "name": "Ab",
            "status": "bad",
            "tags": ["dup", "dup"],
            "meta": {"count": 0, "extra": 1},
            "extra": True,
        }

        with tempfile.TemporaryDirectory() as tmp:
            schema_path = Path(tmp) / "schema.json"
            self._write_text(schema_path, json.dumps(schema))
            valid, violations = self.module.validate_response(json.dumps(response), str(schema_path))

        self.assertFalse(valid)
        joined = "\n".join(violations)
        self.assertIn("$.name: string 'Ab' does not match pattern", joined)
        self.assertIn("$.status: value \"bad\" not in enum", joined)
        self.assertIn("$.tags: array items must be unique", joined)
        self.assertIn("$.meta.count: 0 < minimum 1", joined)
        self.assertIn("$.meta: additional property 'extra' not allowed", joined)
        self.assertIn("$: additional property 'extra' not allowed", joined)

    def test_validate_response_handles_anyof_and_oneof_failures(self) -> None:
        anyof_schema = {
            "anyOf": [
                {"type": "string", "pattern": "^ok$"},
                {"type": "integer", "minimum": 10},
            ]
        }
        oneof_schema = {
            "oneOf": [
                {"type": "number"},
                {"type": "integer"},
            ]
        }

        with tempfile.TemporaryDirectory() as tmp:
            anyof_path = Path(tmp) / "anyof.json"
            oneof_path = Path(tmp) / "oneof.json"
            self._write_text(anyof_path, json.dumps(anyof_schema))
            self._write_text(oneof_path, json.dumps(oneof_schema))

            any_valid, any_violations = self.module.validate_response("3", str(anyof_path))
            one_valid, one_violations = self.module.validate_response("3", str(oneof_path))

        self.assertFalse(any_valid)
        self.assertIn("did not match any of the anyOf schemas", any_violations[0])
        self.assertFalse(one_valid)
        self.assertIn("matched 2", one_violations[0])

    def test_validate_response_accepts_valid_allof_and_multipleof_payload(self) -> None:
        schema = {
            "allOf": [
                {"type": "integer"},
                {"minimum": 2},
                {"multipleOf": 2},
            ]
        }

        with tempfile.TemporaryDirectory() as tmp:
            schema_path = Path(tmp) / "schema.json"
            self._write_text(schema_path, json.dumps(schema))
            valid, violations = self.module.validate_response("4", str(schema_path))

        self.assertTrue(valid)
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
