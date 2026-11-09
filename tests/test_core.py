import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cloudcrew.core import ROOT, Audit, Config, State, digest, load_env, redact
from cloudcrew.llm import object_schema, validate


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.config = Config.load(ROOT / "config/project.example.json")

    def test_example_cannot_touch_aws(self):
        with self.assertRaises(ValueError):
            self.config.validate(cloud=True)

    def test_config_binding_prevents_repointing_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = State(self.config, Path(tmp) / "state.json")
            state.save()
            other = Config(dict(self.config.values, bucket="somebody-elses-bucket"))
            with self.assertRaises(ValueError):
                State(other, state.path)

    def test_no_shell_evaluation_in_env_parser(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("TEST_SECRET=$(touch /tmp/not-executed)\nTEST_EXISTING=new\n")
            with patch.dict(os.environ, {"TEST_EXISTING": "old"}):
                load_env(path)
                self.assertEqual(os.environ["TEST_SECRET"], "$(touch /tmp/not-executed)")
                self.assertEqual(os.environ["TEST_EXISTING"], "old")

    def test_redaction_handles_keys_and_nested_plaintext(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test-never-print-this-secret"}):
            result = redact({"api_key": "sensitive", "observation": ["key=sk-test-never-print-this-secret"]})
            self.assertNotIn("never-print", json.dumps(result))
            self.assertEqual(result["api_key"], "[REDACTED]")

    def test_audit_is_chronological_and_redacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = Audit("unit-test", Path(tmp))
            audit.emit("test", "tool", {"password": "do not log"})
            events = [json.loads(line) for line in (audit.path / "events.jsonl").read_text().splitlines()]
            self.assertEqual([e["sequence"] for e in events], [1, 2])
            self.assertNotIn("do not log", str(events))

    def test_tool_arguments_reject_extra_fields_and_wrong_types(self):
        schema = object_schema({"page": {"type": "string", "enum": ["home", "trails"]}})
        for value in ({"page": "../.env"}, {"page": "home", "command": "delete"}, {"page": 1}):
            with self.assertRaises(ValueError):
                validate(value, schema)

    def test_hash_is_order_independent(self):
        self.assertEqual(digest({"a": 1, "b": 2}), digest({"b": 2, "a": 1}))


if __name__ == "__main__":
    unittest.main()
