import json
import tempfile
import unittest
from pathlib import Path

from cloudcrew.agents import Agent, Tool
from cloudcrew.core import Audit, Config
from cloudcrew.llm import object_schema


class ScriptedModel:
    """Explicit unit fixture, not a runnable provider or fabricated execution log."""
    def __init__(self, calls):
        self.calls = iter(calls)
        self.inputs = []

    def respond(self, instructions, conversation, tools, max_tokens):
        self.inputs.append(list(conversation))
        name, arguments = next(self.calls)
        return {"id": "test-fixture", "output": [{"type": "function_call", "name": name,
            "arguments": json.dumps(arguments), "call_id": "unit-call"}]}


class TestToolbox:
    cloud = False

    def __init__(self):
        self.executed = False

    def tools(self, role):
        def execute():
            self.executed = True
            return {"status": "passed", "observation": "test-fixture-only"}
        return [Tool("check_site", "Unit fixture", object_schema({}), execute)]


def finish(status="complete"):
    return {"status": status, "summary": "Unit fixture only", "findings": [], "evidence": [], "tasks": []}


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.audit = Audit("unit-test-only", Path(self.tmp.name))
        self.toolbox = TestToolbox()
        self.config = Config.load()

    def test_tool_result_passed_back_to_model_before_completion(self):
        model = ScriptedModel([("check_site", {}), ("finish", finish())])
        result = Agent("verification", self.config, self.audit, self.toolbox, model).run({"task": "unit fixture"})
        self.assertEqual(result["status"], "complete")
        outputs = [v for v in model.inputs[1] if v.get("type") == "function_call_output"]
        self.assertEqual(json.loads(outputs[0]["output"])["status"], "passed")
        self.assertTrue(self.toolbox.executed)

    def test_completion_without_required_observations_is_rejected(self):
        model = ScriptedModel([("finish", finish()), ("finish", finish("blocked"))])
        result = Agent("verification", self.config, self.audit, self.toolbox, model).run({"task": "unit fixture"})
        self.assertEqual(result["status"], "blocked")
        self.assertIn("Before completion", model.inputs[1][-1]["output"])

    def test_unknown_tool_cannot_execute(self):
        model = ScriptedModel([("run_shell", {"command": "anything"}), ("finish", finish("blocked"))])
        Agent("verification", self.config, self.audit, self.toolbox, model).run({"task": "unit fixture"})
        self.assertFalse(self.toolbox.executed)
        self.assertIn("outside its role", model.inputs[1][-1]["output"])

    def test_interactive_deployment_cannot_finish_after_only_preparing_a_plan(self):
        class CloudToolbox:
            cloud = True
            interactive = True

            def tools(self, role):
                return [Tool("prepare_plan", "Unit fixture", object_schema({}), lambda: {"status": "prepared"})]

        model = ScriptedModel([("prepare_plan", {}), ("finish", finish()), ("finish", finish("blocked"))])
        result = Agent("infrastructure", self.config, self.audit, CloudToolbox(), model).run({"task": "unit fixture"})
        self.assertEqual(result["status"], "blocked")
        self.assertIn("apply_plan", model.inputs[2][-1]["output"])

    def failure_toolbox(self, root_status=404):
        class CloudToolbox:
            cloud = True
            interactive = False

            def tools(self, role):
                report = {"status": "failed", "checks": [
                    {"name": "page:/", "passed": False, "observation": {"status_code": root_status}},
                    {"name": "page:index.html", "passed": True, "observation": {"status_code": 200}}]}
                return [Tool("verify_deployment", "Unit fixture", object_schema({}), lambda: report)]
        return CloudToolbox()

    def test_expected_failure_measurement_can_finish_without_claiming_healthy(self):
        model = ScriptedModel([("verify_deployment", {}), ("finish", finish())])
        result = Agent("verification", self.config, self.audit, self.failure_toolbox(), model).run(
            {"task": "unit fixture", "context": {"verification_goal": "observe_controlled_failure"}})
        self.assertEqual(result["status"], "complete")
        observed = json.loads(model.inputs[1][-1]["output"])
        self.assertEqual(observed["status"], "failed")

    def test_normal_verification_cannot_claim_complete_with_failed_health_checks(self):
        model = ScriptedModel([("verify_deployment", {}), ("finish", finish()), ("finish", finish("blocked"))])
        result = Agent("verification", self.config, self.audit, self.failure_toolbox(), model).run({"task": "unit fixture"})
        self.assertEqual(result["status"], "blocked")
        self.assertIn("Before completion", model.inputs[2][-1]["output"])

    def test_unexpected_failure_does_not_satisfy_the_controlled_failure_task(self):
        model = ScriptedModel([("verify_deployment", {}), ("finish", finish()), ("finish", finish("blocked"))])
        result = Agent("verification", self.config, self.audit, self.failure_toolbox(503), model).run(
            {"task": "unit fixture", "context": {"verification_goal": "observe_controlled_failure"}})
        self.assertEqual(result["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
