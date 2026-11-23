import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cloudcrew.agents import Agent, Tool
from cloudcrew.core import Audit, Config
from cloudcrew.llm import object_schema
from cloudcrew.release_agents import ReleaseToolbox, read_execution, run_agents
from cloudcrew.release_evidence import export_execution
from test_agents import ScriptedModel, finish
from test_releases import config_fixture, MemoryAWS


class ReleaseAgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.audit = Audit('unit-fixture-only', self.root / 'runs')
        self.config = config_fixture(); self.aws = MemoryAWS(self.config)
        self.agent_config = Config.load()

    def test_five_project6_roles_have_meaningful_restricted_tools(self):
        box = ReleaseToolbox(self.config, self.aws, self.audit)
        for role, required in [('pipeline_engineer', 'render_pipeline'), ('test_engineer', 'run_quality_gate'),
                               ('release_verifier', 'verify_production'), ('release_diagnosis', 'read_execution_evidence'),
                               ('recovery_coordinator', 'prepare_recovery')]:
            tools = {t.name for t in box.tools(role)}
            self.assertIn(required, tools)
            self.assertNotIn('run_shell', tools)
            self.assertNotIn('apply_pipeline', tools)

    def test_pipeline_engineer_executes_real_render_and_validation_tools(self):
        model = ScriptedModel([('render_pipeline', {}), ('validate_pipeline', {}), ('finish', finish())])
        result = Agent('pipeline_engineer', self.agent_config, self.audit,
                       ReleaseToolbox(self.config, self.aws, self.audit), model).run({'task': 'unit fixture'})
        self.assertEqual(result['status'], 'complete')
        self.assertTrue((self.audit.path / 'pipeline/codepipeline.json').is_file())
        output = json.loads(model.inputs[1][-1]['output'])
        self.assertEqual(output['status'], 'prepared')

    def test_recovery_cannot_claim_complete_after_only_preparing_an_authorized_repair(self):
        class Box:
            cloud = True; interactive = True; recovery_plan = {'unit': 'fixture'}
            def tools(self, role):
                return [Tool(name, 'Unit fixture', object_schema({}), lambda: {'status': 'prepared'})
                        for name in ('inspect_release_state', 'prepare_recovery')]
        model = ScriptedModel([('inspect_release_state', {}), ('prepare_recovery', {}), ('finish', finish()), ('finish', finish('blocked'))])
        result = Agent('recovery_coordinator', self.agent_config, self.audit, Box(), model).run({'task': 'unit fixture'})
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('apply_recovery', model.inputs[3][-1]['output'])

    def test_failed_test_gate_cannot_be_reported_as_completed(self):
        class Box:
            cloud = True; interactive = False
            def tools(self, role):
                return [Tool('run_quality_gate', 'Unit fixture', object_schema({}), lambda: {'status': 'failed', 'error': 'Required asset missing'})]
        model = ScriptedModel([('run_quality_gate', {}), ('finish', finish()), ('finish', finish('blocked'))])
        result = Agent('test_engineer', self.agent_config, self.audit, Box(), model).run({'task': 'unit fixture'})
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('run_quality_gate', model.inputs[2][-1]['output'])

    def test_recovery_tool_cannot_apply_a_plan_without_interactive_authorization(self):
        box = ReleaseToolbox(self.config, self.aws, self.audit, interactive=False)
        self.assertEqual(box.apply_recovery('arbitrary')['status'], 'approval_required')
        self.assertFalse(self.aws.writes)

    def test_evidence_diagnosis_is_bound_to_the_requested_execution_and_commit(self):
        class Aws:
            def call(self, service, operation, params):
                if operation == 'get-pipeline-execution': return {'pipelineExecution': {'artifactRevisions': [{'revisionId': 'a' * 40}], 'status': 'Failed'}}
                if operation == 'list-action-executions': return {'actionExecutionDetails': []}
                raise AssertionError(operation)
            def get_json(self, bucket, key, optional=False):
                return {'execution_id': 'unit-execution', 'source_commit': 'b' * 40, 'prefix': 'evidence/unit-execution/unit-build', 'files': []}, 'fixture'
        with self.assertRaisesRegex(ValueError, 'source commit differs'):
            read_execution(self.config, Aws(), 'unit-execution')

    def test_agent_handoffs_pass_prior_structured_findings_programmatically(self):
        messages = []
        class FixtureAgent:
            def __init__(self, role, *args): self.role = role
            def run(self, message):
                messages.append(message)
                return {'status': 'complete', 'summary': self.role + ' fixture result', 'findings': ['unit only']}
        with patch('cloudcrew.release_agents.Agent', FixtureAgent), patch('builtins.print'):
            result = run_agents(self.config, self.aws, self.audit, 'engineer', model=object())
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(messages[1]['sender'], 'pipeline_engineer')
        self.assertEqual(messages[1]['upstream']['pipeline_engineer']['summary'], 'pipeline_engineer fixture result')

    def test_diagnosis_reads_retained_production_and_rollback_reports(self):
        class Aws:
            def call(self, service, operation, params):
                if operation == 'get-pipeline-execution':
                    return {'pipelineExecution': {'artifactRevisions': [{'revisionId': 'a' * 40}]}}
                return {'actionExecutionDetails': []}

            def get_json(self, bucket, key, optional=False):
                if key.endswith('index-validate.json'):
                    return None, None
                if key.endswith('index-release.json'):
                    return {'execution_id': 'unit-execution', 'source_commit': 'a' * 40,
                            'prefix': 'evidence/unit-execution/unit-build',
                            'files': ['audit/production-verification.json',
                                      'audit/rollback-verification.json']}, 'fixture'
                return {'status': 'failed' if 'production-' in key else 'passed'}, 'fixture'

        result = read_execution(self.config, Aws(), 'unit-execution')
        self.assertEqual(result['evidence']['release/audit/production-verification.json']['status'], 'failed')
        self.assertEqual(result['evidence']['release/audit/rollback-verification.json']['status'], 'passed')


class EvidenceTests(unittest.TestCase):
    def test_execution_export_keeps_failed_status_and_hashes_actual_fixture_bytes(self):
        import hashlib
        import zipfile
        config = config_fixture()
        records = {'status': 'inspected', 'execution_id': 'unit-execution', 'pipeline': {'status': 'Failed'}, 'builds': [], 'evidence': {'status': 'rolled_back'}}
        class Aws:
            def get_json(self, *args, **kwargs): return None, None
        with tempfile.TemporaryDirectory() as tmp, patch('cloudcrew.release_evidence.read_execution', return_value=records):
            output = Path(tmp) / 'unit-fixture-evidence.zip'
            export_execution(config, Aws(), 'unit-execution', output)
            with zipfile.ZipFile(output) as archive:
                self.assertIsNone(archive.testzip())
                data = archive.read('project6-evidence/execution.json')
                self.assertIn(b'Failed', data)
                manifest = json.loads(archive.read('project6-evidence/manifest.json'))
                row = next(v for v in manifest if v['path'] == 'execution.json')
                self.assertEqual(row['sha256'], hashlib.sha256(data).hexdigest())


if __name__ == '__main__': unittest.main()
