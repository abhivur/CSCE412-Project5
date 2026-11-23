import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import patch


path = Path(__file__).resolve().parents[1] / "scripts/complete_project6.py"
spec = importlib.util.spec_from_file_location("experiment_runner", path)
runner_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner_module)


class ExperimentRunnerTests(unittest.TestCase):
    def test_modified_first_file_keeps_git_status_columns(self):
        runner = runner_module.Experiments.__new__(runner_module.Experiments)
        output = " M cloudcrew/release_agents.py\n?? scripts/complete_project6.py\n"
        with patch.object(runner_module.subprocess, "check_output", return_value=output):
            rows = runner.git("status", "--porcelain").splitlines()
        self.assertEqual(rows[0][:3], " M ")
        self.assertEqual(rows[0][3:], "cloudcrew/release_agents.py")
        self.assertTrue(all(row[3:] in runner_module.OWN_FILES for row in rows))

    def test_unrelated_work_is_rejected_before_network_or_cloud_changes(self):
        runner = runner_module.Experiments.__new__(runner_module.Experiments)
        def git(*args):
            if args == ("branch", "--show-current"):
                return "main"
            if args == ("status", "--porcelain"):
                return " M website/content.json"
            self.fail("Preflight must stop before another Git operation")
        runner.git = git
        with patch.dict(os.environ, {"OPENAI_API_KEY": "unit-fixture-only"}):
            with self.assertRaisesRegex(RuntimeError, "website/content.json"):
                runner.preflight()


if __name__ == "__main__":
    unittest.main()
