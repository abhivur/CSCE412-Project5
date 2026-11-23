import json
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cloudcrew.core import Audit
from cloudcrew.pipeline_setup import SetupAWS
from cloudcrew.release_aws import ReleaseAWS, ReleaseAwsError
from test_releases import config_fixture


class AwsInputTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.audit = Audit('unit-fixture-only', Path(self.tmp.name))
        self.config = config_fixture()

    def inspect_request(self, expected, stdout='{}', returncode=0):
        def run(command, **kwargs):
            argument = command[command.index('--cli-input-json') + 1]
            self.assertTrue(argument.startswith('file://'))
            self.path = Path(argument[len('file://'):])
            self.assertEqual(json.loads(self.path.read_text()), expected)
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
            self.assertNotIn('input', kwargs)
            self.assertEqual(kwargs['stdin'], subprocess.DEVNULL)
            return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr='Unit fixture error' if returncode else '')
        return run

    def test_identity_request_uses_valid_empty_json_and_cleans_up(self):
        aws = SetupAWS(self.config, self.audit, profile='unit-profile')
        with patch('cloudcrew.pipeline_setup.subprocess.run', side_effect=self.inspect_request({}, '{"Account":"123456789012"}')):
            self.assertEqual(aws.call('sts', 'get-caller-identity', {})['Account'], self.config['account_id'])
        self.assertFalse(self.path.exists())
        self.assertFalse(self.path.parent.exists())

    def test_nested_source_credentials_are_not_in_process_arguments(self):
        secret = 'unit-fixture-source-token'
        params = {'pipeline': {'configuration': {'OAuthToken': secret}}}
        check = self.inspect_request(params)
        def run(command, **kwargs):
            self.assertNotIn(secret, ' '.join(command))
            return check(command, **kwargs)
        with patch('cloudcrew.pipeline_setup.subprocess.run', side_effect=run):
            SetupAWS(self.config, self.audit).call('codepipeline', 'create-pipeline', params)
        self.assertFalse(self.path.exists())

    def test_release_requests_use_the_same_file_transport(self):
        params = {'Id': self.config['distribution_id']}
        with patch('cloudcrew.release_aws.subprocess.run', side_effect=self.inspect_request(params)):
            ReleaseAWS(self.config, self.audit).call('cloudfront', 'get-distribution-config', params)
        self.assertFalse(self.path.exists())

    def test_failed_aws_request_still_removes_the_request_file(self):
        with patch('cloudcrew.pipeline_setup.subprocess.run', side_effect=self.inspect_request({}, returncode=1)):
            with self.assertRaises(ReleaseAwsError):
                SetupAWS(self.config, self.audit).call('sts', 'get-caller-identity', {})
        self.assertFalse(self.path.exists())

    def test_timed_out_request_still_removes_the_request_file(self):
        inspect = self.inspect_request({})
        def run(command, **kwargs):
            inspect(command, **kwargs)
            raise subprocess.TimeoutExpired(command, 100)
        with patch('cloudcrew.pipeline_setup.subprocess.run', side_effect=run):
            with self.assertRaises(subprocess.TimeoutExpired):
                SetupAWS(self.config, self.audit).call('sts', 'get-caller-identity', {})
        self.assertFalse(self.path.exists())


if __name__ == '__main__': unittest.main()
