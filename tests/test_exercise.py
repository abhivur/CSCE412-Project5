import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from cloudcrew.core import Audit, Config
from cloudcrew.exercise import observed_default_document_failure, run_exercise


def measured(status="passed", root=200):
    return {"status": status, "artifact": "temporary-unit-fixture.json", "checks": [
        {"name": "page:/", "passed": root == 200, "observation": {"status_code": root}},
        {"name": "page:index.html", "passed": True, "observation": {"status_code": 200}}]}


class ExerciseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.audit = Audit("unit-test-only", Path(self.tmp.name))
        self.config = Config({
            "project_id": "unit-fixture-project5",
            "domain": "project.example.test",
            "region": "us-east-1",
            "bucket": "unit-fixture-project5",
            "aws_profile": "unit-test-only",
            "hosted_zone_id": None,
            "model": "unit-test-model",
            "max_model_calls_per_agent": 8,
            "max_output_tokens": 2200,
        })
        self.deployment = MagicMock()
        self.deployment.state = SimpleNamespace(data={})
        self.deployment.approval_denied = False
        self.deployment.plan.side_effect = lambda stage: {"stage": stage}
        self.toolbox = SimpleNamespace(deployment=self.deployment, last_verification=None)

    def test_expected_failure_requires_live_root_error_and_working_explicit_index(self):
        self.assertTrue(observed_default_document_failure(measured("failed", 404)))
        self.assertFalse(observed_default_document_failure(measured("failed", 200)))
        report = measured("failed", 404)
        report["checks"][1]["passed"] = False
        self.assertFalse(observed_default_document_failure(report))

    def test_no_failure_change_is_attempted_when_baseline_fails(self):
        self.toolbox.last_verification = measured("failed", 404)
        with patch("cloudcrew.exercise.ResponsesModel"), patch("cloudcrew.exercise.Toolbox", return_value=self.toolbox), \
                patch("cloudcrew.exercise.Agent") as agent, patch("builtins.print"):
            agent.return_value.run.return_value = {"status": "blocked", "summary": "Unit fixture"}
            with self.assertRaisesRegex(RuntimeError, "No controlled failure was introduced"):
                run_exercise(self.config, self.audit)
        self.deployment.apply.assert_not_called()

    def test_exercise_records_failure_and_independent_recovery_before_passing(self):
        def apply(plan):
            self.deployment.state.data["failure_active"] = plan["stage"] == "failure"
        self.deployment.apply.side_effect = apply
        def run(message):
            task = message["task"]
            if "baseline" in task:
                self.toolbox.last_verification = measured()
            elif "Measure the controlled failure" in task:
                self.toolbox.last_verification = measured("failed", 404)
            elif "diagnose these measured failures" in task:
                self.deployment.apply(self.deployment.plan("recovery"))
            else:
                self.toolbox.last_verification = measured()
            return {"status": "complete", "summary": "Unit fixture, not execution evidence"}
        with patch("cloudcrew.exercise.ResponsesModel"), patch("cloudcrew.exercise.Toolbox", return_value=self.toolbox), \
                patch("cloudcrew.exercise.Agent") as agent, patch("builtins.print"):
            agent.return_value.run.side_effect = run
            result = run_exercise(self.config, self.audit)
        self.assertEqual(result["status"], "passed")
        self.assertTrue(result["failure_observed"])
        self.assertTrue(result["website_restored"])
        self.assertEqual([call.args[0]["stage"] for call in self.deployment.apply.call_args_list], ["failure", "recovery"])


if __name__ == "__main__":
    unittest.main()
