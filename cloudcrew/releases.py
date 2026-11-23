"""Publish immutable releases and compensate a failed activation with the saved release."""
from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import time
import uuid
from pathlib import Path

from .core import digest, now, write_json
from .release_artifacts import package_site, validate_bundle, validate_manifest
from .release_config import object_path, release_id
from .release_aws import ReleaseAwsError
from .release_verify import verify_with_retries, verify_legacy


STATE_KEY = 'control/production.json'
ASSET_ORIGIN = 'gbm-release-assets'


def default_origin(config):
    target = config['DefaultCacheBehavior']['TargetOriginId']
    return next(o for o in config['Origins']['Items'] if o['Id'] == target)


def configuration_guard(config):
    value = copy.deepcopy(config)
    default_origin(value)['OriginPath'] = '<release>'
    return digest(value)


def initial_layout(config, version):
    value = copy.deepcopy(config)
    origin = default_origin(value)
    if value['DefaultRootObject'] != 'index.html' or not origin.get('OriginAccessControlId'):
        raise ValueError('Restore the Project 5 default document and OAC before adopting the site')
    if any(o['Id'] == ASSET_ORIGIN for o in value['Origins']['Items']):
        raise ValueError('The reserved asset origin already exists; inspect the current deployment')
    archive = copy.deepcopy(origin)
    archive.update(Id=ASSET_ORIGIN, OriginPath='')
    value['Origins']['Items'].append(archive)
    value['Origins']['Quantity'] = len(value['Origins']['Items'])
    behaviors = value.setdefault('CacheBehaviors', {'Quantity': 0}).setdefault('Items', [])
    if any(b['PathPattern'].lstrip('/') == 'releases/*' for b in behaviors):
        raise ValueError('A release cache behavior already exists')
    asset_behavior = copy.deepcopy(value['DefaultCacheBehavior'])
    asset_behavior.update(PathPattern='releases/*', TargetOriginId=ASSET_ORIGIN)
    behaviors.insert(0, asset_behavior)
    value['CacheBehaviors']['Quantity'] = len(behaviors)
    origin['OriginPath'] = '/releases/' + release_id(version)
    return value


class ReleaseController:
    def __init__(self, config, aws, audit, *, verifier=verify_with_retries, legacy_verifier=verify_legacy, sleep=time.sleep):
        self.config, self.aws, self.audit, self.verifier, self.sleep = config, aws, audit, verifier, sleep
        self.state, self.etag = None, None
        self.legacy_verifier = legacy_verifier

    def owned_distribution(self):
        tags = self.aws.call('cloudfront', 'list-tags-for-resource', {'Resource': self.config.distribution_arn})
        values = {t['Key']: t['Value'] for t in tags['Tags'].get('Items', [])}
        if values.get('CloudCrewBinding') != self.config['ownership_binding']:
            raise PermissionError('Refusing to change a distribution without the original ownership tag')
        result = self.aws.call('cloudfront', 'get-distribution-config', {'Id': self.config['distribution_id']})
        cfg = result['DistributionConfig']
        origin = default_origin(cfg)
        if (origin['DomainName'] != f"{self.config['site_bucket']}.s3.{self.config['region']}.amazonaws.com"
                or self.config['domain'] not in cfg['Aliases'].get('Items', [])
                or cfg['DefaultCacheBehavior']['ViewerProtocolPolicy'] != 'redirect-to-https'):
            raise ValueError('The existing distribution does not match the approved website')
        return result

    def check_buckets(self):
        for bucket in (self.config['site_bucket'], self.config['artifact_bucket']):
            result = self.aws.call('s3api', 'get-public-access-block', {'Bucket': bucket})
            if not all(result['PublicAccessBlockConfiguration'].get(k) is True for k in
                       ('BlockPublicAcls', 'IgnorePublicAcls', 'BlockPublicPolicy', 'RestrictPublicBuckets')):
                raise ValueError('Public access must remain blocked on both buckets')
            if self.aws.call('s3api', 'get-bucket-policy-status', {'Bucket': bucket})['PolicyStatus']['IsPublic']:
                raise ValueError('A deployment bucket has a public policy')
        result = self.aws.call('s3api', 'get-bucket-tagging', {'Bucket': self.config['site_bucket']})
        tags = {t['Key']: t['Value'] for t in result['TagSet']}
        if tags.get('CloudCrewBinding') != self.config['ownership_binding']:
            raise PermissionError('Website bucket ownership does not match')
        result = self.aws.call('s3api', 'get-bucket-tagging', {'Bucket': self.config['artifact_bucket']})
        if {t['Key']: t['Value'] for t in result['TagSet']}.get('GBMReleaseBinding') != self.config.binding:
            raise PermissionError('Artifact bucket ownership does not match')

    def read_state(self):
        self.state, self.etag = self.aws.get_json(self.config['artifact_bucket'], STATE_KEY, optional=True)
        if self.state and self.state.get('binding') != self.config.binding:
            raise ValueError('Production state belongs to another deployment')
        return self.state

    def save_state(self, value):
        result = self.aws.put_json(self.config['artifact_bucket'], STATE_KEY, value,
                                  immutable=self.etag is None, etag=self.etag)
        self.state, self.etag = value, result['ETag']

    def evidence(self, attempt, name, value):
        object_path(attempt)
        self.audit.artifact(name, value)
        self.aws.put_json(self.config['artifact_bucket'], f'evidence/{attempt}/{name}', value)

    def upload(self, bundle, manifest):
        version = validate_manifest(manifest, self.config)
        for name, info in manifest['files'].items():
            data = (bundle / 'site' / name).read_bytes()
            if hashlib.sha256(data).hexdigest() != info['sha256']:
                raise ValueError('Release changed after validation')
            key = f'releases/{version}/{name}'
            try:
                self.aws.put(self.config['site_bucket'], key, data, content_type=info['content_type'], immutable=True,
                             cache_control='public,max-age=31536000,immutable' if name.startswith('assets/') else 'no-cache,max-age=0,must-revalidate')
            except ReleaseAwsError as exc:
                if not exc.conflict: raise
                existing, _ = self.aws.get(self.config['site_bucket'], key)
                if existing != data: raise ValueError('Immutable release ID already has different content') from exc
        key = f'releases/{version}/manifest.json'
        try:
            self.aws.put_json(self.config['artifact_bucket'], key, manifest, immutable=True)
        except ReleaseAwsError as exc:
            if not exc.conflict: raise
            existing, _ = self.aws.get_json(self.config['artifact_bucket'], key)
            if existing != manifest: raise ValueError('Immutable release manifest already differs') from exc

    def stored_manifest(self, reference, *, verify_files=True):
        version = release_id(reference['release_id'])
        manifest, _ = self.aws.get_json(self.config['artifact_bucket'], f'releases/{version}/manifest.json')
        validate_manifest(manifest, self.config, version)
        if manifest['manifest_digest'] != reference['manifest_digest']:
            raise ValueError('Known-good manifest changed')
        if verify_files:
            for name, info in manifest['files'].items():
                data, _ = self.aws.get(self.config['site_bucket'], f'releases/{version}/{name}')
                if hashlib.sha256(data).hexdigest() != info['sha256']:
                    raise ValueError('Known-good release archive is incomplete or corrupt')
        return manifest

    def wait(self, invalidation=None):
        end = time.monotonic() + self.config['cloudfront_timeout_seconds']
        while time.monotonic() < end:
            dist = self.aws.call('cloudfront', 'get-distribution', {'Id': self.config['distribution_id']})
            status = 'Completed' if not invalidation else self.aws.call('cloudfront', 'get-invalidation',
                {'DistributionId': self.config['distribution_id'], 'Id': invalidation})['Invalidation']['Status']
            if dist['Distribution']['Status'] == 'Deployed' and status == 'Completed': return
            self.sleep(10)
        raise TimeoutError('CloudFront readiness timed out')

    def refresh(self, attempt):
        result = self.aws.call('cloudfront', 'create-invalidation', {
            'DistributionId': self.config['distribution_id'],
            'InvalidationBatch': {'Paths': {'Quantity': 1, 'Items': ['/*']}, 'CallerReference': attempt}})
        self.wait(result['Invalidation']['Id'])

    def activate(self, version, *, guard, allowed_paths):
        self.wait()
        current = self.owned_distribution()
        cfg = current['DistributionConfig']
        if configuration_guard(cfg) != guard or default_origin(cfg).get('OriginPath', '') not in allowed_paths:
            raise ValueError('CloudFront changed outside this release; refusing to overwrite it')
        next_config = copy.deepcopy(cfg)
        default_origin(next_config)['OriginPath'] = '/releases/' + release_id(version)
        self.aws.call('cloudfront', 'update-distribution', {'Id': self.config['distribution_id'], 'IfMatch': current['ETag'], 'DistributionConfig': next_config})

    def deploy(self, bundle: Path, commit: str, attempt: str, expected_state_digest=None):
        release_id(commit)
        object_path(attempt)
        checked = validate_bundle(bundle, self.config, commit)
        if checked['status'] != 'passed': raise ValueError('Untested or changed release artifact: ' + '; '.join(checked['errors']))
        manifest = json.loads((bundle / 'manifest.json').read_text())
        self.check_buckets()
        state = self.read_state()
        if expected_state_digest and digest(state) != expected_state_digest:
            raise ValueError('Release approval is stale')
        if not state or not state.get('current'):
            raise ValueError('Adopt and verify the existing website before enabling pipeline deployment')
        if state.get('inflight'):
            raise ValueError('An interrupted release needs recovery; concurrent activation is blocked')
        previous = state['current']
        if previous['release_id'] == commit:
            report = self.verifier(self.config, manifest, self.aws, self.audit)
            return {'status': report['status'], 'release_id': commit, 'idempotent': True, 'verification': report}
        old_manifest = self.stored_manifest(previous)
        dist = self.owned_distribution()['DistributionConfig']
        old_path = '/releases/' + previous['release_id']
        if default_origin(dist).get('OriginPath') != old_path:
            raise ValueError('Live release disagrees with the recorded known-good release')
        guard = configuration_guard(dist)
        pending = {**state, 'inflight': {'execution_id': attempt, 'candidate': commit, 'previous': previous, 'guard': guard}, 'updated_at': now()}
        self.save_state(pending)
        report = {'status': 'failed', 'release_id': commit, 'execution_id': attempt, 'previous_release': previous['release_id'], 'started_at': now()}
        try:
            self.audit.emit('release', 'phase_started', {'phase': 'Deployment', 'release_id': commit})
            self.upload(bundle, manifest)
            self.activate(commit, guard=guard, allowed_paths={old_path, '/releases/' + commit})
            self.audit.emit('release', 'phase_started', {'phase': 'ContentDelivery', 'release_id': commit})
            self.refresh(attempt + '-deploy')
            self.audit.emit('release', 'phase_started', {'phase': 'ProductionVerification', 'release_id': commit})
            verified = self.verifier(self.config, manifest, self.aws, self.audit)
            self.evidence(attempt, 'production-verification.json', verified)
            if verified['status'] != 'passed': raise ValueError('Production verification rejected this release')
            report.update(status='passed', verified_at=now(), verification=verified)
            self.evidence(attempt, 'release-result.json', report)
            promoted = {**pending, 'current': {'release_id': commit, 'manifest_digest': manifest['manifest_digest']},
                        'previous': previous, 'inflight': None, 'last_execution': attempt, 'updated_at': now()}
            try:
                self.save_state(promoted)
            except Exception:
                # A timed-out conditional write may already have committed.
                actual = self.read_state()
                if not actual or actual.get('last_execution') != attempt or actual.get('current') != promoted['current'] or actual.get('inflight'):
                    raise
            return report
        except Exception as exc:
            report['error'] = str(exc)
            self.audit.emit('recovery_coordinator', 'phase_started', {'phase': 'FailureHandling', 'error': str(exc)})
            try:
                current = self.read_state()
                if not current or (current.get('inflight') or {}).get('execution_id') != attempt:
                    raise ValueError('Deployment ownership changed; automatic recovery will not overwrite it')
                self.stored_manifest(previous)
                self.activate(previous['release_id'], guard=guard, allowed_paths={old_path, '/releases/' + commit})
                self.refresh(attempt + '-rollback')
                restored = self.verifier(self.config, old_manifest, self.aws, self.audit)
                self.evidence(attempt, 'rollback-verification.json', restored)
                if restored['status'] != 'passed': raise ValueError('Rollback verification did not pass')
                self.save_state({**state, 'inflight': None, 'last_failed_execution': attempt, 'updated_at': now()})
                report.update(status='rolled_back', website_restored=True, rollback=restored)
            except Exception as recovery_error:
                report.update(status='rollback_failed', website_restored=False, recovery_error=str(recovery_error))
            self.evidence(attempt, 'release-result.json', report)
            return report

    def recover(self, attempt, expected_state_digest=None):
        object_path(attempt)
        self.check_buckets()
        state = self.read_state()
        if expected_state_digest and digest(state) != expected_state_digest:
            raise ValueError('Recovery approval is stale')
        if not state or not state.get('inflight'):
            raise ValueError('No interrupted release is recorded')
        pending = state['inflight']
        previous = pending['previous']
        if previous is None:
            return self.restore_adoption(state, attempt)
        manifest = self.stored_manifest(previous)
        self.activate(previous['release_id'], guard=pending['guard'], allowed_paths={
            '/releases/' + previous['release_id'], '/releases/' + release_id(pending['candidate'])})
        self.refresh(attempt + '-recover')
        verified = self.verifier(self.config, manifest, self.aws, self.audit)
        self.evidence(attempt, 'recovery-verification.json', verified)
        if verified['status'] != 'passed': raise ValueError('Recovered release has not passed production checks')
        self.save_state({**state, 'current': previous, 'inflight': None, 'updated_at': now()})
        return {'status': 'passed', 'restored_release': previous['release_id'], 'verification': verified}

    def restore_adoption(self, state, attempt):
        pending = state['inflight']
        if pending.get('kind') != 'adoption': raise ValueError('Missing recorded adoption recovery information')
        before, _ = self.aws.get_json(self.config['artifact_bucket'], pending['backup_key'])
        self.wait()
        current = self.owned_distribution()
        if (current['DistributionConfig'] != before['DistributionConfig']
                and (configuration_guard(current['DistributionConfig']) != pending['guard']
                     or default_origin(current['DistributionConfig']).get('OriginPath') != '/releases/' + pending['candidate'])):
            raise ValueError('Unrelated CloudFront changes prevent adoption recovery')
        self.aws.call('cloudfront', 'update-distribution', {'Id': self.config['distribution_id'], 'IfMatch': current['ETag'], 'DistributionConfig': before['DistributionConfig']})
        self.refresh(attempt + '-restore-adoption')
        result = self.legacy_verifier(self.config, pending['legacy_manifest'], self.aws)
        self.evidence(attempt, 'adoption-restoration.json', result)
        if result['status'] != 'passed': raise ValueError('Legacy website restoration has not passed verification')
        self.save_state({**state, 'inflight': None, 'current': None, 'adoption_restored': True, 'updated_at': now()})
        return {'status': 'passed', 'legacy_site_restored': True, 'verification': result}

    def adopt(self, legacy_manifest):
        self.check_buckets()
        existing = self.read_state()
        if existing and (existing.get('current') or existing.get('inflight')):
            raise ValueError('Release state already exists or needs recovery')
        self.wait()
        before = self.owned_distribution()
        if default_origin(before['DistributionConfig']).get('OriginPath', ''):
            raise ValueError('Expected the original Project 5 root origin')
        version = 'bootstrap-' + digest({'files': legacy_manifest, 'binding': self.config.binding})[:24]
        attempt = 'adoption-' + uuid.uuid4().hex
        healthy = self.legacy_verifier(self.config, legacy_manifest, self.aws)
        self.evidence(attempt, 'legacy-verification.json', healthy)
        if healthy['status'] != 'passed': raise ValueError('Project 5 must pass real verification before adoption')
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'legacy'
            source.mkdir()
            for name, expected_hash in legacy_manifest.items():
                object_path(name)
                data, _ = self.aws.get(self.config['site_bucket'], name)
                if hashlib.sha256(data).hexdigest() != expected_hash:
                    raise ValueError('Project 5 files do not match the saved publication manifest')
                path = source / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            bundle = Path(tmp) / 'release'
            manifest = package_site(source, bundle, version, self.config, bootstrap=True)
            after = initial_layout(before['DistributionConfig'], version)
            self.evidence(attempt, 'adoption-backup.json', before)
            state = {'schema_version': 1, 'binding': self.config.binding, 'current': None, 'previous': None,
                     'inflight': {'kind': 'adoption', 'execution_id': attempt, 'candidate': version, 'previous': None,
                                  'backup_key': f'evidence/{attempt}/adoption-backup.json', 'legacy_manifest': legacy_manifest,
                                  'guard': configuration_guard(after)}, 'updated_at': now()}
            self.save_state(state)
            try:
                self.upload(bundle, manifest)
                self.aws.call('cloudfront', 'update-distribution', {'Id': self.config['distribution_id'], 'IfMatch': before['ETag'], 'DistributionConfig': after})
                self.refresh(attempt)
                verified = self.verifier(self.config, manifest, self.aws, self.audit)
                self.evidence(attempt, 'adoption-verification.json', verified)
                if verified['status'] != 'passed': raise ValueError('Adopted baseline failed production verification')
                self.save_state({**state, 'current': {'release_id': version, 'manifest_digest': manifest['manifest_digest']},
                                 'inflight': None, 'updated_at': now()})
                return {'status': 'passed', 'release_id': version, 'verification': verified}
            except Exception:
                self.recover(attempt + '-compensate')
                raise
