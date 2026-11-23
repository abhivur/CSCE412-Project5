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

    def test_s3_download_uses_explicit_arguments_and_preserves_bytes_and_etag(self):
        downloaded = []
        def run(command, **kwargs):
            self.assertNotIn('--cli-input-json', command)
            self.assertEqual(command[command.index('--bucket') + 1], self.config['site_bucket'])
            self.assertEqual(command[command.index('--key') + 1], 'index.html')
            self.assertEqual(kwargs['stdin'], subprocess.DEVNULL)
            path = Path(command[-1])
            downloaded.append(path)
            path.write_bytes(b'actual unit fixture bytes')
            return subprocess.CompletedProcess(command, 0, stdout='{"ETag":"unit-etag"}', stderr='')
        with patch('cloudcrew.release_aws.subprocess.run', side_effect=run):
            data, etag = ReleaseAWS(self.config, self.audit).get(self.config['site_bucket'], 'index.html')
        self.assertEqual(data, b'actual unit fixture bytes')
        self.assertEqual(etag, 'unit-etag')
        self.assertFalse(downloaded[0].exists())

    def test_s3_download_retains_optional_version_and_etag_conditions(self):
        params = {'Bucket': self.config['site_bucket'], 'Key': 'index.html', 'VersionId': 'unit-version', 'IfMatch': 'unit-etag'}
        process = subprocess.CompletedProcess([], 0, stdout='{}', stderr='')
        with patch('cloudcrew.release_aws.subprocess.run', return_value=process) as run:
            ReleaseAWS(self.config, self.audit).call('s3api', 'get-object', params, output_file=Path(self.tmp.name) / 'out')
        command = run.call_args.args[0]
        self.assertEqual(command[command.index('--version-id') + 1], 'unit-version')
        self.assertEqual(command[command.index('--if-match') + 1], 'unit-etag')

    def test_missing_s3_object_is_an_optional_state_read_not_a_parser_error(self):
        process = subprocess.CompletedProcess([], 254, stdout='', stderr='An error occurred (NoSuchKey) when calling GetObject')
        with patch('cloudcrew.release_aws.subprocess.run', return_value=process):
            result = ReleaseAWS(self.config, self.audit).get_json(self.config['artifact_bucket'], 'control/production.json', optional=True)
        self.assertEqual(result, (None, None))

    def test_s3_download_without_output_file_is_rejected_before_execution(self):
        with patch('cloudcrew.release_aws.subprocess.run') as run, self.assertRaises(ValueError):
            ReleaseAWS(self.config, self.audit).call('s3api', 'get-object', {'Bucket': self.config['site_bucket'], 'Key': 'index.html'})
        run.assert_not_called()


if __name__ == '__main__': unittest.main()
