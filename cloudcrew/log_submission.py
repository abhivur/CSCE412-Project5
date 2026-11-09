"""Prepare a rubric-mapped ZIP from actual execution records, with a completeness gate."""
from __future__ import annotations

import hashlib
import json
import os
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .core import ROOT, Config, now, redact, write_json
from .exercise import observed_default_document_failure

ROLES = {
    "planner": ("01-requirements-agent.log", "Requirements and planning"),
    "website": ("02-website-agent.log", "Website"),
    "infrastructure": ("03-infrastructure-agent.log", "Infrastructure"),
    "verification": ("04-security-verification-agent.log", "Security and verification"),
    "diagnosis": ("05-diagnosis-recovery-agent.log", "Diagnosis and recovery"),
}
RUN_NAME = re.compile(r"\d{8}T\d{6}-[a-f0-9]{8}")
SECRET = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b|\bsk-[A-Za-z0-9_-]{16,}\b|-----BEGIN (?:RSA |EC )?PRIVATE KEY-----")


@dataclass
class Run:
    path: Path
    events: list[dict]
    artifacts: dict[str, dict]

    @property
    def run_id(self):
        return self.path.name

    @property
    def purpose(self):
        return self.events[0].get("data", {}).get("purpose", "")

    def has(self, event, operation=None):
        return any(e["event"] == event and (operation is None or e.get("data", {}).get("operation") == operation) for e in self.events)


def read_runs(root: Path) -> list[Run]:
    records = []
    if not root.exists():
        return records
    for path in sorted(root.iterdir()):
        source = path / "events.jsonl"
        if not path.is_dir() or path.is_symlink() or not RUN_NAME.fullmatch(path.name) or not source.is_file() or source.is_symlink():
            continue
        events = [json.loads(line) for line in source.read_text().splitlines() if line.strip()]
        if not events or events[-1].get("event") not in ("run_completed", "run_failed"):
            continue  # An active run is not a finished execution record.
        previous = 0
        for event in events:
            sequence = event.get("sequence", 0)
            if event.get("run_id") != path.name or sequence <= previous or not event.get("timestamp"):
                raise ValueError(f"Inconsistent event identity/sequence in {source}; inspect the source rather than manufacturing replacements")
            previous = sequence
        if "unit-test" in events[0].get("data", {}).get("purpose", "").lower():
            continue
        artifacts = {}
        for artifact in sorted(path.glob("*.json")):
            if artifact.is_file() and not artifact.is_symlink():
                artifacts[artifact.name] = json.loads(artifact.read_text())
        records.append(Run(path, events, artifacts))
    return records


def select_runs(records: list[Run]) -> list[Run]:
    selected = {}
    def latest(predicate):
        matches = [record for record in records if predicate(record)]
        if matches:
            selected[matches[-1].run_id] = matches[-1]
    latest(lambda r: r.has("operation_completed", "ensure_private_bucket"))
    latest(lambda r: r.has("operation_completed", "write_certificate_dns"))
    latest(lambda r: r.has("operation_completed", "ensure_distribution") and r.has("agent_completed"))
    latest(lambda r: r.purpose == "publish-gbm-cloud-content")
    latest(lambda r: "local-site-check.json" in r.artifacts and r.artifacts["local-site-check.json"].get("status") == "passed")
    latest(lambda r: "dns-diagnostic.json" in r.artifacts)
    successful = [r for r in records if r.artifacts.get("exercise-result.json", {}).get("status") == "passed"]
    if successful:
        selected[successful[-1].run_id] = successful[-1]
    else:
        latest(lambda r: r.purpose == "exercise")
    latest(lambda r: r.artifacts.get("verification.json", {}).get("status") == "passed")
    return [selected[key] for key in sorted(selected)]


def locate(event):
    return {"run_id": event["run_id"], "sequence": event["sequence"], "timestamp": event["timestamp"],
            "agent": event["agent"], "event": event["event"]}


def assess(selected: list[Run], domain: str, current_manifest: dict, failure_active: bool = False) -> dict:
    events = [event for run in selected for event in run.events]
    role_details = {}
    for role in ROLES:
        assigned = [e for e in events if e["agent"] == role and e["event"] == "task_received"]
        decisions = [e for e in events if e["agent"] == role and e["event"] == "agent_completed"]
        inference = [e for e in events if e["agent"] == role and e["event"] == "model_response"
                     and str(e.get("data", {}).get("response_id", "")).startswith("resp_")]
        role_details[role] = {"covered": bool(assigned and decisions and inference),
                              "tasks": len(assigned), "model_responses": len(inference), "decisions": len(decisions),
                              "example": locate(decisions[-1]) if decisions else None}
    messages = [e for e in events if e["event"] == "agent_message" and e.get("data", {}).get("upstream")]
    operations = [e for e in events if e["event"] == "tool_completed" and e["agent"] in ROLES
                  and e.get("data", {}).get("name") != "finish"]
    tooling_roles = sorted({e["agent"] for e in operations})
    exercises = [r for r in selected if "exercise-result.json" in r.artifacts]
    exercise = exercises[-1] if exercises else None
    failure_checks = {}
    proof = {}
    if exercise:
        artifacts = exercise.artifacts
        baseline = artifacts.get("exercise-baseline.json", {})
        failure = artifacts.get("exercise-failure.json", {})
        recovery = artifacts.get("exercise-recovery.json", {})
        result = artifacts["exercise-result.json"]
        approvals = [e for e in exercise.events if e["event"] == "approval_granted"]
        diagnosis = [e for e in exercise.events if e["agent"] == "diagnosis" and e["event"] == "agent_completed"]
        corrections = [e for e in exercise.events if e["event"] == "tool_requested"
                       and e.get("data", {}).get("name") == "prepare_plan"
                       and _arguments(e.get("data", {}).get("arguments", {})).get("stage") == "recovery"]
        operations_completed = {e.get("data", {}).get("operation") for e in exercise.events if e["event"] == "operation_completed"}
        def first_sequence(event_name, predicate):
            return next((e["sequence"] for e in exercise.events if e["event"] == event_name and predicate(e)), None)
        failure_approval = first_sequence("approval_granted", lambda e: e["data"].get("stage") == "failure" and e["agent"] == "human")
        failure_applied = first_sequence("operation_completed", lambda e: e["data"].get("operation") == "introduce_failure")
        recovery_approval = first_sequence("approval_granted", lambda e: e["data"].get("stage") == "recovery" and e["agent"] == "human")
        recovery_applied = first_sequence("operation_completed", lambda e: e["data"].get("operation") == "restore_configuration")
        before_checks = baseline.get("checks", [])
        after_checks = recovery.get("checks", [])
        index_after = next((e for e in after_checks if e.get("name") == "page:index.html"), {})
        failure_checks = {
            "passing_live_baseline": baseline.get("status") == "passed" and bool(before_checks) and all(c.get("passed") is True for c in before_checks),
            "actual_root_404_and_working_index": observed_default_document_failure(failure),
            "real_configuration_failure_applied": "introduce_failure" in operations_completed,
            "model_generated_diagnosis": any(e.get("data", {}).get("status") == "complete" for e in diagnosis)
                and any(e["agent"] == "diagnosis" and e["event"] == "model_response"
                        and str(e.get("data", {}).get("response_id", "")).startswith("resp_") for e in exercise.events),
            "proposed_recovery_plan": bool(corrections) and "plan-recovery.json" in artifacts,
            "failure_approved_by_human": any(e["data"].get("stage") == "failure" and e["agent"] == "human" for e in approvals),
            "recovery_approved_by_human": any(e["data"].get("stage") == "recovery" and e["agent"] == "human" for e in approvals),
            "approvals_precede_changes": all(value is not None for value in (failure_approval, failure_applied, recovery_approval, recovery_applied))
                and failure_approval < failure_applied < recovery_approval < recovery_applied,
            "saved_configuration_restored": "restore_configuration" in operations_completed,
            "passing_live_recovery": recovery.get("status") == "passed" and bool(after_checks) and all(c.get("passed") is True for c in after_checks),
            "domain_matches_submission": baseline.get("domain") == domain and recovery.get("domain") == domain,
            "recovery_matches_published_homepage": bool(current_manifest.get("index.html"))
                and index_after.get("observation", {}).get("body_sha256") == current_manifest.get("index.html"),
            "exercise_reports_complete": result.get("status") == "passed" and result.get("failure_observed") is True and result.get("website_restored") is True,
            "no_active_failure": not failure_active,
        }
        proof = {"run_id": exercise.run_id, "baseline": "exercise-baseline.json", "failure": "exercise-failure.json",
                 "recovery": "exercise-recovery.json", "approval_receipts": [locate(e) for e in approvals],
                 "diagnosis_decisions": [locate(e) for e in diagnosis]}
    criteria = {
        "agent_execution_records": {"rubric_points": 8, "covered": sum(v["covered"] for v in role_details.values()) >= 4, "roles": role_details},
        "programmatic_communication": {"rubric_points": 7, "covered": bool(messages), "handoffs_with_upstream_results": len(messages),
                                      "examples": [locate(e) for e in messages[:5]]},
        "tool_execution_and_results": {"rubric_points": 7, "covered": len(operations) >= 2 and len(tooling_roles) >= 2,
                                      "completed_tool_calls": len(operations), "agents_executing_tools": tooling_roles,
                                      "examples": [locate(e) for e in operations[:5]]},
        "controlled_failure_and_recovery": {"rubric_points": 6, "covered": bool(failure_checks) and all(failure_checks.values()),
                                          "checks": failure_checks, "source": proof},
        "organization_and_redaction": {"rubric_points": 2, "covered": True,
                                      "note": "Role logs, communication JSON, tool log, experiment log, evidence map, source records, hash manifest and secret scan are generated."},
    }
    missing = [key for key, value in criteria.items() if not value["covered"]]
    return {"generated_at": now(), "brand": "GBM Cloud", "domain": domain, "status": "ready" if not missing else "incomplete",
            "missing_requirements": missing, "criteria": criteria, "selected_runs": [r.run_id for r in selected],
            "grading_note": "Rubric points identify the assignment categories; coverage is not a promised grade."}


def _arguments(value):
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return {}


def cleaned(value):
    value = redact(value)
    if isinstance(value, dict):
        return {key: cleaned(item) for key, item in value.items()}
    if isinstance(value, list):
        return [cleaned(item) for item in value]
    if isinstance(value, str):
        return re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{12,}", "Bearer [REDACTED]", value)
    return value


def json_text(value):
    return json.dumps(cleaned(value), indent=2, ensure_ascii=False) + "\n"


def event_log(events, description):
    text = [description, "Derived from actual recorded events; timestamps are UTC. Payloads are redacted, not rewritten as success claims.", ""]
    if not events:
        text.append("NO MATCHING EXECUTION EVENTS ARE PRESENT.")
    for event in events:
        text += [f"[{event['timestamp']}] run={event['run_id']} seq={event['sequence']:04d} agent={event['agent']} event={event['event']}",
                 json.dumps(cleaned(event["data"]), indent=2, ensure_ascii=False), ""]
    return "\n".join(text) + "\n"


def create_files(selected: list[Run], readiness: dict) -> dict[str, str]:
    events = [e for run in selected for e in run.events]
    files = {}
    exercise = next((run for run in reversed(selected) if run.artifacts.get("exercise-result.json", {}).get("status") == "passed"), None)
    summary = ["GBM CLOUD — EXECUTION EVIDENCE SUMMARY", "Generated from the recorded events and measurements; all timestamps are UTC.",
               "Package coverage: " + readiness["status"], "Website: https://" + readiness["domain"] + "/", ""]
    if exercise:
        baseline = exercise.artifacts.get("exercise-baseline.json", {})
        failure = exercise.artifacts.get("exercise-failure.json", {})
        recovery = exercise.artifacts.get("exercise-recovery.json", {})
        failure_checks = {item["name"]: item for item in failure.get("checks", [])}
        diagnosis = exercise.artifacts["exercise-result.json"].get("diagnosis") or {}
        summary += ["CONTROLLED FAILURE AND RECOVERY", "Source run: " + exercise.run_id,
                    f"Baseline: {baseline.get('passed')}/{baseline.get('total')} checks passed; source exercise-baseline.json.",
                    "Measured failure: root HTTP " + str(failure_checks.get("page:/", {}).get("observation", {}).get("status_code"))
                    + "; explicit /index.html HTTP " + str(failure_checks.get("page:index.html", {}).get("observation", {}).get("status_code")) + ".",
                    f"During failure: {failure.get('passed')}/{failure.get('total')} health checks passed; the failing checks are retained as failures.",
                    "Recorded agent decision: " + diagnosis.get("summary", "No completed decision recorded."),
                    *["  Finding: " + finding for finding in diagnosis.get("findings", [])],
                    f"Recovery: {recovery.get('passed')}/{recovery.get('total')} checks passed; source exercise-recovery.json.",
                    "Human approvals and actual AWS update results are in 08-failure-recovery.log and source-runs/" + exercise.run_id + "/events.jsonl.", ""]
    else:
        summary += ["No completed controlled-exercise result is present. This is an incomplete review package.", ""]
    summary += ["OTHER EVIDENCE", "01–05: five specialized roles, including original task inputs, model usage and structured decisions.",
                "06: actual programmatic handoffs with upstream results. 07: selected tools and executable AWS observations.",
                "09: complete validation measurements. 10: rubric-category coverage and source event locators.",
                "11: earlier real DNS troubleshooting. 12: chronological selected-run index. 13: export secret scan.",
                "source-runs/: redacted source JSONL/JSON. manifest.json: hashes of the exported files."]
    files["00-run-summary.txt"] = "\n".join(summary) + "\n"
    for role, (filename, label) in ROLES.items():
        files[filename] = event_log([e for e in events if e["agent"] == role], label + " agent execution records")
    files["06-agent-communication.json"] = json_text([e for e in events if e["event"] == "agent_message"])
    tool_events = [e for e in events if e["event"] in
                   ("tool_requested", "tool_completed", "tool_failed", "aws_request", "aws_result", "aws_error", "operation_started", "operation_completed", "operation_failed")]
    files["07-tool-execution.log"] = event_log(tool_events, "Actual model-selected function tools, AWS requests/results, and recorded external CLI operations")
    exercise_ids = {r.run_id for r in selected if r.purpose == "exercise"}
    files["08-failure-recovery.log"] = event_log([e for e in events if e["run_id"] in exercise_ids], "Controlled failure and recovery: baseline, approvals, measured failure, model diagnosis, correction, and final checks where executed")
    validation = []
    for run in selected:
        for name, data in run.artifacts.items():
            if name == "verification.json" or name.startswith("exercise-") or name == "s3-content-verification.json":
                validation.append({"run_id": run.run_id, "source": f"source-runs/{run.run_id}/{name}", "data": data})
    files["09-validation-results.json"] = json_text(validation)
    files["10-evidence-map.json"] = json_text(readiness)
    dns_events = [e for e in events if e["event"] == "dns_diagnostic"]
    files["11-dns-troubleshooting.log"] = event_log(dns_events, "Additional actual DNS troubleshooting; separate from the controlled failure exercise")
    timeline = []
    for run in selected:
        timeline.append({"run_id": run.run_id, "started_at": run.events[0]["timestamp"], "ended_at": run.events[-1]["timestamp"],
                         "purpose": run.purpose, "last_event": run.events[-1]["event"],
                         "event_count": len(run.events), "source": f"source-runs/{run.run_id}/events.jsonl"})
        files[f"source-runs/{run.run_id}/events.jsonl"] = "".join(json.dumps(cleaned(e), ensure_ascii=False) + "\n" for e in run.events)
        for name, data in run.artifacts.items():
            files[f"source-runs/{run.run_id}/{name}"] = json_text(data)
    files["12-run-index.json"] = json_text(timeline)
    status = "READY: required categories have recorded evidence." if readiness["status"] == "ready" else "INCOMPLETE: DO NOT SUBMIT AS A COMPLETE LOG PACKAGE."
    files["README.txt"] = (
        "GBM CLOUD — CSCE 412 PROJECT 5 — AI AGENT EXECUTION LOGS\n"
        + status + "\n\n"
        "Website: https://" + readiness["domain"] + "/\n"
        "All execution timestamps and run directory names use UTC.\n\n"
        "START HERE\n"
        "1. Read 00-run-summary.txt for measured outcomes, then 10-evidence-map.json for assignment-category coverage.\n"
        "2. Read 01 through 05 for specialized-agent tasks, real model response metadata, tool results and structured decisions.\n"
        "3. Read 06 for the actual programmatic task/result handoffs, and 07 for requested tools and real observations.\n"
        "4. Read 08 chronologically for the controlled experiment, then 09 for measured validation results.\n"
        "5. Source JSONL/JSON records are retained under source-runs; 12 lists each selected run and purpose.\n\n"
        "WORKFLOW AND PROVENANCE\n"
        "The original multi-agent deployment records include requirements planning, website work, AWS deployment,\n"
        "security verification and diagnosis. Early deployment verification recorded real DNS/propagation failures.\n"
        "The website was subsequently rebranded as GBM Cloud; older source logs retain the earlier project wording.\n"
        "The GBM Cloud content-publication run is explicitly labeled as Codex invoking AWS CLI, rather than a new\n"
        "custom-agent execution. The successful controlled exercise, when present, records its own passing baseline,\n"
        "actual missing-default-document change, root-404/index-200 observation, model diagnosis and proposed repair,\n"
        "separate human approvals, recorded configuration restoration, and passing live recovery.\n"
        "Early DNS trouble is additional evidence and does not substitute for the controlled experiment.\n\n"
        "LOG FORMAT\n"
        "Readable logs are views of source events, identified by run_id and sequence. Model findings/decisions are\n"
        "the agent_completed payloads; model_response records carry actual response IDs, model names and usage.\n"
        "The package contains brief structured decisions and observations, not private model chain-of-thought.\n"
        "No unit-test fixture, screenshot transcript, or manually invented success event is included as execution evidence.\n\n"
        "REDACTION AND INTEGRITY\n"
        "Sensitive fields, configured environment secrets, common API/access-key patterns and bearer tokens are redacted.\n"
        "The export is scanned before ZIP creation. No .env, credential file or source-code test fixture is packaged.\n"
        "manifest.json records SHA-256 hashes of the exported files, after redaction; source timestamps and sequences\n"
        "are preserved. The readable views intentionally duplicate selected source events for convenient review.\n\n"
        "REMAINING COVERAGE GAPS\n"
        + ("\n".join(readiness["missing_requirements"]) if readiness["missing_requirements"] else "None identified by the package completeness checks.")
        + "\n"
    )
    return files


def secret_scan(files: dict[str, str]) -> dict:
    secrets = [value for key, value in os.environ.items()
               if len(value) > 7 and re.search(r"SECRET|PASSWORD|API_KEY|ACCESS_KEY|SESSION_TOKEN", key)]
    flagged = []
    for name, content in files.items():
        if SECRET.search(content) or any(value in content for value in secrets):
            flagged.append(name)
    if flagged:
        raise ValueError("Secret scan blocked export; inspect these filenames without exposing their contents: " + ", ".join(flagged))
    return {"status": "passed", "files_scanned": len(files), "checks": ["configured environment secrets", "API/access-key patterns", "private-key headers"],
            "note": "Capture and export also redact sensitive structured fields and bearer tokens."}


def prepare_log_submission(config: Config, draft: bool = False, runs_root: Path | None = None, output_root: Path | None = None, state_data: dict | None = None) -> dict:
    records = read_runs(runs_root or ROOT / "runtime/runs")
    selected = select_runs(records)
    if state_data is None:
        state_path = ROOT / "runtime/state.json"
        state_data = json.loads(state_path.read_text()) if state_path.exists() else {}
    readiness = assess(selected, config["domain"], state_data.get("published_manifest", {}), bool(state_data.get("failure_active")))
    destination = output_root or ROOT / "submission"
    destination.mkdir(parents=True, exist_ok=True)
    write_json(destination / "agent-logs-readiness.json", readiness)
    if readiness["status"] != "ready" and not draft:
        raise ValueError("Agent logs are not submission-ready. Missing: " + ", ".join(readiness["missing_requirements"])
                         + ". Complete a real successful 'python3 -m cloudcrew exercise', then rerun 'python3 -m cloudcrew logs'.")
    files = create_files(selected, readiness)
    try:
        scan = secret_scan(files)
    except ValueError:
        readiness["status"] = "incomplete"
        readiness["criteria"]["organization_and_redaction"]["covered"] = False
        if "organization_and_redaction" not in readiness["missing_requirements"]:
            readiness["missing_requirements"].append("organization_and_redaction")
        write_json(destination / "agent-logs-readiness.json", readiness)
        raise
    files["13-secret-scan.json"] = json_text(scan)
    hashes = [{"file": name, "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(), "bytes": len(content.encode("utf-8"))}
              for name, content in sorted(files.items())]
    files["manifest.json"] = json_text({"generated_at": now(), "hash_algorithm": "SHA-256", "files": hashes})
    filename = "project5-agent-logs.zip" if readiness["status"] == "ready" else "project5-agent-logs-INCOMPLETE.zip"
    output = destination / filename
    staging = destination / (filename + ".tmp")
    with zipfile.ZipFile(staging, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(files.items()):
            archive.writestr("project5-agent-logs/" + name, content.encode("utf-8"))
    with zipfile.ZipFile(staging) as archive:
        if archive.testzip() is not None:
            raise ValueError("ZIP integrity check failed")
    staging.replace(output)
    sha = hashlib.sha256(output.read_bytes()).hexdigest()
    (destination / (filename + ".sha256")).write_text(sha + "  " + filename + "\n")
    return {"status": readiness["status"], "path": str(output), "bytes": output.stat().st_size, "sha256": sha,
            "files": len(files), "selected_runs": readiness["selected_runs"], "secret_scan": scan,
            "missing_requirements": readiness["missing_requirements"]}
