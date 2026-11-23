import copy
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cloudcrew import site
from cloudcrew.agents import Agent, Tool
from cloudcrew.cicd import approve, pipeline_context
from cloudcrew.core import Audit, ROOT, digest, redact
from cloudcrew.llm import object_schema
from cloudcrew.pipeline_config import definitions, role_policy, validate_definitions
from cloudcrew.release_artifacts import file_manifest, package_site, validate_bundle
from cloudcrew.release_aws import ReleaseAWS
from cloudcrew.release_config import ReleaseConfig, object_path, release_id
from cloudcrew.release_verify import verify_production
from test_agents import ScriptedModel, finish
from test_releases import MemoryAWS, config_fixture, initial_layout, base_distribution


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.config = config_fixture(); self.commit = 'b' * 40
        self.source = self.root / 'source'; site.build(self.source)
        self.bundle = self.root / 'bundle'
        self.manifest = package_site(self.source, self.bundle, self.commit, self.config)

    def test_packaged_pages_have_versioned_assets_and_source_identity(self):
        for name in site.PAGES.values():
            text = (self.bundle / 'site' / name).read_text()
            self.assertIn('/releases/' + self.commit + '/assets/app.js', text)
            self.assertIn('name="gbm-release" content="' + self.commit, text)
        self.assertEqual(validate_bundle(self.bundle, self.config, self.commit)['status'], 'passed')

    def test_missing_required_asset_is_rejected_even_with_an_updated_manifest(self):
        (self.bundle / 'site/assets/app.js').unlink()
        self._rehash()
        self.assertEqual(validate_bundle(self.bundle, self.config)['status'], 'failed')

    def test_broken_link_is_rejected_even_when_bytes_match_manifest(self):
        path = self.bundle / 'site/index.html'; path.write_text(path.read_text().replace('href="about.html"', 'href="absent.html"'))
        self._rehash()
        report = validate_bundle(self.bundle, self.config)
        self.assertEqual(report['status'], 'failed')
        self.assertIn('missing', str(report['errors']))

    def test_release_for_a_different_source_commit_cannot_pass(self):
        self.assertEqual(validate_bundle(self.bundle, self.config, 'a' * 40)['status'], 'failed')

    def test_changed_release_json_cannot_claim_another_release(self):
        path = self.bundle / 'site/release.json'; value = json.loads(path.read_text()); value['release_id'] = 'a' * 40
        path.write_text(json.dumps(value)); self._rehash()
        self.assertEqual(validate_bundle(self.bundle, self.config)['status'], 'failed')

    def test_credentials_in_static_artifacts_are_rejected(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'unit-fixture-sensitive-value'}):
            (self.bundle / 'site/leak.js').write_text('unit-fixture-sensitive-value')
            with self.assertRaisesRegex(ValueError, 'credential'): file_manifest(self.bundle / 'site')

    def test_release_paths_reject_traversal_and_unsupported_files(self):
        for value in ('../outside', '/absolute', 'assets/../.env', 'assets//x', 'assets/a?token=1'):
            with self.subTest(value=value), self.assertRaises(ValueError): object_path(value)
        (self.bundle / 'site/private.pem').write_text('unit fixture')
        with self.assertRaisesRegex(ValueError, 'Unsupported'): file_manifest(self.bundle / 'site')

    def _rehash(self):
        self.manifest['files'] = file_manifest(self.bundle / 'site')
        self.manifest['manifest_digest'] = digest({k: v for k, v in self.manifest.items() if k != 'manifest_digest'})
        (self.bundle / 'manifest.json').write_text(json.dumps(self.manifest))


class PipelineTests(unittest.TestCase):
    def setUp(self): self.config = config_fixture()

    def test_pipeline_has_real_gate_before_release_and_queued_execution(self):
        plan = definitions(self.config)
        self.assertEqual(validate_definitions(plan, self.config)['status'], 'passed')
        pipeline = plan['pipeline']['pipeline']
        self.assertEqual(pipeline['executionMode'], 'QUEUED')
        self.assertEqual([s['name'] for s in pipeline['stages']], ['Source', 'BuildAndValidate', 'Release'])
        release = pipeline['stages'][2]['actions'][0]
        self.assertEqual(release['inputArtifacts'], [{'name': 'SourceArtifact'}, {'name': 'ValidatedArtifact'}])
        self.assertEqual(release['configuration']['PrimarySource'], 'SourceArtifact')

    def test_no_llm_credentials_lambda_or_cloudwatch_jobs_in_pipeline(self):
        text = json.dumps(definitions(self.config))
        self.assertNotIn('OPENAI_API_KEY', text)
        self.assertNotIn('lambda:', text)
        for project in definitions(self.config)['projects'].values():
            self.assertFalse(project['environment']['privilegedMode'])
            self.assertEqual(project['logsConfig']['cloudWatchLogs']['status'], 'DISABLED')
            self.assertEqual(project['logsConfig']['s3Logs']['status'], 'ENABLED')

    def test_validation_role_cannot_write_site_or_update_cloudfront(self):
        text = json.dumps(role_policy(self.config, 'validate'))
        self.assertNotIn(self.config['site_bucket'], text)
        self.assertNotIn('cloudfront:', text)
        self.assertNotIn('Delete', text)

    def test_release_role_targets_only_project_distribution_and_release_prefixes(self):
        statements = role_policy(self.config, 'release')['Statement']
        cf = [s for s in statements if 'cloudfront:UpdateDistribution' in s['Action']][0]
        self.assertEqual(cf['Resource'], [self.config.distribution_arn])
        site_put = [s for s in statements if 's3:PutObject' in s['Action'] and any(self.config['site_bucket'] in r for r in s['Resource'])]
        self.assertEqual(site_put[0]['Resource'], ['arn:aws:s3:::' + self.config['site_bucket'] + '/releases/*'])

    def test_webhook_is_branch_specific_and_plan_has_no_actual_token(self):
        with patch.dict(os.environ, {'GITHUB_SOURCE_TOKEN': 'unit-fixture-github-credential'}):
            plan = definitions(self.config)
        self.assertNotIn('unit-fixture-github-credential', json.dumps(plan))
        self.assertEqual(plan['webhook']['webhook']['filters'], [{'jsonPath': '$.ref', 'matchEquals': 'refs/heads/main'}])

    def test_edited_pipeline_plan_is_rejected(self):
        plan = definitions(self.config); plan['pipeline']['pipeline']['executionMode'] = 'PARALLEL'
        with self.assertRaisesRegex(ValueError, 'edited or is stale'): validate_definitions(plan, self.config)

    def test_extra_secret_configuration_fields_are_rejected(self):
        value = dict(self.config.values, api_key='unit fixture')
        with self.assertRaisesRegex(ValueError, 'credentials belong'): ReleaseConfig(value).validate()

    def test_connections_cannot_be_enabled_without_instructor_approval(self):
        value = dict(self.config.values, source_provider='connection', connection_arn='arn:aws:codeconnections:us-east-1:123456789012:connection/1234-abcd')
        with self.assertRaisesRegex(ValueError, 'instructor approval'): ReleaseConfig(value).validate()

    def test_pipeline_revision_mismatch_is_rejected(self):
        env = {'CODEBUILD_INITIATOR': 'codepipeline/' + self.config['project'], 'GBM_SOURCE_COMMIT': 'a' * 40,
               'CODEBUILD_RESOLVED_SOURCE_VERSION': 'b' * 40, 'GBM_EXECUTION_ID': 'unit-run', 'CODEBUILD_BUILD_ID': 'project:unit-build'}
        with patch.dict(os.environ, env, clear=True), self.assertRaisesRegex(ValueError, 'does not match'):
            pipeline_context(self.config)

    def test_unattended_deployment_requires_the_expected_pipeline(self):
        with patch.dict(os.environ, {'CODEBUILD_INITIATOR': 'codepipeline/unrelated'}, clear=True), self.assertRaises(PermissionError):
            pipeline_context(self.config)

    def test_context_uses_actual_commit_execution_and_build_id(self):
        env = {'CODEBUILD_INITIATOR': 'codepipeline/' + self.config['project'], 'GBM_SOURCE_COMMIT': 'a' * 40,
               'CODEBUILD_RESOLVED_SOURCE_VERSION': 'a' * 40, 'GBM_EXECUTION_ID': 'unit-run', 'CODEBUILD_BUILD_ID': 'project:unit-build'}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(pipeline_context(self.config), ('a' * 40, 'unit-run', 'unit-build'))

    def test_noninteractive_setup_approval_is_not_bypassed(self):
        with patch('sys.stdin.isatty', return_value=False), patch('builtins.print'), self.assertRaises(PermissionError): approve({'change': 'unit fixture'})

    def test_oauth_tokens_are_redacted_in_nested_payloads_and_errors(self):
        with patch.dict(os.environ, {'GITHUB_SOURCE_TOKEN': 'unit-fixture-github-credential'}):
            self.assertEqual(redact({'configuration': {'OAuthToken': 'unit-fixture-github-credential'}})['configuration']['OAuthToken'], '[REDACTED]')
            self.assertNotIn('unit-fixture-github-credential', redact('error: unit-fixture-github-credential'))


class ReleaseBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.config = config_fixture(); self.audit = Audit('unit-fixture-only', Path(self.tmp.name))

    def test_read_only_tools_cannot_write(self):
        aws = ReleaseAWS(self.config, self.audit)
        with self.assertRaises(PermissionError): aws.call('cloudfront', 'update-distribution', {'Id': self.config['distribution_id']})

    def test_release_tools_cannot_modify_unrelated_resources_or_run_iam(self):
        aws = ReleaseAWS(self.config, self.audit, mutable=True)
        for service, operation, params in [('s3api', 'put-object', {'Bucket': 'unrelated', 'Key': 'releases/a/index.html'}),
                                            ('cloudfront', 'update-distribution', {'Id': 'UNRELATED'}),
                                            ('iam', 'create-role', {})]:
            with self.subTest(operation=operation), self.assertRaises(PermissionError): aws.call(service, operation, params)

    def test_invalidation_id_is_not_mistaken_for_a_distribution_id(self):
        aws = ReleaseAWS(self.config, self.audit)
        process = subprocess.CompletedProcess([], 0, stdout='{"Invalidation":{"Status":"Completed"}}', stderr='')
        with patch('cloudcrew.release_aws.subprocess.run', return_value=process):
            result = aws.call('cloudfront', 'get-invalidation', {'DistributionId': self.config['distribution_id'], 'Id': 'INVALIDATION123'})
        self.assertEqual(result['Invalidation']['Status'], 'Completed')

    def test_codebuild_uses_service_role_credentials_not_a_local_profile(self):
        aws = ReleaseAWS(self.config, self.audit, profile='local-only')
        process = subprocess.CompletedProcess([], 0, stdout='{}', stderr='')
        with patch.dict(os.environ, {'CODEBUILD_BUILD_ID': 'unit:fixture'}), patch('cloudcrew.release_aws.subprocess.run', return_value=process) as run:
            aws.call('cloudfront', 'get-distribution-config', {'Id': self.config['distribution_id']})
        self.assertNotIn('--profile', run.call_args.args[0])
        args = run.call_args.args[0]
        self.assertTrue(args[args.index('--cli-input-json') + 1].startswith('file://'))
        self.assertNotIn('input', run.call_args.kwargs)


class ProductionVerificationTests(unittest.TestCase):
    def test_browser_failure_blocks_otherwise_healthy_http_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); config = config_fixture(); source = root / 'source'; site.build(source)
            manifest = package_site(source, root / 'bundle', 'a' * 40, config)
            aws = MemoryAWS(config); aws.cf = initial_layout(base_distribution(config), 'a' * 40)
            original_call = aws.call
            def call(service, operation, params):
                if operation == 'get-bucket-policy': return {'Policy': json.dumps({'Statement': [{'Effect': 'Allow', 'Principal': {'Service': 'cloudfront.amazonaws.com'}, 'Action': 's3:GetObject', 'Condition': {'StringEquals': {'AWS:SourceArn': config.distribution_arn}}}]})}
                if operation == 'get-origin-access-control': return {'OriginAccessControl': {'OriginAccessControlConfig': {'SigningBehavior': 'always', 'SigningProtocol': 'sigv4'}}}
                return original_call(service, operation, params)
            aws.call = call
            def fetch(url):
                if url.startswith('http://'): return {'status_code': 301, 'headers': {'location': f"https://{config['domain']}/"}}
                if '.s3.' in url: return {'status_code': 403}
                path = url.split(config['domain'] + '/', 1)[1]
                if path.startswith('missing-'): return {'status_code': 404, 'custom_404_present': True}
                if path.startswith('releases/'): path = path.split('/', 2)[2]
                path = path or 'index.html'
                return {'status_code': 200, 'body_sha256': manifest['files'][path]['sha256'], 'headers': {'via': 'cloudfront', 'x-amz-cf-id': 'unit-fixture'}}
            report = verify_production(config, manifest, aws, browser=lambda _: {'status': 'failed'}, fetcher=fetch,
                                       tls=lambda _: {'hostname_verified': True, 'chain_verified': True, 'protocol': 'TLSv1.3'},
                                       public_release=lambda _: {'release_id': 'a' * 40, 'source_commit': 'a' * 40, 'binding': config.binding})
            self.assertEqual(report['status'], 'failed')
            failed = [c['name'] for c in report['checks'] if not c['passed']]
            self.assertEqual(failed, ['critical_planner_function'])


if __name__ == '__main__': unittest.main()
