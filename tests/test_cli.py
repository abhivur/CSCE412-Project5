import unittest
from unittest.mock import MagicMock, patch

from cloudcrew.cli import main


class CliTests(unittest.TestCase):
    def test_deploy_uses_the_same_plan_and_approval_path(self):
        # Only a CLI routing fixture. Does not create real agent or AWS evidence.
        with patch("sys.argv", ["cloudcrew", "deploy", "--stage", "certificate-dns"]), \
                patch("cloudcrew.cli.Audit"), patch("cloudcrew.cli.Config.load"), \
                patch("cloudcrew.deploy.Deployment") as deployment, patch("builtins.print"):
            instance = deployment.return_value
            plan = {"sha256": "fixture-plan"}
            instance.plan.return_value = plan
            instance.apply.return_value = {"status": "applied"}
            main()
            instance.plan.assert_called_once_with("certificate-dns")
            instance.apply.assert_called_once_with(plan)


if __name__ == "__main__":
    unittest.main()
