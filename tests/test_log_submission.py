"""Export validation fixtures live only in temporary directories, never runtime/runs."""
import hashlib
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from cloudcrew.core import Config, ROOT
from cloudcrew.log_submission import Run, assess, cleaned, prepare_log_submission, read_runs, secret_scan

DOMAIN = "csce412teststudent.me"
HASH = "a" * 64


def event_list(run_id):
    events = []
    def add(agent, kind, data):
        events.append({"run_id": run_id, "sequence": len(events) + 1, "timestamp": "2026-01-01T00:00:00+00:00",
                       "agent": agent, "event": kind, "data": data})
    return events, add


def report(status="passed", root=200):
    return {"status": status, "domain": DOMAIN, "checks": [
        {"name": "page:/", "passed": root == 200, "observation": {"status_code": root, "body_sha256": HASH}},
        {"name": "page:index.html", "passed": True, "observation": {"status_code": 200, "body_sha256": HASH}}]}


def write_run(root, run_id, events, artifacts=None):
    path = root / run_id
    path.mkdir(parents=True)
    (path / "events.jsonl").write_text("".join(json.dumps(event) + "\n" for event in events))
    for name, value in (artifacts or {}).items():
        (path / name).write_text(json.dumps(value))
    return path


def make_fixtures(root):
    # Synthetic data is exclusively a unit test fixture; it is never submitted.
    first = "20260101T000000-00000001"
    events, add = event_list(first)
    add("system", "run_started", {"purpose": "agents"})
    for role in ("planner", "website", "infrastructure", "verification", "diagnosis"):
        add(role, "task_received", {"task": "Unit fixture only"})
        add(role, "model_response", {"response_id": "resp_unit_fixture", "model": "unit-fixture"})
        add(role, "agent_completed", {"status": "complete", "summary": "Unit fixture only"})
    add("orchestrator", "agent_message", {"sender": "planner", "recipient": "website", "upstream": {"fixture": True}})
    add("website", "tool_completed", {"name": "build_site", "result": {"status": "built"}})
    add("infrastructure", "tool_completed", {"name": "prepare_plan", "result": {"status": "prepared"}})
    add("infrastructure", "operation_completed", {"operation": "ensure_distribution"})
    add("system", "run_completed", {})
    write_run(root, first, events)

    second = "20260101T000001-00000002"
    events, add = event_list(second)
    add("system", "run_started", {"purpose": "exercise"})
    add("human", "approval_granted", {"stage": "failure", "method": "interactive_exact_digest"})
    add("infrastructure", "operation_completed", {"operation": "introduce_failure"})
    add("verification", "verification_check", report("failed", 404)["checks"][0])
    add("diagnosis", "model_response", {"response_id": "resp_unit_fixture_diagnosis"})
    add("diagnosis", "tool_requested", {"name": "prepare_plan", "arguments": '{"stage":"recovery"}'})
    add("human", "approval_granted", {"stage": "recovery", "method": "interactive_exact_digest"})
    add("infrastructure", "operation_completed", {"operation": "restore_configuration"})
    add("diagnosis", "agent_completed", {"status": "complete", "summary": "Unit fixture only"})
    add("verification", "verification_check", report()["checks"][0])
    add("system", "run_completed", {})
    artifacts = {"exercise-baseline.json": report(), "exercise-failure.json": report("failed", 404),
                 "exercise-recovery.json": report(), "verification.json": report(), "plan-recovery.json": {"stage": "recovery"},
                 "exercise-result.json": {"status": "passed", "failure_observed": True, "website_restored": True}}
    write_run(root, second, events, artifacts)
    return second


class LogSubmissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.runs = self.root / "runs"
        self.output = self.root / "submission"
        values = json.loads((ROOT / "config/project.example.json").read_text())
        self.config = Config(dict(values, domain=DOMAIN))
        self.state = {"published_manifest": {"index.html": HASH}, "failure_active": False}

    def test_missing_failure_evidence_blocks_final_submission_filename(self):
        self.runs.mkdir()
        with self.assertRaisesRegex(ValueError, "not submission-ready"):
            prepare_log_submission(self.config, runs_root=self.runs, output_root=self.output, state_data=self.state)
        self.assertFalse((self.output / "project5-agent-logs.zip").exists())
        readiness = json.loads((self.output / "agent-logs-readiness.json").read_text())
        self.assertIn("controlled_failure_and_recovery", readiness["missing_requirements"])

    def test_complete_fixture_exports_readable_logs_sources_and_valid_hashes(self):
        make_fixtures(self.runs)
        result = prepare_log_submission(self.config, runs_root=self.runs, output_root=self.output, state_data=self.state)
        self.assertEqual(result["status"], "ready")
        with zipfile.ZipFile(result["path"]) as archive:
            self.assertIsNone(archive.testzip())
            names = archive.namelist()
            self.assertIn("project5-agent-logs/README.txt", names)
            self.assertIn("project5-agent-logs/08-failure-recovery.log", names)
            self.assertTrue(any("/source-runs/" in name for name in names))
            manifest = json.loads(archive.read("project5-agent-logs/manifest.json"))
            for item in manifest["files"]:
                data = archive.read("project5-agent-logs/" + item["file"])
                self.assertEqual(hashlib.sha256(data).hexdigest(), item["sha256"])

    def test_a_failed_or_incomplete_exercise_cannot_be_reported_ready(self):
        second = make_fixtures(self.runs)
        path = self.runs / second / "exercise-result.json"
        path.write_text(json.dumps({"status": "failed", "failure_observed": True, "website_restored": True}))
        records = read_runs(self.runs)
        evidence = assess(records, DOMAIN, {"index.html": HASH})
        self.assertFalse(evidence["criteria"]["controlled_failure_and_recovery"]["covered"])

    def test_stale_recovery_different_homepage_or_active_failure_is_rejected(self):
        make_fixtures(self.runs)
        records = read_runs(self.runs)
        self.assertEqual(assess(records, DOMAIN, {"index.html": "b" * 64})["status"], "incomplete")
        self.assertEqual(assess(records, DOMAIN, {"index.html": HASH}, failure_active=True)["status"], "incomplete")

    def test_secret_redaction_and_scan_do_not_print_or_export_tokens(self):
        secret = "sk-proj-" + "x" * 40
        with patch.dict(os.environ, {"OPENAI_API_KEY": secret}):
            text = json.dumps(cleaned({"api_key": secret, "observation": "Bearer " + secret}))
            self.assertNotIn(secret, text)
            self.assertEqual(secret_scan({"safe.json": text})["status"], "passed")
            with self.assertRaisesRegex(ValueError, "Secret scan blocked"):
                secret_scan({"unsafe.json": secret})

    def test_unfinished_runs_are_not_exported_as_completed_records(self):
        run_id = "20260101T000000-00000001"
        events, add = event_list(run_id)
        add("system", "run_started", {"purpose": "exercise"})
        write_run(self.runs, run_id, events)
        self.assertEqual(read_runs(self.runs), [])


if __name__ == "__main__":
    unittest.main()
