from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from .core import ROOT, Audit, Config, write_json


def main() -> None:
    parser = argparse.ArgumentParser(description="GBM Cloud: CSCE 412 multi-agent secure AWS website")
    parser.add_argument("--config", type=Path, help="Configuration JSON; defaults to config/project.json")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Copy example configuration without overwriting existing files")
    commands.add_parser("doctor", help="Check local prerequisites without printing credentials or contacting AWS")
    commands.add_parser("build", help="Build the GBM Cloud website and compatibility pages into dist/")
    commands.add_parser("check", help="Check built local website navigation and assets")
    commands.add_parser("architecture", help="Save an offline architecture specification")
    plan = commands.add_parser("plan", help="Prepare an AWS change plan; performs read-only cloud requests as needed")
    from .deploy import STAGES
    plan.add_argument("--stage", required=True, choices=STAGES)
    deploy = commands.add_parser("deploy", help="Prepare, display and interactively approve a stage in one command")
    deploy.add_argument("--stage", required=True, choices=STAGES)
    apply = commands.add_parser("apply", help="Review and interactively approve one exact saved AWS change plan")
    apply.add_argument("--plan", required=True, type=Path)
    commands.add_parser("status", help="Read actual certificate and CloudFront status")
    commands.add_parser("verify", help="Run deterministic live verification and save technical evidence")
    commands.add_parser("dns", help="Compare system, public and authoritative DNS without changing any settings")
    exercise = commands.add_parser("exercise", help="Run a real approved failure, agent diagnosis and approved recovery with live evidence")
    exercise.add_argument("--timeout", type=int, default=900, help="Maximum seconds per CloudFront readiness wait (1–1800)")
    ready = commands.add_parser("wait", help="Wait for CloudFront deployment and the latest invalidation; read-only")
    ready.add_argument("--timeout", type=int, default=900)
    agents = commands.add_parser("agents", help="Execute real LLM agents with restricted tools and structured messages")
    agents.add_argument("--mode", choices=("local", "deploy", "verify", "diagnose"), default="local")
    agents.add_argument("--stage", choices=STAGES, default="foundation")
    agents.add_argument("--interactive-approvals", action="store_true", help="Allow agents to present plans for terminal approval")
    agents.add_argument("--request", default="", help="Optional high-level requirements for the planner")
    bundle = commands.add_parser("bundle", help="Package actual execution records into the submission logs ZIP")
    bundle.add_argument("--run", action="append", default=[], help="Run ID to include (repeatable); default includes all runs")
    logs = commands.add_parser("logs", help="Prepare the rubric-mapped agent log submission; requires real completed failure/recovery evidence")
    logs.add_argument("--draft", action="store_true", help="Export a clearly marked incomplete review ZIP when evidence is missing")
    commands.add_parser("report", help="Generate evidence-aware HTML and PDF technical report drafts")
    args = parser.parse_args()
    audit = None
    try:
        if args.command == "init":
            for source, destination in [(ROOT / "config/project.example.json", ROOT / "config/project.json"),
                                        (ROOT / ".env.example", ROOT / ".env")]:
                if not destination.exists():
                    shutil.copyfile(source, destination)
                    if destination.name == ".env":
                        destination.chmod(0o600)
                    print(f"Created {destination.relative_to(ROOT)}")
                else:
                    print(f"Kept existing {destination.relative_to(ROOT)}")
            return
        config = Config.load(args.config)
        audit = Audit(args.command)
        if args.command == "doctor":
            report = {"python": sys.version.split()[0], "python_supported": sys.version_info >= (3, 10),
                      "aws_cli_available": bool(shutil.which("aws")), "node_available_for_tests": bool(shutil.which("node")),
                      "api_key_configured": bool(os.environ.get("OPENAI_API_KEY")), "model": config["model"],
                      "aws_profile": config["aws_profile"], "git_repository_initialized": (ROOT / ".git").exists(),
                      "note": "Presence checks only. Does not establish API billing, AWS authentication, permissions, domain ownership, or deployment."}
            try:
                config.validate(cloud=True)
                report["cloud_config_valid"] = True
            except ValueError as exc:
                report.update(cloud_config_valid=False, cloud_config_error=str(exc))
            audit.artifact("doctor.json", report)
            print(json.dumps(report, indent=2))
            return
        from . import site
        from .agents import Toolbox, workflow
        from .deploy import Deployment
        from .verify import verify_live
        result = {}
        if args.command == "build":
            result = site.build()
        elif args.command == "check":
            result = site.check()
            audit.artifact("local-site-check.json", result)
        elif args.command == "architecture":
            result = Toolbox(config, audit, cloud=False).architecture()
        elif args.command == "plan":
            result = Deployment(config, audit).plan(args.stage)
            path = audit.path / f"plan-{args.stage}.json"
            print(f"Review plan: {path}")
            print(f"Apply: python -m cloudcrew apply --plan '{path}'")
        elif args.command == "apply":
            result = Deployment(config, audit).apply(json.loads(args.plan.read_text()))
        elif args.command == "deploy":
            deployment = Deployment(config, audit)
            result = deployment.apply(deployment.plan(args.stage))
        elif args.command == "status":
            config.validate(cloud=True)
            result = Deployment(config, audit).status()
            audit.artifact("infrastructure-status.json", result)
        elif args.command == "verify":
            result = verify_live(config, audit)
        elif args.command == "dns":
            from .dns import diagnose_dns
            result = diagnose_dns(config, audit)
        elif args.command == "wait":
            result = Deployment(config, audit).wait_until_ready(args.timeout)
        elif args.command == "exercise":
            from .exercise import run_exercise
            result = run_exercise(config, audit, args.timeout)
        elif args.command == "agents":
            result = workflow(config, audit, args.mode, args.stage, args.interactive_approvals, args.request)
        elif args.command == "bundle":
            from .submission import bundle_logs
            result = bundle_logs(args.run)
        elif args.command == "logs":
            from .log_submission import prepare_log_submission
            result = prepare_log_submission(config, args.draft)
        elif args.command == "report":
            from .submission import build_report
            result = build_report(config)
        audit.emit("system", "run_completed", {"command": args.command, "result": result})
        print(json.dumps(result, indent=2))
        print(f"Execution records: {audit.path}")
        if result.get("status") in ("failed", "blocked", "incomplete"):
            raise SystemExit(1)
    except (ValueError, RuntimeError, PermissionError, OSError, KeyError) as exc:
        from .core import redact
        message = redact(str(exc))
        if audit:
            audit.emit("system", "run_failed", {"error": message})
            print(f"Execution records: {audit.path}", file=sys.stderr)
        print(f"Error: {message}", file=sys.stderr)
        raise SystemExit(1) from None
