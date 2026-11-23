import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from cloudcrew import site
from cloudcrew.core import Audit, digest, write_json
from cloudcrew.release_artifacts import package_site, validate_bundle, validate_manifest
from cloudcrew.release_aws import ReleaseAwsError
from cloudcrew.release_config import ReleaseConfig
from cloudcrew.releases import ReleaseController, STATE_KEY, configuration_guard, default_origin, initial_layout


def config_fixture():
    value = json.loads((Path(__file__).resolve().parents[1] / 'config/cicd.production.json').read_text())
    value.update(account_id='123456789012', domain='release.example.test', site_bucket='unit-fixture-site',
                 artifact_bucket='unit-fixture-artifacts', distribution_id='UNITTEST', ownership_binding='c' * 64,
                 verify_attempts=1, verify_delay_seconds=0, cloudfront_timeout_seconds=30)
    config = ReleaseConfig(value)
    config.validate()
    return config


def base_distribution(config):
    return {'Enabled': True, 'DefaultRootObject': 'index.html', 'Aliases': {'Quantity': 1, 'Items': [config['domain']]},
            'Origins': {'Quantity': 1, 'Items': [{'Id': 'private-s3', 'DomainName': f"{config['site_bucket']}.s3.{config['region']}.amazonaws.com",
                         'OriginPath': '', 'OriginAccessControlId': 'UNITOAC', 'S3OriginConfig': {'OriginAccessIdentity': ''}}]},
            'DefaultCacheBehavior': {'TargetOriginId': 'private-s3', 'ViewerProtocolPolicy': 'redirect-to-https',
                                     'CachePolicyId': 'managed-cache'},
            'CacheBehaviors': {'Quantity': 0}, 'ViewerCertificate': {'MinimumProtocolVersion': 'TLSv1.2_2021'},
            'CustomErrorResponses': {'Quantity': 0}}


class MemoryAWS:
    """Isolated unit fixture. It never runs AWS CLI or creates execution evidence."""
    def __init__(self, config):
        self.config = config
        self.objects, self.etags, self.writes = {}, {}, []
        self.counter = 0
        self.cf = base_distribution(config)
        self.cf_etag = 'original'
        self.reject_cas = False
        self.fail_upload_key = None

    def get(self, bucket, key):
        if (bucket, key) not in self.objects: raise ReleaseAwsError('get-object', 'NoSuchKey')
        return self.objects[bucket, key], self.etags[bucket, key]

    def get_json(self, bucket, key, optional=False):
        try:
            data, etag = self.get(bucket, key)
            return json.loads(data), etag
        except ReleaseAwsError:
            if optional: return None, None
            raise

    def put(self, bucket, key, body, immutable=False, etag=None, **kwargs):
        if key == self.fail_upload_key: raise RuntimeError('Unit fixture partial upload failure')
        if immutable and (bucket, key) in self.objects: raise ReleaseAwsError('put-object', 'PreconditionFailed')
        if etag and (self.reject_cas or self.etags.get((bucket, key)) != etag): raise ReleaseAwsError('put-object', 'PreconditionFailed')
        self.counter += 1
        self.objects[bucket, key] = body
        self.etags[bucket, key] = str(self.counter)
        self.writes.append(('put', bucket, key))
        return {'ETag': str(self.counter)}

    def put_json(self, bucket, key, value, **kwargs):
        return self.put(bucket, key, json.dumps(value, sort_keys=True).encode(), **kwargs)

    def call(self, service, operation, params):
        if operation == 'get-public-access-block': return {'PublicAccessBlockConfiguration': {k: True for k in ('BlockPublicAcls', 'IgnorePublicAcls', 'BlockPublicPolicy', 'RestrictPublicBuckets')}}
        if operation == 'get-bucket-policy-status': return {'PolicyStatus': {'IsPublic': False}}
        if operation == 'get-bucket-tagging':
            tags = {'CloudCrewBinding': self.config['ownership_binding']} if params['Bucket'] == self.config['site_bucket'] else {'GBMReleaseBinding': self.config.binding}
            return {'TagSet': [{'Key': k, 'Value': v} for k, v in tags.items()]}
        if operation == 'list-tags-for-resource': return {'Tags': {'Items': [{'Key': 'CloudCrewBinding', 'Value': self.config['ownership_binding']}]}}
        if operation == 'get-distribution-config': return {'DistributionConfig': copy.deepcopy(self.cf), 'ETag': self.cf_etag}
        if operation == 'get-distribution': return {'Distribution': {'Status': 'Deployed', 'DistributionConfig': copy.deepcopy(self.cf)}}
        if operation == 'get-invalidation': return {'Invalidation': {'Status': 'Completed'}}
        if operation == 'update-distribution':
            if params['IfMatch'] != self.cf_etag: raise RuntimeError('Stale CloudFront ETag')
            self.cf = copy.deepcopy(params['DistributionConfig'])
            self.cf_etag += '-next'
            self.writes.append(('cloudfront', operation, default_origin(self.cf)['OriginPath']))
            return {}
        if operation == 'create-invalidation': return {'Invalidation': {'Id': 'UNITINVALIDATION'}}
        raise AssertionError('Unexpected fixture operation: ' + operation)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = config_fixture()
        self.audit = Audit('unit-fixture-only', self.root / 'audit')
        self.aws = MemoryAWS(self.config)
        self.old, self.new = 'a' * 40, 'b' * 40
        source = self.root / 'source'
        site.build(source)
        self.legacy = {k: v['sha256'] for k, v in __import__('cloudcrew.release_artifacts', fromlist=['file_manifest']).file_manifest(source).items()}
        self.previous = self.root / 'previous'
        self.candidate = self.root / 'candidate'
        self.old_manifest = package_site(source, self.previous, self.old, self.config)
        self.new_manifest = package_site(source, self.candidate, self.new, self.config)
        self.verifications = []
        self.fail_candidate = False
        self.fail_rollback = False
        def verify(config, manifest, aws, audit):
            version = manifest['release_id']
            self.verifications.append(version)
            failed = self.fail_candidate and version == self.new or self.fail_rollback and version == self.old
            return {'status': 'failed' if failed else 'passed', 'release_id': version, 'checks': [{'name': 'fixture', 'passed': not failed}]}
        self.controller = ReleaseController(self.config, self.aws, self.audit, verifier=verify,
                                            legacy_verifier=lambda *args: {'status': 'passed'}, sleep=lambda _: None)
        self.controller.upload(self.previous, self.old_manifest)
        self.aws.cf = initial_layout(base_distribution(self.config), self.old)
        self.initial_state = {'schema_version': 1, 'binding': self.config.binding,
                              'current': {'release_id': self.old, 'manifest_digest': self.old_manifest['manifest_digest']},
                              'previous': None, 'inflight': None}
        self.aws.put_json(self.config['artifact_bucket'], STATE_KEY, self.initial_state)
        self.aws.writes.clear()

    def state(self): return self.aws.get_json(self.config['artifact_bucket'], STATE_KEY)[0]

    def test_release_promotes_only_after_real_verification_and_keeps_previous(self):
        original_verifier = self.controller.verifier
        def verify(*args):
            self.assertEqual(self.state()['current']['release_id'], self.old)
            self.assertIsNotNone(self.state()['inflight'])
            return original_verifier(*args)
        self.controller.verifier = verify
        result = self.controller.deploy(self.candidate, self.new, 'unit-execution')
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(self.state()['current']['release_id'], self.new)
        self.assertEqual(self.state()['previous']['release_id'], self.old)
        self.assertIsNone(self.state()['inflight'])
        self.assertEqual(default_origin(self.aws.cf)['OriginPath'], '/releases/' + self.new)
        self.assertEqual(self.aws.cf['ViewerCertificate'], base_distribution(self.config)['ViewerCertificate'])

    def test_failed_production_checks_restore_complete_previous_release_and_fail_candidate(self):
        self.fail_candidate = True
        result = self.controller.deploy(self.candidate, self.new, 'unit-execution')
        self.assertEqual(result['status'], 'rolled_back')
        self.assertTrue(result['website_restored'])
        self.assertEqual(self.verifications, [self.new, self.old])
        self.assertEqual(self.state()['current']['release_id'], self.old)
        self.assertEqual(default_origin(self.aws.cf)['OriginPath'], '/releases/' + self.old)
        self.assertIsNone(self.state()['inflight'])

    def test_partial_upload_never_promotes_candidate_and_restores_previous(self):
        self.aws.fail_upload_key = 'releases/' + self.new + '/index.html'
        result = self.controller.deploy(self.candidate, self.new, 'unit-execution')
        self.assertEqual(result['status'], 'rolled_back')
        self.assertEqual(self.state()['current']['release_id'], self.old)
        self.assertNotIn(self.new, self.verifications)

    def test_changed_artifact_is_blocked_before_any_write(self):
        (self.candidate / 'site/index.html').write_text('broken content')
        with self.assertRaisesRegex(ValueError, 'Untested or changed'):
            self.controller.deploy(self.candidate, self.new, 'unit-execution')
        self.assertFalse(self.aws.writes)

    def test_concurrent_or_interrupted_release_blocks_new_deployment(self):
        state = self.state(); state['inflight'] = {'execution_id': 'another-run'}
        self.aws.put_json(self.config['artifact_bucket'], STATE_KEY, state); self.aws.writes.clear()
        with self.assertRaisesRegex(ValueError, 'interrupted release'):
            self.controller.deploy(self.candidate, self.new, 'unit-execution')
        self.assertFalse(self.aws.writes)

    def test_state_cas_conflict_stops_before_cloudfront_mutation(self):
        self.aws.reject_cas = True
        with self.assertRaises(ReleaseAwsError): self.controller.deploy(self.candidate, self.new, 'unit-execution')
        self.assertFalse(any(w[0] == 'cloudfront' for w in self.aws.writes))

    def test_corrupt_known_good_is_not_used_as_a_rollback_target(self):
        self.aws.objects[self.config['site_bucket'], f'releases/{self.old}/index.html'] = b'corrupt'
        with self.assertRaisesRegex(ValueError, 'archive is incomplete or corrupt'):
            self.controller.deploy(self.candidate, self.new, 'unit-execution')
        self.assertFalse(self.aws.writes)

    def test_unrelated_cloudfront_drift_is_not_overwritten_by_rollback(self):
        def verify(config, manifest, aws, audit):
            aws.cf['ViewerCertificate']['MinimumProtocolVersion'] = 'external-change'
            return {'status': 'failed'}
        self.controller.verifier = verify
        result = self.controller.deploy(self.candidate, self.new, 'unit-execution')
        self.assertEqual(result['status'], 'rollback_failed')
        self.assertEqual(self.aws.cf['ViewerCertificate']['MinimumProtocolVersion'], 'external-change')
        self.assertIsNotNone(self.state()['inflight'])

    def test_failed_rollback_verification_keeps_recovery_state(self):
        self.fail_candidate = self.fail_rollback = True
        result = self.controller.deploy(self.candidate, self.new, 'unit-execution')
        self.assertEqual(result['status'], 'rollback_failed')
        self.assertFalse(result['website_restored'])
        self.assertIsNotNone(self.state()['inflight'])

    def test_interrupted_activation_recovers_from_recorded_state(self):
        state = self.state()
        state['inflight'] = {'execution_id': 'interrupted', 'candidate': self.new,
                             'previous': state['current'], 'guard': configuration_guard(self.aws.cf)}
        self.aws.put_json(self.config['artifact_bucket'], STATE_KEY, state)
        default_origin(self.aws.cf)['OriginPath'] = '/releases/' + self.new
        result = self.controller.recover('unit-recovery')
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(default_origin(self.aws.cf)['OriginPath'], '/releases/' + self.old)
        self.assertIsNone(self.state()['inflight'])

    def test_manual_approval_cannot_apply_to_a_changed_release_state(self):
        with self.assertRaisesRegex(ValueError, 'approval is stale'):
            self.controller.deploy(self.candidate, self.new, 'unit-execution', expected_state_digest=digest({'old': 'state'}))
        self.assertFalse(self.aws.writes)

    def test_immutable_release_content_cannot_be_replaced(self):
        self.aws.objects[self.config['site_bucket'], f'releases/{self.new}/index.html'] = b'different content'
        self.aws.etags[self.config['site_bucket'], f'releases/{self.new}/index.html'] = 'foreign'
        result = self.controller.deploy(self.candidate, self.new, 'unit-execution')
        self.assertEqual(result['status'], 'rolled_back')
        self.assertIn('different content', result['error'])

    def test_idempotent_same_release_still_requires_live_verification(self):
        result = self.controller.deploy(self.previous, self.old, 'unit-retry')
        self.assertTrue(result['idempotent'])
        self.assertEqual(self.verifications, [self.old])
        self.assertFalse(self.aws.writes)

    def test_release_manifest_cannot_point_to_another_environment(self):
        changed = copy.deepcopy(self.new_manifest); changed['binding'] = 'wrong'
        with self.assertRaisesRegex(ValueError, 'another deployment'):
            validate_manifest(changed, self.config)

    def test_symlinks_cannot_enter_a_release(self):
        (self.candidate / 'site/extra.js').symlink_to(self.root / 'outside.js')
        report = validate_bundle(self.candidate, self.config, self.new)
        self.assertEqual(report['status'], 'failed')
        self.assertIn('symlinks', str(report['errors']))

    def test_initial_adoption_preserves_a_verified_copy_of_existing_files(self):
        self.aws.objects.pop((self.config['artifact_bucket'], STATE_KEY))
        self.aws.cf = base_distribution(self.config)
        for path in (self.root / 'source').rglob('*'):
            if path.is_file(): self.aws.put(self.config['site_bucket'], path.relative_to(self.root / 'source').as_posix(), path.read_bytes())
        result = self.controller.adopt(self.legacy)
        self.assertEqual(result['status'], 'passed')
        self.assertTrue(result['release_id'].startswith('bootstrap-'))
        self.assertEqual(self.state()['current']['release_id'], result['release_id'])
        self.assertIn((self.config['site_bucket'], 'index.html'), self.aws.objects)

    def test_failed_initial_adoption_restores_original_distribution_and_can_be_retried(self):
        self.aws.objects.pop((self.config['artifact_bucket'], STATE_KEY))
        self.aws.cf = base_distribution(self.config)
        for path in (self.root / 'source').rglob('*'):
            if path.is_file(): self.aws.put(self.config['site_bucket'], path.relative_to(self.root / 'source').as_posix(), path.read_bytes())
        self.controller.verifier = lambda *args: {'status': 'failed'}
        with self.assertRaisesRegex(ValueError, 'baseline failed'):
            self.controller.adopt(self.legacy)
        self.assertEqual(self.aws.cf, base_distribution(self.config))
        self.assertIsNone(self.state()['inflight'])
        self.assertTrue(self.state()['adoption_restored'])
        self.controller.verifier = lambda *args: {'status': 'passed'}
        self.assertEqual(self.controller.adopt(self.legacy)['status'], 'passed')


if __name__ == '__main__': unittest.main()
