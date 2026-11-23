"""Project 6 commands for pipeline setup, quality gates, releases, and agent review."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from .core import ROOT, Audit, digest, load_env, redact, write_json
from .pipeline_config import render, validate_definitions
from .pipeline_setup import SetupAWS, apply_setup
from .release_artifacts import run_checks
from .release_aws import ReleaseAWS
from .release_config import ReleaseConfig, object_path, release_id
from .release_verify import browser_available, verify_with_retries
from .releases import ReleaseController


def settings(path=None):
    load_env()
    if not path and os.environ.get('GBM_SETTINGS_JSON'):
        value = ReleaseConfig(json.loads(os.environ['GBM_SETTINGS_JSON']))
        value.validate()
        return value
    return ReleaseConfig.load(path)


def pipeline_context(config):
    if os.environ.get('CODEBUILD_INITIATOR') != 'codepipeline/' + config['project']:
        raise PermissionError('Unattended execution requires the configured CodePipeline initiator')
    commit = release_id(os.environ.get('GBM_SOURCE_COMMIT', ''))
    if commit.startswith('bootstrap-'):
        raise ValueError('A source release must use its actual commit hash')
    resolved = os.environ.get('CODEBUILD_RESOLVED_SOURCE_VERSION', '')
    if resolved != commit:
        raise ValueError('CodeBuild source revision does not match the CodePipeline source commit')
    execution = os.environ.get('GBM_EXECUTION_ID', '')
    if not re.fullmatch(r'[A-Za-z0-9-]{1,128}', execution):
        raise ValueError('Missing or invalid CodePipeline execution ID')
    build_id = os.environ.get('CODEBUILD_BUILD_ID', '').split(':')[-1]
    if not re.fullmatch(r'[A-Za-z0-9-]{1,128}', build_id):
        raise ValueError('Missing CodeBuild build ID')
    return commit, execution, build_id


def approve(plan):
    print(json.dumps(redact(plan), indent=2))
    if not sys.stdin.isatty(): raise PermissionError('Run this change in an interactive terminal to review and approve it')
    phrase = 'approve ' + digest(plan)[:12]
    if input(f"Type '{phrase}' to apply this exact change: ").strip() != phrase:
        raise PermissionError('Change was not approved')


def publish_evidence(config, aws, stage):
    commit, execution, build = pipeline_context(config)
    prefix = f'evidence/{execution}/{build}'
    output = ROOT / 'out'
    files = []
    for path in sorted(output.rglob('*')) if output.exists() else []:
        if path.is_symlink(): raise ValueError('Evidence cannot contain symlinks')
        if not path.is_file() or 'release' in path.relative_to(output).parts or 'unversioned' in path.relative_to(output).parts:
            continue
        if path.suffix not in ('.json', '.jsonl', '.txt', '.xml', '.png'): continue
        name = object_path(path.relative_to(output).as_posix())
        data = path.read_bytes() if path.suffix == '.png' else redact(path.read_text()).encode()
        aws.put(config['artifact_bucket'], prefix + '/' + name, data,
                content_type='image/png' if path.suffix == '.png' else 'application/json')
        files.append(name)
    run_file = output / 'run.json'
    if run_file.exists():
        audit_path = Path(json.loads(run_file.read_text())['audit_path']).resolve()
        if not audit_path.is_relative_to((ROOT / 'runtime/runs').resolve()):
            raise ValueError('Audit evidence path is outside the run directory')
        for path in audit_path.glob('*'):
            if path.is_file() and not path.is_symlink() and path.suffix in ('.json', '.jsonl'):
                aws.put(config['artifact_bucket'], prefix + '/audit/' + path.name, redact(path.read_text()).encode())
                files.append('audit/' + path.name)
    index = {'schema_version': 1, 'source_commit': commit, 'execution_id': execution,
             'build_id': os.environ['CODEBUILD_BUILD_ID'], 'stage': stage, 'prefix': prefix, 'files': files}
    aws.put_json(config['artifact_bucket'], f'evidence/{execution}/index-{stage}.json', index)
    return {'status': 'saved', **index}


def main():
    parser = argparse.ArgumentParser(description='GBM Cloud Project 6 CI/CD and release control')
    parser.add_argument('--config', type=Path, help='Public CI/CD configuration JSON')
    parser.add_argument('--profile', help='Local AWS profile; omitted in CodeBuild to use its service role')
    parser.add_argument('--env-file', type=Path, help='Optional local environment file; never committed')
    commands = parser.add_subparsers(dest='command', required=True)
    plan = commands.add_parser('pipeline-plan', help='Generate and validate actual AWS configuration and scoped service-role policies')
    plan.add_argument('--output', type=Path, default=ROOT / 'runtime/project6/pipeline')
    apply = commands.add_parser('pipeline-apply', help='Review and approve administrator setup once')
    apply.add_argument('--plan', type=Path, required=True)
    apply.add_argument('--infrastructure-only', action='store_true', help='Create private artifacts, roles and build projects before connecting GitHub')
    validate = commands.add_parser('validate', help='Run mandatory tests and package the exact candidate release')
    validate.add_argument('--commit')
    validate.add_argument('--pipeline', action='store_true')
    deploy = commands.add_parser('release', help='Activate a tested release, verify it, and roll back on failure')
    deploy.add_argument('--bundle', type=Path, required=True)
    deploy.add_argument('--pipeline', action='store_true')
    adopt = commands.add_parser('adopt', help='Approve one-time migration of the existing site into a versioned known-good release')
    adopt.add_argument('--project5-state', type=Path, required=True)
    commands.add_parser('status', help='Read pipeline state and the current known-good release')
    commands.add_parser('verify', help='Run actual browser, transport, content, and security checks on the current release')
    commands.add_parser('recover', help='Review and approve recovery of an interrupted release')
    evidence = commands.add_parser('publish-evidence', help='Persist build results even when the pipeline fails')
    evidence.add_argument('--stage', choices=('validate', 'release'), required=True)
    agents = commands.add_parser('agents', help='Run real specialized Project 6 agents with structured handoffs')
    agents.add_argument('--mode', choices=('engineer', 'verify', 'diagnose', 'recover'), default='engineer')
    agents.add_argument('--execution', help='Actual CodePipeline execution ID to analyze')
    agents.add_argument('--interactive-approvals', action='store_true')
    export = commands.add_parser('evidence', help='Export actual AWS execution, build logs, reports, and linked real agent runs')
    export.add_argument('--execution', required=True)
    export.add_argument('--agent-run', action='append', default=[])
    export.add_argument('--output', type=Path)
    args = parser.parse_args()
    audit = None
    try:
        if args.env_file: load_env(args.env_file)
        config = settings(args.config)
        audit = Audit('project6-' + args.command)
        if args.command in ('validate', 'release'):
            if (ROOT / 'out').is_symlink(): raise ValueError('Generated output cannot be a symlink')
            write_json(ROOT / 'out/run.json', {'audit_path': str(audit.path)})
        aws = ReleaseAWS(config, audit, profile=args.profile or os.environ.get('AWS_PROFILE'))
        controller = ReleaseController(config, aws, audit)
        if args.command == 'pipeline-plan':
            result = render(config, args.output)
        elif args.command == 'pipeline-apply':
            plan = json.loads(args.plan.read_text())
            validate_definitions(plan, config)
            approve({'scope': 'infrastructure_only' if args.infrastructure_only else 'full_pipeline', 'plan': plan})
            audit.emit('human', 'approval_granted', {'change': 'pipeline_setup', 'digest': digest(plan)})
            result = apply_setup(config, plan, SetupAWS(config, audit, args.profile or os.environ.get('AWS_PROFILE')),
                                 infrastructure_only=args.infrastructure_only)
        elif args.command == 'validate':
            commit = pipeline_context(config)[0] if args.pipeline else args.commit
            if not commit:
                commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
            release_id(commit)
            output = ROOT / 'out'
            for name in ('release', 'unversioned'):
                path = output / name
                if path.is_symlink(): raise ValueError('Generated output cannot be a symlink')
                if path.exists(): shutil.rmtree(path)
            result = run_checks(config, commit, output)
            audit.artifact('validation-report.json', result)
            write_json(output / 'run.json', {'audit_path': str(audit.path)})
        elif args.command == 'release':
            manifest = json.loads((args.bundle / 'manifest.json').read_text())
            commit = manifest['release_id']
            approved_state_digest = None
            if args.pipeline:
                expected, execution, build = pipeline_context(config)
                if expected != commit: raise ValueError('Release artifact is for another source commit')
                attempt = execution + '/' + build
            else:
                attempt = 'manual-' + uuid.uuid4().hex
                approved_state = controller.read_state()
                approve({'binding': config.binding, 'release_id': commit, 'manifest_digest': manifest['manifest_digest'], 'current_state': approved_state})
                approved_state_digest = digest(approved_state)
            browser_available()
            aws.mutable = True
            write_json(ROOT / 'out/run.json', {'audit_path': str(audit.path)})
            result = controller.deploy(args.bundle, commit, attempt, expected_state_digest=approved_state_digest)
            write_json(ROOT / 'out/release-result.json', result)
        elif args.command == 'adopt':
            legacy = json.loads(args.project5_state.read_text())
            if legacy.get('binding') != config['ownership_binding'] or legacy.get('failure_active'):
                raise ValueError('Project 5 must be restored and match the original ownership binding')
            if legacy.get('distribution_id') != config['distribution_id']:
                raise ValueError('Project 5 state identifies another distribution')
            browser_available()
            approve({'binding': config.binding, 'change': 'Adopt the existing site as the initial verified release',
                     'legacy_manifest': legacy['published_manifest'], 'distribution': controller.owned_distribution()})
            aws.mutable = True
            result = controller.adopt(legacy['published_manifest'])
        elif args.command == 'recover':
            state = controller.read_state()
            if not state or not state.get('inflight'): raise ValueError('No interrupted release needs recovery')
            approve({'change': 'Restore only the recorded previous release', 'state': state})
            browser_available()
            aws.mutable = True
            result = controller.recover('recovery-' + uuid.uuid4().hex, expected_state_digest=digest(state))
        elif args.command == 'status':
            result = {'status': 'inspected', 'release_state': controller.read_state(),
                      'pipeline': aws.call('codepipeline', 'get-pipeline-state', {'name': config['project']})}
        elif args.command == 'verify':
            state = controller.read_state()
            if not state or not state.get('current'): raise ValueError('No known-good release has been adopted')
            result = verify_with_retries(config, controller.stored_manifest(state['current']), aws, audit)
        elif args.command == 'publish-evidence':
            aws.mutable = True
            result = publish_evidence(config, aws, args.stage)
        elif args.command == 'evidence':
            from .release_evidence import export_execution
            from .release_agents import execution_id
            identifier = execution_id(args.execution)
            result = export_execution(config, aws, identifier,
                args.output or ROOT / 'submission' / f'project6-{identifier}.zip', args.agent_run)
        else:
            from .release_agents import run_agents
            result = run_agents(config, aws, audit, args.mode, args.execution, args.interactive_approvals)
        audit.emit('system', 'run_completed', {'command': args.command, 'result': result})
        print(json.dumps(redact(result), indent=2))
        print(f'Execution records: {audit.path}')
        if result.get('status') in ('failed', 'rolled_back', 'rollback_failed', 'blocked'): raise SystemExit(1)
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as exc:
        message = redact(str(exc))
        if audit: audit.emit('system', 'run_failed', {'error': message})
        print('Error: ' + message, file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
