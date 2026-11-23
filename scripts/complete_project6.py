#!/usr/bin/env python3
"""Run the real Project 6 experiments and retain their measured evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cloudcrew.core import Audit, load_env, now, redact, write_json
from cloudcrew.release_aws import ReleaseAWS
from cloudcrew.release_config import ReleaseConfig
from cloudcrew.release_agents import read_execution
from cloudcrew.releases import ReleaseController

OWN_FILES = {
    "scripts/complete_project6.py",
    "scripts/finalize_project6_report.py",
    "docs/project6-demo-script.md",
    "cloudcrew/release_agents.py",
    "tests/test_release_agents.py",
    "tests/test_experiment_runner.py",
}
BUG = """
// Demo-only bug: the production condition hides the planner form.
if (typeof window !== "undefined" && window.location &&
    window.location.hostname === "csce412abhivur.me") {
  document.addEventListener("DOMContentLoaded", () => {
    const form = document.getElementById("launch-planner");
    if (form) form.remove();
  });
}
"""


class Experiments:
    def __init__(self, profile):
        self.profile = profile
        self.config = ReleaseConfig.load()
        self.audit = Audit("project6-experiments")
        self.aws = ReleaseAWS(self.config, self.audit, profile=profile)
        self.controller = ReleaseController(self.config, self.aws, self.audit)
        self.folder = ROOT / "runtime/project6/experiments" / self.audit.run_id
        self.folder.mkdir(parents=True)
        self.results = {"schema_version": 1, "started_at": now(),
                        "audit_run": self.audit.run_id, "experiments": {},
                        "agent_runs": {}, "errors": []}
        self.originals = {}
        self.source_started = False
        self.last_commit = None
        self.push_attempted = False

    def save(self):
        write_json(self.folder / "summary.json", self.results)

    def command(self, label, args, timeout=1200):
        print(f"[{label}] Running.", flush=True)
        path = self.folder / (label + ".log")
        # Never put environment values or credentials in command arguments.
        with path.open("w") as log:
            process = subprocess.Popen(args, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                       stdin=subprocess.DEVNULL, text=True)
            end = time.monotonic() + timeout
            while process.poll() is None:
                if time.monotonic() >= end:
                    process.terminate()
                    try:
                        process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                    raise TimeoutError(f"{label} timed out; inspect {path}")
                time.sleep(1)
        output = redact(path.read_text(errors="replace"))
        path.write_text(output)
        if process.returncode:
            raise RuntimeError(f"{label} failed. See {path}\n" + output[-1800:])
        print(f"[{label}] Complete.", flush=True)
        return output

    def git(self, *args):
        # Leading spaces are part of Git's two-column status format.
        return subprocess.check_output(["git", *args], cwd=ROOT, text=True).rstrip("\r\n")

    def cli(self, label, *args, timeout=1200):
        output = self.command(label, [sys.executable, "-m", "cloudcrew.cicd",
                              "--profile", self.profile, *args], timeout)
        match = re.search(r"Execution records: (.+)", output)
        if not match:
            raise RuntimeError(f"{label} did not record an audit run")
        run = Path(match.group(1).strip()).resolve()
        if run.parent != (ROOT / "runtime/runs").resolve():
            raise RuntimeError("Unexpected audit path")
        return run

    def agent(self, label, mode, execution=None):
        args = ["agents", "--mode", mode]
        if execution:
            args += ["--execution", execution]
        try:
            run = self.cli(label, *args)
            events = [json.loads(s) for s in (run / "events.jsonl").read_text().splitlines()]
            if not any(e["event"] == "model_response" and
                       str(e["data"].get("response_id", "")).startswith("resp_") for e in events):
                raise RuntimeError("No actual model response was recorded")
            self.results["agent_runs"][label] = {
                "run_id": run.name, "execution_id": execution, "status": "complete"}
            self.save()
            return run.name
        except Exception as exc:
            self.results["agent_runs"][label] = {"status": "failed", "error": redact(str(exc))}
            self.results["errors"].append(label + ": " + redact(str(exc)))
            self.save()
            print(f"[{label}] Recorded an error; continuing through source restoration.", flush=True)
            return None

    def state(self):
        value = self.controller.read_state()
        if not value or not value.get("current") or value.get("inflight"):
            raise RuntimeError("Production has no settled known-good release; resolve recovery first")
        return value

    def verify(self, label, expected):
        if self.state()["current"]["release_id"] != expected:
            raise RuntimeError(f"{label}: production state does not match the expected release")
        run = self.cli(label, "verify")
        events = [json.loads(s) for s in (run / "events.jsonl").read_text().splitlines()]
        final = next(e["data"]["result"] for e in reversed(events) if e["event"] == "run_completed")
        if final.get("status") != "passed" or final.get("release_id") != expected:
            raise RuntimeError(f"{label}: independent verification failed")
        write_json(self.folder / (label + ".json"), final)
        return final

    def wait(self, commit, expected, label):
        print(f"[{label}] Waiting for GitHub webhook execution of {commit[:12]}.", flush=True)
        end = time.monotonic() + 2700
        execution = None
        last_status = None
        while time.monotonic() < end:
            if execution is None:
                items = self.aws.call("codepipeline", "list-pipeline-executions",
                                      {"pipelineName": self.config["project"], "maxResults": 30})
                matches = [item for item in items.get("pipelineExecutionSummaries", [])
                           if any(r.get("revisionId") == commit for r in item.get("sourceRevisions", []))
                           and item.get("trigger", {}).get("triggerType") == "Webhook"]
                if matches:
                    execution = matches[0]["pipelineExecutionId"]
            if execution:
                result = self.aws.call("codepipeline", "get-pipeline-execution",
                                      {"pipelineName": self.config["project"],
                                       "pipelineExecutionId": execution})["pipelineExecution"]
                status = result["status"]
                if status != last_status:
                    print(f"[{label}] {execution}: {status}", flush=True)
                    last_status = status
                if status in ("Succeeded", "Failed", "Stopped", "Superseded", "Cancelled"):
                    records = read_execution(self.config, self.aws, execution)
                    write_json(self.folder / (label + "-execution.json"), records)
                    self.results["experiments"][label] = {
                        "execution_id": execution, "commit": commit, "pipeline_status": status}
                    self.save()
                    if status != expected:
                        raise RuntimeError(f"{label}: expected {expected}, measured {status}")
                    return records
            time.sleep(15)
        raise TimeoutError(f"{label}: pipeline wait timed out. Execution: {execution}")

    def record_for(self, records, suffix):
        matches = [v for k, v in records["evidence"].items() if k.endswith(suffix)]
        if not matches:
            raise RuntimeError("Required actual evidence is missing: " + suffix)
        return matches[0]

    def commit_push(self, label, paths, message):
        if self.last_commit and self.git("rev-parse", "HEAD") != self.last_commit:
            raise RuntimeError("HEAD changed outside this experiment; refusing to commit")
        changed = self.git("diff", "--name-only").splitlines()
        staged = self.git("diff", "--cached", "--name-only").splitlines()
        if set(changed + staged) - set(paths):
            raise RuntimeError("Other tracked files changed; refusing to include unrelated work")
        self.command(label + "-add", ["git", "add", "--", *paths])
        self.command(label + "-commit", ["git", "commit", "-m", message])
        self.last_commit = self.git("rev-parse", "HEAD")
        self.results.setdefault("commits", {})[label] = self.last_commit
        self.save()
        self.push_attempted = True
        self.command(label + "-push", ["git", "push", "origin", "HEAD:main"], timeout=180)
        return self.last_commit

    def export(self, label, records, agent=None):
        execution = records["execution_id"]
        args = ["evidence", "--execution", execution, "--output",
                str(self.folder / (label + "-evidence.zip"))]
        if agent:
            args += ["--agent-run", agent]
        self.cli(label + "-export", *args)

    def preflight(self):
        load_env()
        if not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError("Set OPENAI_API_KEY in the ignored local .env first")
        if self.git("branch", "--show-current") != "main":
            raise RuntimeError("Run from the deployment branch main")
        changes = self.git("status", "--porcelain").splitlines()
        if any(line[3:] not in OWN_FILES for line in changes):
            unexpected = [line[3:] for line in changes if line[3:] not in OWN_FILES]
            raise RuntimeError("Save other work first. Unexpected changed paths: " + ", ".join(unexpected))
        self.last_commit = self.git("rev-parse", "HEAD")
        self.preparation_paths = [line[3:] for line in changes]
        remote = self.git("ls-remote", "origin", "refs/heads/main").split()[0]
        if remote != self.last_commit:
            raise RuntimeError("Local main and remote main differ; synchronize before experiments")
        self.command("network-check", [sys.executable, "-c",
                     "import socket; socket.getaddrinfo('api.openai.com',443)"], timeout=30)
        self.command("browser-check", [sys.executable, "-c",
                     "from cloudcrew.release_verify import browser_available; browser_available()"], timeout=60)
        initial = self.state()
        if initial["current"]["release_id"] != self.last_commit:
            raise RuntimeError("Wait until the current HEAD is the verified production release")
        for name in ("website/assets/cloud-security.svg", "website/assets/app.js"):
            self.originals[name] = (ROOT / name).read_bytes()
            target = self.folder / "source-backup" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self.originals[name])
        self.results["baseline"] = initial
        self.save()
        return initial["current"]["release_id"]

    def run(self):
        baseline = self.preflight()
        self.verify("baseline-verification", baseline)
        successful = self.results["baseline"]["last_execution"].split("/")[0]
        records = read_execution(self.config, self.aws, successful)
        if records["pipeline"]["pipelineExecution"]["status"] != "Succeeded":
            raise RuntimeError("Baseline execution is not successful")
        self.results["experiments"]["successful-release"] = {
            "execution_id": successful, "commit": baseline, "pipeline_status": "Succeeded"}
        self.export("successful-release", records)
        if self.preparation_paths:
            prepared = self.commit_push("workflow-preparation", self.preparation_paths,
                                       "Add controlled experiment workflow and complete diagnosis evidence")
            records = self.wait(prepared, "Succeeded", "workflow-preparation")
            self.verify("workflow-preparation-verification", prepared)
            self.export("workflow-preparation", records)
            baseline = prepared
            self.results["prepared_baseline"] = self.state()
            self.save()
        self.agent("engineering-agents", "engineer")

        self.source_started = True
        asset = "website/assets/cloud-security.svg"
        (ROOT / asset).unlink()
        blocked = self.commit_push("blocked-source", [asset],
                                  "Demonstrate required asset validation blocking deployment")
        records = self.wait(blocked, "Failed", "blocked-source")
        actions = records["actions"]["actionExecutionDetails"]
        if not any(a["stageName"] == "BuildAndValidate" and a["status"] == "Failed" for a in actions):
            raise RuntimeError("The defect did not fail at the mandatory quality gate")
        if any(a["stageName"] == "Release" for a in actions):
            raise RuntimeError("The rejected source unexpectedly reached Release")
        validation = self.record_for(records, "validation-report.json")
        if validation["status"] != "failed":
            raise RuntimeError("Missing failed validation measurement")
        self.verify("blocked-source-production-unchanged", baseline)
        agent = self.agent("blocked-source-diagnosis", "diagnose", records["execution_id"])
        self.export("blocked-source", records, agent)
        self.results["experiments"]["blocked-source"]["production_unchanged"] = True
        self.save()

        (ROOT / asset).write_bytes(self.originals[asset])
        repaired = self.commit_push("asset-restoration", [asset],
                                   "Restore required asset after validation experiment")
        records = self.wait(repaired, "Succeeded", "asset-restoration")
        self.verify("asset-restoration-verification", repaired)
        self.export("asset-restoration", records)

        app = "website/assets/app.js"
        (ROOT / app).write_bytes(self.originals[app] + BUG.encode())
        candidate = self.commit_push("production-defect", [app],
                                     "Demonstrate production planner verification and rollback")
        records = self.wait(candidate, "Failed", "production-defect")
        actions = records["actions"]["actionExecutionDetails"]
        if not any(a["stageName"] == "BuildAndValidate" and a["status"] == "Succeeded" for a in actions):
            raise RuntimeError("The production-only defect did not pass pre-deployment validation")
        release = self.record_for(records, "release-result.json")
        failed = self.record_for(records, "production-verification.json")
        rollback = self.record_for(records, "rollback-verification.json")
        if (release.get("status") != "rolled_back" or not release.get("website_restored")
                or failed.get("status") != "failed" or rollback.get("status") != "passed"
                or rollback.get("release_id") != repaired):
            raise RuntimeError("Actual production failure and complete rollback have not been verified")
        if not any(c["name"] == "critical_planner_function" and not c["passed"] for c in failed["checks"]):
            raise RuntimeError("The failed measurement was not the intended browser planner function")
        self.verify("rollback-independent-verification", repaired)
        self.results["experiments"]["production-defect"].update(
            release_status="rolled_back", restored_release=repaired,
            failed_checks=[c["name"] for c in failed["checks"] if not c["passed"]],
            rollback_passed=rollback["passed"], rollback_total=rollback["total"])
        self.save()
        agent = self.agent("rollback-diagnosis", "diagnose", records["execution_id"])
        self.export("production-defect", records, agent)

        (ROOT / app).write_bytes(self.originals[app])
        paths = [app] + [p for p in sorted(OWN_FILES) if (ROOT / p).exists()]
        final = self.commit_push("final-correction", paths,
                                 "Remove controlled planner defect and add reproducible demo workflow")
        records = self.wait(final, "Succeeded", "final-correction")
        self.verify("final-production-verification", final)
        self.agent("final-verification-agent", "verify")
        self.export("final-correction", records)
        self.source_started = False
        self.results["final_release"] = final
        self.results["completed_at"] = now()
        self.results["status"] = "complete" if not self.results["errors"] else "needs_attention"
        self.save()

    def restore_on_error(self):
        if not self.source_started:
            return
        changed = []
        for name, data in self.originals.items():
            path = ROOT / name
            if not path.exists() or path.read_bytes() != data:
                path.write_bytes(data)
                changed.append(name)
        if not changed:
            return
        print("[cleanup] Restored the original source files.", flush=True)
        # A corrective push queues behind the bounded automatic rollback.
        if self.push_attempted and self.git("rev-parse", "HEAD") == self.last_commit:
            commit = self.commit_push("emergency-source-restoration", changed,
                                      "Restore healthy source after interrupted controlled experiment")
            records = self.wait(commit, "Succeeded", "emergency-source-restoration")
            self.verify("emergency-production-verification", commit)
            self.export("emergency-source-restoration", records)

    def bundle(self):
        filename = ("project6-execution-evidence.zip" if self.results.get("status") == "complete"
                    else "project6-incomplete-" + self.audit.run_id + ".zip")
        destination = ROOT / "submission" / filename
        destination.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(self.folder.rglob("*")):
                if path.is_file():
                    archive.write(path, "project6/" + str(path.relative_to(self.folder)))
            runs = {self.audit.run_id}
            runs.update(v["run_id"] for v in self.results["agent_runs"].values() if v.get("run_id"))
            for identifier in sorted(runs):
                for path in sorted((ROOT / "runtime/runs" / identifier).glob("*")):
                    if path.is_file() and path.suffix in (".json", ".jsonl"):
                        archive.writestr("project6/agents/" + identifier + "/" + path.name,
                                         redact(path.read_text()))
        print(f"Evidence: {destination}", flush=True)
        print("SHA-256:", hashlib.sha256(destination.read_bytes()).hexdigest(), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="default")
    args = parser.parse_args()
    os.chdir(ROOT)
    run = Experiments(args.profile)
    success = False
    try:
        run.run()
        success = run.results["status"] == "complete"
    except (Exception, KeyboardInterrupt) as exc:
        run.results["status"] = "failed"
        run.results["errors"].append(redact(str(exc) or "Interrupted"))
        run.save()
        print("Experiment stopped:", redact(str(exc)), flush=True)
        try:
            run.restore_on_error()
        except Exception as cleanup:
            run.results["errors"].append("Cleanup: " + redact(str(cleanup)))
            print("Inspect the recovery state before another run:", redact(str(cleanup)), flush=True)
    finally:
        run.save()
        run.audit.emit("system", "run_completed" if success else "run_failed", run.results)
        run.bundle()
    if success:
        run.command("refresh-report", [sys.executable, "scripts/finalize_project6_report.py",
                                      "--results", str(run.folder)])
        print("All experiments passed. The evidence, report, and demo script are ready.", flush=True)
    else:
        print("Some requirements remain incomplete. See:", run.folder / "summary.json", flush=True)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
