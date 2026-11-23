"""Reuse the Project 5 agent loop with tools for pipeline and release evidence."""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

from .agents import Agent, Tool
from .core import ROOT, Config, digest
from .llm import ResponsesModel, object_schema
from .pipeline_config import definitions, render, validate_definitions
from .release_artifacts import run_checks
from .release_verify import browser_available, verify_with_retries
from .releases import ReleaseController


def execution_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9-]{1,128}', value):
        raise ValueError('Use an actual CodePipeline execution ID')
    return value


def read_execution(config, aws, execution):
    execution_id(execution)
    pipeline = aws.call('codepipeline', 'get-pipeline-execution', {'pipelineName': config['project'], 'pipelineExecutionId': execution})
    actions = aws.call('codepipeline', 'list-action-executions', {'pipelineName': config['project'], 'filter': {'pipelineExecutionId': execution}})
    builds = []
    for action in actions.get('actionExecutionDetails', []):
        identifier = action.get('output', {}).get('executionResult', {}).get('externalExecutionId')
        provider = action.get('input', {}).get('actionTypeId', {}).get('provider')
        if provider == 'CodeBuild' and identifier:
            builds.extend(aws.call('codebuild', 'batch-get-builds', {'ids': [identifier]}).get('builds', []))
    evidence = {}
    revisions = {r.get('revisionId') for r in pipeline.get('pipelineExecution', {}).get('artifactRevisions', [])}
    for stage in ('validate', 'release'):
        index, _ = aws.get_json(config['artifact_bucket'], f'evidence/{execution}/index-{stage}.json', optional=True)
        if not index: continue
        if index.get('execution_id') != execution or not index.get('prefix', '').startswith(f'evidence/{execution}/'):
            raise ValueError('Evidence index does not match the requested execution')
        if revisions and index.get('source_commit') not in revisions:
            raise ValueError('Evidence source commit differs from the actual pipeline execution')
        for name in index['files']:
            if name.endswith('.json') and not name.startswith('audit/'):
                item, _ = aws.get_json(config['artifact_bucket'], index['prefix'] + '/' + name)
                evidence[stage + '/' + name] = item
    return {'status': 'inspected', 'execution_id': execution, 'pipeline': pipeline, 'actions': actions, 'builds': builds, 'evidence': evidence}


class ReleaseToolbox:
    cloud = True

    def __init__(self, config, aws, audit, execution=None, interactive=False):
        self.config, self.aws, self.audit = config, aws, audit
        self.execution, self.interactive = execution, interactive
        self.controller = ReleaseController(config, aws, audit)
        self.recovery_plan = None

    def inspect(self):
        return {'status': 'inspected', 'config': self.config.values, 'roles': [
            'pipeline_engineer', 'test_engineer', 'release_verifier', 'release_diagnosis', 'recovery_coordinator'],
            'pipeline': definitions(self.config), 'website_pages': ['index.html', 'solutions.html', 'resources.html', 'about.html', 'planner.html']}

    def quality(self):
        import subprocess
        commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
        report = run_checks(self.config, commit, ROOT / 'runtime/agent-checks' / self.audit.run_id)
        report['artifact'] = self.audit.artifact('agent-quality-gate.json', report)
        return report

    def verify(self):
        state = self.controller.read_state()
        if not state or not state.get('current'): raise ValueError('No known-good release is recorded')
        manifest = self.controller.stored_manifest(state['current'])
        return verify_with_retries(self.config, manifest, self.aws, self.audit)

    def prepare_recovery(self):
        state = self.controller.read_state()
        if not state or not state.get('inflight'):
            return {'status': 'not_needed', 'finding': 'No interrupted release is recorded. Automatic rollback or the last deployment may already have completed.', 'state': state}
        pending = state['inflight']
        if not pending.get('previous'):
            raise ValueError('Initial adoption needs the recorded adoption restoration, not a release rollback')
        known = self.controller.stored_manifest(pending['previous'])
        plan = {'status': 'prepared', 'binding': self.config.binding, 'state_digest': digest(state),
                'failed_release': pending['candidate'], 'restore_release': known['release_id'],
                'operations': ['restore_recorded_origin_path', 'invalidate', 'verify_production']}
        plan['sha256'] = digest(plan)
        self.recovery_plan = plan
        self.audit.artifact('agent-recovery-plan.json', plan)
        return plan

    def apply_recovery(self, sha256):
        from .cicd import approve
        if not self.interactive: return {'status': 'approval_required', 'instruction': 'Review recovery with --interactive-approvals in a terminal'}
        plan = self.recovery_plan
        if not plan or plan['sha256'] != sha256: raise ValueError('Only the prepared deterministic recovery plan may be requested')
        state = self.controller.read_state()
        if digest(state) != plan['state_digest']: raise ValueError('Recovery plan is stale')
        approve(plan)
        self.audit.emit('human', 'approval_granted', {'stage': 'project6_recovery', 'plan': plan['sha256']})
        browser_available()
        self.aws.mutable = True
        try:
            return self.controller.recover('agent-recovery-' + uuid.uuid4().hex, expected_state_digest=plan['state_digest'])
        finally:
            self.aws.mutable = False

    def tools(self, role):
        empty = object_schema({})
        available = {
            'inspect_release_project': Tool('inspect_release_project', 'Inspect actual source configuration and concrete pipeline definitions.', empty, self.inspect),
            'render_pipeline': Tool('render_pipeline', 'Write the real pipeline, build projects, webhook, and scoped role policies locally. No AWS changes.', empty,
                                    lambda: render(self.config, self.audit.path / 'pipeline')),
            'validate_pipeline': Tool('validate_pipeline', 'Validate actual pipeline definitions and buildspec quality gates.', empty,
                                      lambda: validate_definitions(definitions(self.config), self.config)),
            'run_quality_gate': Tool('run_quality_gate', 'Execute the real Python/JavaScript suites, build the site, check navigation, and validate a commit-associated package.', empty, self.quality),
            'inspect_release_state': Tool('inspect_release_state', 'Read actual private release state without changing it.', empty,
                                        lambda: {'status': 'inspected', 'state': self.controller.read_state()}),
            'verify_production': Tool('verify_production', 'Measure the live release, security, and actual browser planner function.', empty, self.verify),
            'read_execution_evidence': Tool('read_execution_evidence', 'Read the requested actual CodePipeline/CodeBuild execution and its retained reports.', empty,
                                           lambda: read_execution(self.config, self.aws, execution_id(self.execution))),
            'prepare_recovery': Tool('prepare_recovery', 'Identify the recorded previous known-good release and prepare only its bounded restoration.', empty, self.prepare_recovery),
            'apply_recovery': Tool('apply_recovery', 'Request human approval for the prepared restoration. Cannot select an arbitrary release.',
                                   object_schema({'sha256': {'type': 'string'}}), self.apply_recovery),
        }
        choices = {
            'pipeline_engineer': ['inspect_release_project', 'render_pipeline', 'validate_pipeline'],
            'test_engineer': ['inspect_release_project', 'run_quality_gate'],
            'release_verifier': ['inspect_release_state', 'verify_production'],
            'release_diagnosis': ['read_execution_evidence', 'inspect_release_state'],
            'recovery_coordinator': ['inspect_release_state', 'read_execution_evidence', 'prepare_recovery', 'apply_recovery'],
        }
        return [available[name] for name in choices[role]]


def run_agents(config, aws, audit, mode, execution=None, interactive=False, *, model=None):
    if mode in ('diagnose', 'recover'): execution_id(execution)
    agent_config = Config.load()
    model = model or ResponsesModel(agent_config['model'])
    toolbox = ReleaseToolbox(config, aws, audit, execution, interactive)
    results = {}

    def invoke(role, task):
        message = {'sender': next(reversed(results)) if results else 'user', 'recipient': role,
                   'message_id': digest({'run': audit.run_id, 'role': role, 'sequence': audit.seq})[:16],
                   'task': task, 'context': {'project': 'Project 6', 'mode': mode, 'execution_id': execution,
                                            'interactive_approvals_enabled': interactive}, 'upstream': dict(results)}
        audit.emit('orchestrator', 'agent_message', message)
        print(f'[{role}] {task}', flush=True)
        result = Agent(role, agent_config, audit, toolbox, model).run(message)
        results[role] = result
        print(f'[{role}] {result["status"]}: {result["summary"]}', flush=True)
        return result

    if mode == 'engineer':
        invoke('pipeline_engineer', 'Inspect, render and validate the real release pipeline. Explain the quality gates and any missing setup.')
        invoke('test_engineer', 'Run mandatory executable tests and inspect the actual website release artifact. Report the measured gate result.')
    elif mode == 'verify':
        invoke('release_verifier', 'Independently evaluate the current public release, including browser function and security. Complete only after passing measurements.')
    else:
        invoke('release_diagnosis', 'Analyze this actual failed release. Identify the observed failure, source commit, affected stage, evidence paths, root cause and corrective action.')
        invoke('recovery_coordinator', 'Inspect actual release state. Identify the saved known-good version and whether automatic recovery already completed. Prepare bounded recovery if needed.' +
               (' Request the exact human approval and execute the prepared restoration if an interrupted release still exists.' if mode == 'recover' and interactive else ' Do not apply changes.'))
        if mode == 'recover' and interactive:
            invoke('release_verifier', 'Independently measure the current public release after recovery. Only complete if all checks pass.')
    return {'status': 'complete' if all(r['status'] == 'complete' for r in results.values()) else 'blocked', 'agents': results,
            'note': 'Actual model calls and executable tools only; deterministic pipeline jobs do not require an LLM.'}
