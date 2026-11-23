"""Administrator setup is explicit, scoped, and separate from the pipeline roles."""
from __future__ import annotations

import copy
import json
import os
import secrets
import subprocess
import time

from .core import redact
from .aws_input import cli_input
from .pipeline_config import definitions, validate_definitions
from .release_aws import ReleaseAwsError


ADMIN_OPERATIONS = {
    'sts': {'get-caller-identity'},
    's3api': {'head-bucket', 'get-bucket-tagging', 'create-bucket', 'put-bucket-tagging',
              'put-public-access-block', 'put-bucket-ownership-controls', 'put-bucket-encryption',
              'put-bucket-versioning', 'put-bucket-policy'},
    'iam': {'get-role', 'create-role', 'put-role-policy', 'update-assume-role-policy'},
    'codebuild': {'batch-get-projects', 'create-project', 'update-project'},
    'codepipeline': {'get-pipeline', 'list-tags-for-resource', 'create-pipeline', 'update-pipeline',
                     'put-webhook', 'register-webhook-with-third-party'},
}


class SetupAWS:
    def __init__(self, config, audit, profile=None):
        self.config, self.audit, self.profile = config, audit, profile

    def call(self, service, operation, params):
        if operation not in ADMIN_OPERATIONS.get(service, set()):
            raise PermissionError('Administrator operation is outside the setup allowlist')
        args = ['aws', service, operation, '--region', self.config['region'], '--output', 'json',
                '--no-cli-pager', '--cli-connect-timeout', '10', '--cli-read-timeout', '60']
        if self.profile: args += ['--profile', self.profile]
        self.audit.emit('pipeline_setup', 'aws_request', {'service': service, 'operation': operation, 'parameters': params})
        with cli_input(params) as request:
            result = subprocess.run(args + ['--cli-input-json', request], stdin=subprocess.DEVNULL,
                                    text=True, capture_output=True, timeout=100,
                                    env=dict(os.environ, AWS_PAGER='', AWS_MAX_ATTEMPTS='3', AWS_CLI_AUTO_PROMPT='off'))
        if result.returncode: raise ReleaseAwsError(operation, result.stderr)
        value = json.loads(result.stdout) if result.stdout.strip() else {}
        self.audit.emit('pipeline_setup', 'aws_result', {'service': service, 'operation': operation, 'result': value})
        return value


def missing(exc):
    return any(name in str(exc) for name in ('NoSuchEntity', 'NoSuchBucket', 'Not Found', '(404)', 'PipelineNotFoundException'))


def apply_setup(config, plan, aws, *, sleep=time.sleep, infrastructure_only=False):
    validate_definitions(plan, config)
    token = os.environ.get('GITHUB_SOURCE_TOKEN', '')
    if not infrastructure_only and config['source_provider'] == 'github_oauth' and not token:
        raise ValueError('Set GITHUB_SOURCE_TOKEN locally before applying the pipeline plan; never put it in JSON or chat')
    identity = aws.call('sts', 'get-caller-identity', {})
    if identity['Account'] != config['account_id']:
        raise PermissionError('AWS login is for a different account')
    bucket = config['artifact_bucket']
    bucket_tags = [{'Key': 'Project', 'Value': config['project']}, {'Key': 'GBMReleaseBinding', 'Value': config.binding}]
    try:
        tags = aws.call('s3api', 'get-bucket-tagging', {'Bucket': bucket})
        if {t['Key']: t['Value'] for t in tags['TagSet']}.get('GBMReleaseBinding') != config.binding:
            raise PermissionError('Artifact bucket exists without this deployment ownership tag')
    except ReleaseAwsError as exc:
        if not missing(exc): raise
        request = {'Bucket': bucket}
        if config['region'] != 'us-east-1': request['CreateBucketConfiguration'] = {'LocationConstraint': config['region']}
        aws.call('s3api', 'create-bucket', request)
        aws.call('s3api', 'put-bucket-tagging', {'Bucket': bucket, 'Tagging': {'TagSet': bucket_tags}})
    aws.call('s3api', 'put-public-access-block', {'Bucket': bucket, 'PublicAccessBlockConfiguration': {
        'BlockPublicAcls': True, 'IgnorePublicAcls': True, 'BlockPublicPolicy': True, 'RestrictPublicBuckets': True}})
    aws.call('s3api', 'put-bucket-ownership-controls', {'Bucket': bucket, 'OwnershipControls': {'Rules': [{'ObjectOwnership': 'BucketOwnerEnforced'}]}})
    aws.call('s3api', 'put-bucket-encryption', {'Bucket': bucket, 'ServerSideEncryptionConfiguration': {'Rules': [{'ApplyServerSideEncryptionByDefault': {'SSEAlgorithm': 'AES256'}}]}})
    aws.call('s3api', 'put-bucket-versioning', {'Bucket': bucket, 'VersioningConfiguration': {'Status': 'Enabled'}})
    aws.call('s3api', 'put-bucket-policy', {'Bucket': bucket, 'Policy': json.dumps({'Version': '2012-10-17', 'Statement': [{
        'Effect': 'Deny', 'Principal': '*', 'Action': 's3:*', 'Resource': [f'arn:aws:s3:::{bucket}', f'arn:aws:s3:::{bucket}/*'],
        'Condition': {'Bool': {'aws:SecureTransport': 'false'}}}]})})
    for kind, role in plan['roles'].items():
        try:
            existing = aws.call('iam', 'get-role', {'RoleName': role['name']})
            tags = {t['Key']: t['Value'] for t in existing['Role'].get('Tags', [])}
            if tags.get('GBMReleaseBinding') != config.binding: raise PermissionError('Refusing to update a foreign IAM role')
            aws.call('iam', 'update-assume-role-policy', {'RoleName': role['name'], 'PolicyDocument': json.dumps(role['trust'])})
        except ReleaseAwsError as exc:
            if not missing(exc): raise
            aws.call('iam', 'create-role', {'RoleName': role['name'], 'AssumeRolePolicyDocument': json.dumps(role['trust']), 'Tags': bucket_tags})
        aws.call('iam', 'put-role-policy', {'RoleName': role['name'], 'PolicyName': 'Project6Only', 'PolicyDocument': json.dumps(role['policy'])})
    sleep(10)
    for kind, project in plan['projects'].items():
        existing = aws.call('codebuild', 'batch-get-projects', {'names': [project['name']]})
        if existing['projects']:
            tags = {t['key']: t['value'] for t in existing['projects'][0].get('tags', [])}
            if tags.get('GBMReleaseBinding') != config.binding: raise PermissionError('Refusing to update a foreign build project')
        aws.call('codebuild', 'update-project' if existing['projects'] else 'create-project', project)
    if infrastructure_only:
        return {'status': 'configured', 'scope': 'infrastructure_only', 'artifact_bucket': bucket,
                'next': 'Adopt the known-good website, then apply the full pipeline plan with GitHub authorization.'}
    pipeline = copy.deepcopy(plan['pipeline'])
    if config['source_provider'] == 'github_oauth':
        pipeline['pipeline']['stages'][0]['actions'][0]['configuration']['OAuthToken'] = token
    try:
        existing = aws.call('codepipeline', 'get-pipeline', {'name': config['project']})
        arn = f"arn:aws:codepipeline:{config['region']}:{config['account_id']}:{config['project']}"
        tags = aws.call('codepipeline', 'list-tags-for-resource', {'resourceArn': arn})
        if {t['key']: t['value'] for t in tags['tags']}.get('GBMReleaseBinding') != config.binding:
            raise PermissionError('Refusing to update a foreign pipeline')
        pipeline.pop('tags')
        aws.call('codepipeline', 'update-pipeline', pipeline)
    except ReleaseAwsError as exc:
        if not missing(exc): raise
        aws.call('codepipeline', 'create-pipeline', pipeline)
    if plan['webhook']:
        webhook = copy.deepcopy(plan['webhook'])
        webhook['webhook']['authenticationConfiguration']['SecretToken'] = secrets.token_urlsafe(48)
        aws.call('codepipeline', 'put-webhook', webhook)
        aws.call('codepipeline', 'register-webhook-with-third-party', {'webhookName': webhook['webhook']['name']})
    return {'status': 'configured', 'pipeline': config['project'], 'source': config['repository_owner'] + '/' + config['repository_name'], 'branch': config['branch']}
