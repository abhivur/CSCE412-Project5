#!/usr/bin/env python3
"""Display compact projections of actual recorded demo evidence, without executing tools."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cloudcrew.core import ROOT, load_env, redact

RUN_ID = re.compile(r"\d{8}T\d{6}-[a-f0-9]{8}")


def events(path: Path) -> list[dict]:
    text = (path / "events.jsonl").read_text()
    lines = text.splitlines()
    result = []
    for index, line in enumerate(lines):
        try:
            result.append(json.loads(line))
        except json.JSONDecodeError:
            # An active writer may not have finished its last JSONL record yet.
            if index != len(lines) - 1 or text.endswith("\n"):
                raise
    return result


def brief(value):
    if not isinstance(value, dict):
        return value
    keys = ("status", "summary", "passed", "total", "pages_checked", "brand",
            "artifact", "deployment_status", "error", "operations", "sha256")
    result = {key: value[key] for key in keys if key in value}
    return result or {"recorded_fields": list(value)}


def show(value) -> None:
    print(json.dumps(redact(value), indent=2, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--latest", choices=("agents", "exercise"), help="Newest actual run for this command, including an active run")
    selection.add_argument("--run", help="Exact recorded run ID, without a path")
    parser.add_argument("--section", choices=("summary", "handoffs", "tools", "recovery", "verification"), default="summary")
    args = parser.parse_args()
    load_env()
    base = ROOT / "runtime/runs"
    candidates = sorted((p for p in base.iterdir() if RUN_ID.fullmatch(p.name)
                         and p.is_dir() and not p.is_symlink()), reverse=True) if base.exists() else []
    selected = None
    records = []
    if args.run and not RUN_ID.fullmatch(args.run):
        parser.error("Use a run ID printed by cloudcrew, not a filesystem path")
    for path in candidates:
        if args.run and path.name != args.run:
            continue
        if not (path / "events.jsonl").is_file():
            continue
        current = events(path)
        if args.latest and not any(e.get("event") == "run_started" and e.get("data", {}).get("purpose") == args.latest for e in current):
            continue
        selected, records = path, current
        break
    if selected is None:
        parser.error("No matching actual run exists in this workspace. Execute the workflow first.")
    print(f"Recorded run: {selected.name} | section: {args.section}")
    print("Read-only projection of saved records. This viewer does not invoke agents or AWS.\n")
    if args.section == "summary":
        for event in records:
            if event["event"] in ("run_started", "agent_completed", "run_completed", "run_failed", "agent_budget_exhausted"):
                show({"time_utc": event["timestamp"], "sequence": event["sequence"],
                      "agent": event["agent"], "event": event["event"], "result": brief(event["data"])})
        models = [e for e in records if e["event"] == "model_response"]
        show({"recorded_model_calls": len(models), "models": sorted({e["data"]["model"] for e in models})})
    elif args.section == "handoffs":
        for event in records:
            if event["event"] != "agent_message":
                continue
            data = event["data"]
            show({"time_utc": event["timestamp"], "sequence": event["sequence"],
                  **{key: data[key] for key in ("sender", "recipient", "message_id", "task")},
                  "context": {key: data.get("context", {})[key] for key in ("mode", "stage", "verification_goal") if key in data.get("context", {})},
                  "upstream": {key: brief(value) for key, value in data.get("upstream", {}).items()}})
    elif args.section == "tools":
        for event in records:
            kind, data = event["event"], event["data"]
            common = {"sequence": event["sequence"], "agent": event["agent"], "event": kind}
            if kind == "model_response":
                show({**common, "model": data["model"], "response_id": data["response_id"], "usage": data.get("usage")})
            elif kind in ("tool_requested", "tool_completed", "tool_failed") and data.get("name") != "finish":
                item = {**common, "tool": data["name"], "call_id": data.get("call_id")}
                if "arguments" in data:
                    item["arguments"] = json.loads(data["arguments"])
                if "result" in data:
                    item["result"] = brief(data["result"])
                show(item)
    elif args.section == "recovery":
        for stage in ("failure", "recovery"):
            path = selected / f"plan-{stage}.json"
            if path.exists():
                data = json.loads(path.read_text())
                show({"artifact": path.name, "stage": data["stage"], "operations": data["operations"],
                      "before_default_root": data["details"]["before"]["DefaultRootObject"],
                      "after_default_root": data["details"]["after"]["DefaultRootObject"], "sha256": data["sha256"]})
        for event in records:
            if event["event"] in ("approval_granted", "operation_completed", "exercise_phase_failed"):
                show({"time_utc": event["timestamp"], "sequence": event["sequence"], "event": event["event"], "data": event["data"]})
        path = selected / "diagnosis-result.json"
        if path.exists():
            data = json.loads(path.read_text())
            show({"artifact": path.name, "status": data["status"], "summary": data["summary"], "findings": data["findings"]})
    else:
        for phase in ("baseline", "failure", "recovery"):
            path = selected / f"exercise-{phase}.json"
            if path.exists():
                data = json.loads(path.read_text())
                show({"phase": phase, **brief(data), "root_and_index": [
                    {"check": c["name"], "passed": c["passed"], "status_code": c.get("observation", {}).get("status_code")}
                    for c in data.get("checks", []) if c["name"] in ("page:/", "page:index.html")]})
        path = selected / "exercise-result.json"
        if path.exists():
            data = json.loads(path.read_text())
            show({"artifact": path.name, **{key: data[key] for key in ("status", "failure_observed", "website_restored", "error")}})


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError) as exc:
        print(f"Evidence could not be read: {redact(str(exc))}", file=sys.stderr)
        raise SystemExit(1)
