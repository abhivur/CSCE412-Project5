import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cloudcrew.aws import AWS, AwsError
from cloudcrew.core import ROOT, Audit, Config, State, digest
from cloudcrew.deploy import Deployment, bucket_policy, contains_config, distribution_config


def config_fixture():
    return Config(dict(json.loads((ROOT / "config/project.example.json").read_text()),
                       domain="csce412teststudent.me", bucket="csce412-test-fixture-only"))


class FakeAWS:
    """Unit-test fixture only. Never presented as agent or AWS execution evidence."""
    def __init__(self, handler=None):
        self.mutable = False
        self.calls = []
        self.handler = handler or (lambda *args, **kwargs: {})

    def call(self, service, operation, params=None, **kwargs):
        self.calls.append((service, operation, params, kwargs))
        return self.handler(service, operation, params, **kwargs)


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = config_fixture()
        self.audit = Audit("unit-test-only", self.root / "runs")
        self.state = State(self.config, self.root / "state.json")

    def deployment(self, aws=None):
        return Deployment(self.config, self.audit, self.state, aws or FakeAWS())

    def test_aws_rejects_unapproved_services_deletions_and_writes(self):
        aws = AWS(self.config, self.audit)
        for service, operation in [("ec2", "run-instances"), ("s3api", "delete-bucket"), ("s3api", "create-bucket")]:
            with self.assertRaises(PermissionError):
                aws.call(service, operation, {})

    def test_plan_tampering_rejected_before_any_write(self):
        deployment = self.deployment()
        plan = deployment.plan("foundation")
        plan["details"]["bucket"] = "someone-else"
        with patch("cloudcrew.deploy.ROOT", self.root):
            with self.assertRaisesRegex(ValueError, "stale or edited"):
                deployment.apply(plan)
        self.assertFalse(deployment.aws.calls)

    def test_state_change_invalidates_plan(self):
        deployment = self.deployment()
        plan = deployment.plan("foundation")
        self.state.data["some_resource"] = "new-state"
        self.state.save()
        with patch("cloudcrew.deploy.ROOT", self.root):
            with self.assertRaisesRegex(ValueError, "stale or edited"):
                deployment.apply(plan)

    def test_noninteractive_apply_cannot_bypass_approval(self):
        deployment = self.deployment()
        plan = deployment.plan("foundation")
        with patch("cloudcrew.deploy.ROOT", self.root), patch("cloudcrew.deploy.sys.stdin.isatty", return_value=False):
            with self.assertRaisesRegex(PermissionError, "interactive"):
                deployment.apply(plan)
        self.assertFalse(deployment.aws.mutable)

    def test_wrong_approval_phrase_cannot_execute(self):
        deployment = self.deployment()
        plan = deployment.plan("foundation")
        with patch("cloudcrew.deploy.ROOT", self.root), patch("cloudcrew.deploy.sys.stdin.isatty", return_value=True), patch("builtins.print"):
            with self.assertRaises(PermissionError):
                deployment.apply(plan, input_fn=lambda _: "yes")
        self.assertFalse(deployment.aws.calls)

    def test_denied_approval_cannot_trigger_repeated_prompts_in_one_run(self):
        deployment = self.deployment()
        deployment.approval_denied = True
        with self.assertRaisesRegex(PermissionError, "no further approval prompts"):
            deployment.apply(deployment.plan("foundation"), input_fn=lambda _: self.fail("Approval must not be requested again"))

    def test_readiness_requires_invalidation_completion_too(self):
        self.state.data.update(distribution_id="EDIST", distribution_arn="arn:test", last_invalidation="IEXAMPLE")
        deployment = self.deployment(FakeAWS(lambda *_args, **_kwargs: {"Invalidation": {"Status": "InProgress"}}))
        with patch.object(deployment, "owned_distribution", return_value={"Status": "Deployed"}), \
                patch("cloudcrew.deploy.time.monotonic", side_effect=[0, 2]):
            with self.assertRaises(TimeoutError):
                deployment.wait_until_ready(timeout_seconds=1)

    def test_readiness_returns_only_when_deployment_and_invalidation_complete(self):
        self.state.data.update(distribution_id="EDIST", distribution_arn="arn:test", last_invalidation="IEXAMPLE")
        deployment = self.deployment(FakeAWS(lambda *_args, **_kwargs: {"Invalidation": {"Status": "Completed"}}))
        with patch.object(deployment, "owned_distribution", return_value={"Status": "Deployed"}), patch("builtins.print"):
            result = deployment.wait_until_ready()
        self.assertEqual(result, {"distribution": "Deployed", "invalidation": "Completed"})

    def test_foreign_bucket_cannot_be_retagged_or_changed(self):
        aws = FakeAWS(lambda *_args, **_kwargs: {"TagSet": [{"Key": "CloudCrewBinding", "Value": "foreign-project"}]})
        deployment = self.deployment(aws)
        with self.assertRaises(PermissionError):
            deployment._ensure_private_bucket({})
        self.assertFalse(any(operation.startswith("put-") for _, operation, *_ in aws.calls))

    def test_existing_project_bucket_is_reused_and_secured(self):
        aws = FakeAWS(lambda *_args, **_kwargs: {"TagSet": [{"Key": "CloudCrewBinding", "Value": self.config.binding}]})
        deployment = self.deployment(aws)
        deployment._ensure_private_bucket({})
        operations = [operation for _, operation, *_ in aws.calls]
        self.assertNotIn("create-bucket", operations)
        self.assertIn("put-public-access-block", operations)
        self.assertTrue(self.state.data["bucket_ready"])

    def test_bucket_policy_limits_cloudfront_to_exact_distribution(self):
        policy = bucket_policy(self.config, "arn:aws:cloudfront::111122223333:distribution/EXAMPLE")
        allow = policy["Statement"][0]
        self.assertEqual(allow["Action"], "s3:GetObject")
        self.assertEqual(allow["Condition"]["StringEquals"]["AWS:SourceArn"], "arn:aws:cloudfront::111122223333:distribution/EXAMPLE")
        self.assertEqual(policy["Statement"][1]["Effect"], "Deny")

    def test_distribution_uses_private_rest_origin_tls_and_real_404(self):
        result = distribution_config(self.config, {"oac_id": "EOAC", "certificate_arn": "certificate"})
        self.assertNotIn("s3-website", result["Origins"]["Items"][0]["DomainName"])
        self.assertEqual(result["DefaultCacheBehavior"]["ViewerProtocolPolicy"], "redirect-to-https")
        self.assertTrue(all(item["ResponseCode"] == "404" for item in result["CustomErrorResponses"]["Items"]))

    def test_failure_requires_passing_baseline(self):
        self.state.data.update(distribution_id="EDIST", distribution_arn="arn:test")
        deployment = self.deployment()
        with patch.object(deployment, "owned_distribution", return_value={"Status": "Deployed", "DistributionConfig": {"DefaultRootObject": "index.html"}}):
            with self.assertRaisesRegex(ValueError, "passing live verification"):
                deployment.plan("failure")

    def test_recovery_refuses_unrelated_configuration_drift(self):
        self.state.data.update(failure_backup={"DefaultRootObject": "index.html"}, failure_active=True,
                               failure_config_digest=digest({"DefaultRootObject": "controlled-missing.html"}))
        deployment = self.deployment()
        with patch.object(deployment, "owned_distribution", return_value={"Status": "Deployed", "DistributionConfig": {"DefaultRootObject": "someone-elses-change.html"}}):
            with self.assertRaisesRegex(ValueError, "outside the exercise"):
                deployment.plan("recovery")

    def test_failure_saves_backup_before_aws_update_even_if_request_fails(self):
        self.state.data["distribution_id"] = "EDIST"
        def handler(*args, **kwargs):
            saved = json.loads(self.state.path.read_text())
            self.assertEqual(saved["failure_backup"], {"DefaultRootObject": "index.html"})
            raise AwsError("cloudfront", "update-distribution", "network interrupted")
        deployment = self.deployment(FakeAWS(handler))
        with patch.object(deployment, "owned_distribution", return_value={}):
            with self.assertRaises(AwsError):
                deployment._introduce_failure({"before": {"DefaultRootObject": "index.html"},
                    "after": {"DefaultRootObject": "missing.html"}, "etag": "old", "baseline_run": "unit-fixture"})
        self.assertTrue(json.loads(self.state.path.read_text())["failure_active"])

    def test_recovery_can_reconcile_an_interrupted_request_that_never_changed_aws(self):
        original = {"DefaultRootObject": "index.html"}
        self.state.data.update(distribution_id="EDIST", failure_backup=original, failure_active=True,
                               failure_config_digest=digest({"DefaultRootObject": "missing.html"}))
        aws = FakeAWS(lambda *_args, **_kwargs: {"DistributionConfig": original, "ETag": "original-etag"})
        deployment = self.deployment(aws)
        with patch.object(deployment, "owned_distribution", return_value={"Status": "Deployed", "DistributionConfig": original}):
            plan = deployment.plan("recovery")
        self.assertEqual(plan["operations"][0], "confirm_original_configuration")
        self.assertEqual(plan["details"]["after"], original)

    def test_aws_response_defaults_do_not_mask_managed_configuration_drift(self):
        self.assertTrue(contains_config({"Enabled": True, "Extra": 5}, {"Enabled": True}))
        self.assertFalse(contains_config({"Enabled": False, "Extra": 5}, {"Enabled": True}))


if __name__ == "__main__":
    unittest.main()
