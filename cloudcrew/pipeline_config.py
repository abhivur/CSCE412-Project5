"""Generate the actual CodePipeline, CodeBuild, webhook, and service-role requests."""
from __future__ import annotations

import json
import hashlib
from pathlib import Path

from .core import ROOT, digest, write_json


def trust(service, account, source_arn):
    entry = {'Effect': 'Allow', 'Principal': {'Service': service}, 'Action': 'sts:AssumeRole'}
    if service == 'codebuild.amazonaws.com':
        entry['Condition'] = {'StringEquals': {'aws:SourceAccount': account}, 'ArnLike': {'aws:SourceArn': source_arn}}
    return {'Version': '2012-10-17', 'Statement': [entry]}


def statement(actions, resources, **extra):
    return {'Effect': 'Allow', 'Action': actions, 'Resource': resources, **extra}


def role_policy(config, kind):
    bucket = f"arn:aws:s3:::{config['artifact_bucket']}"
    pipeline = config['project']
    statements = [statement(['s3:GetBucketLocation', 's3:GetBucketVersioning', 's3:GetBucketAcl'], [bucket]),
        statement(['s3:GetObject', 's3:GetObjectVersion', 's3:PutObject', 's3:PutObjectAcl'], [bucket + '/' + pipeline + '/*']),
        statement(['kms:Decrypt', 'kms:Encrypt', 'kms:GenerateDataKey'], [f"arn:aws:kms:{config['region']}:{config['account_id']}:key/*"],
                  Condition={'StringEquals': {'kms:ViaService': f"s3.{config['region']}.amazonaws.com", 'kms:CallerAccount': config['account_id']},
                             'StringLike': {'kms:EncryptionContext:aws:s3:arn': bucket + '/*'}})]
    if kind == 'pipeline':
        statements.append(statement(['codebuild:StartBuild', 'codebuild:BatchGetBuilds'],
            [f"arn:aws:codebuild:{config['region']}:{config['account_id']}:project/{pipeline}-{stage}" for stage in ('validate', 'release')]))
        if config['source_provider'] == 'connection':
            statements.append(statement(['codeconnections:UseConnection', 'codestar-connections:UseConnection'], [config['connection_arn']]))
    else:
        statements.append(statement(['s3:PutObject', 's3:GetObject', 's3:PutObjectAcl'], [bucket + '/evidence/*', bucket + '/build-logs/*']))
        if kind == 'release':
            site = f"arn:aws:s3:::{config['site_bucket']}"
            statements += [
                statement(['s3:GetObject', 's3:PutObject'], [site + '/releases/*']),
                statement(['s3:GetObject'], [site + '/*']),
                statement(['s3:GetObject', 's3:PutObject'], [bucket + '/releases/*', bucket + '/control/*']),
                statement(['s3:GetBucketPublicAccessBlock', 's3:GetBucketPolicyStatus', 's3:GetBucketPolicy', 's3:GetBucketTagging', 's3:GetEncryptionConfiguration'], [site, bucket]),
                statement(['cloudfront:GetDistribution', 'cloudfront:GetDistributionConfig', 'cloudfront:ListTagsForResource',
                           'cloudfront:UpdateDistribution', 'cloudfront:CreateInvalidation', 'cloudfront:GetInvalidation'], [config.distribution_arn]),
                statement(['cloudfront:GetOriginAccessControl'], [f"arn:aws:cloudfront::{config['account_id']}:origin-access-control/*"]),
            ]
    return {'Version': '2012-10-17', 'Statement': statements}


def definitions(config):
    name, account, region = config['project'], config['account_id'], config['region']
    tags = [{'key': 'Project', 'value': name}, {'key': 'GBMReleaseBinding', 'value': config.binding}]
    role_arn = lambda kind: f'arn:aws:iam::{account}:role/{name}-{kind}'
    projects, roles = {}, {}
    for kind in ('pipeline', 'validate', 'release'):
        source = f'arn:aws:codepipeline:{region}:{account}:{name}' if kind == 'pipeline' else f'arn:aws:codebuild:{region}:{account}:project/{name}-{kind}'
        roles[kind] = {'name': name + '-' + kind,
                       'trust': trust('codepipeline.amazonaws.com' if kind == 'pipeline' else 'codebuild.amazonaws.com', account, source),
                       'policy': role_policy(config, kind)}
        if kind == 'pipeline': continue
        projects[kind] = {
            'name': name + '-' + kind, 'description': 'GBM Cloud Project 6 ' + kind,
            'source': {'type': 'CODEPIPELINE', 'buildspec': f'ci/buildspec-{kind}.yml'},
            'artifacts': {'type': 'CODEPIPELINE'}, 'serviceRole': role_arn(kind),
            'environment': {'type': 'LINUX_CONTAINER', 'image': config['image'], 'computeType': 'BUILD_GENERAL1_SMALL',
                            'privilegedMode': False, 'imagePullCredentialsType': 'CODEBUILD',
                            'environmentVariables': [{'name': 'GBM_SETTINGS_JSON', 'value': json.dumps(config.values), 'type': 'PLAINTEXT'},
                                                     {'name': 'GBM_STAGE', 'value': kind, 'type': 'PLAINTEXT'}]},
            'timeoutInMinutes': 40, 'queuedTimeoutInMinutes': 60, 'concurrentBuildLimit': 1,
            'logsConfig': {'cloudWatchLogs': {'status': 'DISABLED'}, 's3Logs': {'status': 'ENABLED',
                'location': config['artifact_bucket'] + '/build-logs/' + kind, 'encryptionDisabled': False}},
            'tags': tags,
        }
    source = {'name': 'GitHub', 'namespace': 'SourceVars', 'runOrder': 1, 'outputArtifacts': [{'name': 'SourceArtifact'}]}
    if config['source_provider'] == 'github_oauth':
        source.update(actionTypeId={'category': 'Source', 'owner': 'ThirdParty', 'provider': 'GitHub', 'version': '1'},
                      configuration={'Owner': config['repository_owner'], 'Repo': config['repository_name'], 'Branch': config['branch'],
                                     'OAuthToken': '<GITHUB_SOURCE_TOKEN supplied locally>', 'PollForSourceChanges': 'false'})
    else:
        source.update(actionTypeId={'category': 'Source', 'owner': 'AWS', 'provider': 'CodeStarSourceConnection', 'version': '1'},
                      configuration={'ConnectionArn': config['connection_arn'], 'FullRepositoryId': config['repository_owner'] + '/' + config['repository_name'],
                                     'BranchName': config['branch'], 'DetectChanges': 'true', 'OutputArtifactFormat': 'CODE_ZIP'})
    def build_action(kind, inputs, outputs):
        variables = [{'name': 'GBM_SOURCE_COMMIT', 'value': '#{SourceVars.CommitId}', 'type': 'PLAINTEXT'},
                     {'name': 'GBM_EXECUTION_ID', 'value': '#{codepipeline.PipelineExecutionId}', 'type': 'PLAINTEXT'}]
        return {'name': 'Validate' if kind == 'validate' else 'DeployVerifyRecover',
                'actionTypeId': {'category': 'Build', 'owner': 'AWS', 'provider': 'CodeBuild', 'version': '1'},
                'configuration': {'ProjectName': projects[kind]['name'], 'PrimarySource': 'SourceArtifact',
                                  'EnvironmentVariables': json.dumps(variables)},
                'inputArtifacts': [{'name': n} for n in inputs], 'outputArtifacts': [{'name': n} for n in outputs], 'runOrder': 1}
    pipeline = {'pipeline': {'name': name, 'roleArn': role_arn('pipeline'), 'pipelineType': 'V2', 'executionMode': 'QUEUED',
                'artifactStore': {'type': 'S3', 'location': config['artifact_bucket']},
                'stages': [{'name': 'Source', 'actions': [source]},
                           {'name': 'BuildAndValidate', 'actions': [build_action('validate', ['SourceArtifact'], ['ValidatedArtifact'])]},
                           {'name': 'Release', 'actions': [build_action('release', ['SourceArtifact', 'ValidatedArtifact'], ['ReleaseEvidence'])]}]}, 'tags': tags}
    webhook = None
    if config['source_provider'] == 'github_oauth':
        webhook = {'webhook': {'name': name + '-github', 'targetPipeline': name, 'targetAction': 'GitHub',
            'authentication': 'GITHUB_HMAC',
            'authenticationConfiguration': {'SecretToken': '<unique webhook secret supplied during setup>'},
            'filters': [{'jsonPath': '$.ref', 'matchEquals': 'refs/heads/' + config['branch']}]}}
    result = {'schema_version': 1, 'binding': config.binding, 'config': config.values, 'roles': roles,
              'projects': projects, 'pipeline': pipeline, 'webhook': webhook,
              'logical_steps': ['Source', 'BuildAndValidation', 'Deployment', 'ContentDelivery', 'ProductionVerification', 'FailureHandling']}
    result['buildspec_hashes'] = {f'ci/buildspec-{stage}.yml': hashlib.sha256((ROOT / 'ci' / f'buildspec-{stage}.yml').read_bytes()).hexdigest()
                                 for stage in ('validate', 'release')}
    result['sha256'] = digest(result)
    return result


def validate_definitions(plan, config, root=ROOT):
    if plan != definitions(config): raise ValueError('Pipeline configuration was edited or is stale')
    for stage in ('validate', 'release'):
        path = root / 'ci' / f'buildspec-{stage}.yml'
        spec = json.loads(path.read_text())  # JSON is a YAML subset accepted by CodeBuild.
        commands = spec['phases']['build']['commands']
        expected = 'validate' if stage == 'validate' else 'release'
        if not any(c.startswith('python3 -m cloudcrew.cicd ' + expected + ' ') and c.endswith('--pipeline') for c in commands):
            raise ValueError('Pipeline does not execute the required quality/release command')
        if 'finally' not in spec['phases']['build']:
            raise ValueError('Pipeline must retain evidence even when a command fails')
        if spec['phases']['build'].get('on-failure') != 'ABORT':
            raise ValueError('A mandatory quality or release failure must abort its action')
    return {'status': 'passed', 'steps': plan['logical_steps'], 'execution_mode': 'QUEUED', 'source_branch': config['branch']}


def render(config, output):
    plan = definitions(config)
    validate_definitions(plan, config)
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / 'pipeline-plan.json', plan)
    for kind, project in plan['projects'].items(): write_json(output / f'codebuild-{kind}.json', project)
    for kind, role in plan['roles'].items():
        write_json(output / f'{kind}-trust.json', role['trust'])
        write_json(output / f'{kind}-policy.json', role['policy'])
    write_json(output / 'codepipeline.json', plan['pipeline'])
    if plan['webhook']: write_json(output / 'webhook.json', plan['webhook'])
    return {'status': 'prepared', 'plan': str(output / 'pipeline-plan.json'), 'sha256': plan['sha256'], 'validation': validate_definitions(plan, config)}
