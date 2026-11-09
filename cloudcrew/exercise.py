"""A real controlled failure exercise with measured baselines and approved recovery."""
from __future__ import annotations

from .agents import Agent, Toolbox
from .core import Audit, Config, digest
from .llm import ResponsesModel
from .verify import observed_default_document_failure


def run_exercise(config: Config, audit: Audit, timeout: int = 900) -> dict:
    config.validate(cloud=True)
    model = ResponsesModel(config["model"])
    toolbox = Toolbox(config, audit, cloud=True, interactive=True, stage="recovery")
    deployment = toolbox.deployment
    if deployment.state.data.get("failure_active"):
        raise ValueError("An earlier exercise is still active. Run 'python3 -m cloudcrew deploy --stage recovery' before starting another.")
    deployment.wait_until_ready(timeout)

    def invoke(role, task, upstream, verification_goal="healthy_deployment"):
        message = {"sender": "verification" if role == "diagnosis" else "orchestrator", "recipient": role,
                   "message_id": digest({"run": audit.run_id, "sequence": audit.seq, "role": role})[:16],
                   "task": task, "context": {"mode": "controlled_failure_exercise", "stage": "recovery", "interactive_approvals_enabled": True,
                                              "verification_goal": verification_goal},
                   "upstream": upstream}
        audit.emit("orchestrator", "agent_message", message)
        print(f"[{role}] {task}", flush=True)
        try:
            result = Agent(role, config, audit, toolbox, model).run(message)
        finally:
            # Preserve measured evidence even if a later model call or completion fails.
            if verification_goal == "observe_controlled_failure" and toolbox.last_verification and observed_default_document_failure(toolbox.last_verification):
                audit.artifact("exercise-failure.json", toolbox.last_verification)
        print(f"[{role}] {result['status']}: {result['summary']}", flush=True)
        return result

    print("Phase 1/4: Establish a passing live baseline.", flush=True)
    baseline_agent = invoke("verification", "Run verify_deployment to establish a fully passing live baseline before introducing any failure.", {})
    baseline = toolbox.last_verification
    if not baseline or baseline["status"] != "passed" or baseline_agent["status"] != "complete":
        raise RuntimeError("Baseline verification did not pass. No controlled failure was introduced. Resolve the recorded failures and rerun.")
    audit.artifact("exercise-baseline.json", baseline)

    failure_report, diagnosis, exercise_error = None, None, None
    try:
        print("Phase 2/4: Review and approve the controlled missing-default-document change.", flush=True)
        deployment.apply(deployment.plan("failure"))
        deployment.wait_until_ready(timeout)
        toolbox.last_verification = None
        invoke("verification", "Measure the controlled failure with verify_deployment. Compare the root URL with /index.html and report the actual failure. Your task is complete once the expected root-404/index-200 failure is observed; keep the health report failed and hand the evidence to diagnosis. Do not retry in search of a healthy deployment during this intentional-failure phase.", {"baseline": baseline}, verification_goal="observe_controlled_failure")
        failure_report = toolbox.last_verification
        if not failure_report or not observed_default_document_failure(failure_report):
            raise RuntimeError("The expected root-404 / explicit-index-200 failure was not measured. Restore the website; this is not a successful demonstration.")
        audit.artifact("exercise-failure.json", failure_report)
        print("Phase 3/4: Diagnose the real failure and approve the recorded restoration.", flush=True)
        diagnosis = invoke("diagnosis", "Inspect the infrastructure and diagnose these measured failures. Prepare the recovery plan, explain the root cause and corrective action, then call apply_plan to request human approval and restore the recorded configuration. No other stage is authorized.",
                           {"baseline": baseline, "failure": failure_report})
    except KeyboardInterrupt:
        audit.emit("orchestrator", "exercise_interrupted", {"failure_active": deployment.state.data.get("failure_active", False)})
        raise RuntimeError("Exercise interrupted. If a failure is active, restore it with: python3 -m cloudcrew deploy --stage recovery") from None
    except Exception as exc:
        if toolbox.last_verification and observed_default_document_failure(toolbox.last_verification):
            failure_report = toolbox.last_verification
        exercise_error = str(exc)
        audit.emit("orchestrator", "exercise_phase_failed", {"error": exercise_error})

    if deployment.state.data.get("failure_active"):
        if deployment.approval_denied:
            raise PermissionError("Recovery approval was denied; no further prompt will be issued. Restore the active failure when ready with: python3 -m cloudcrew deploy --stage recovery")
        # A model/API error must not prevent presenting the tested deterministic repair.
        print("The agent has not completed restoration. Review the saved-configuration recovery plan to restore the website.", flush=True)
        deployment.wait_until_ready(timeout)
        deployment.apply(deployment.plan("recovery"))
    if deployment.approval_denied:
        raise PermissionError("The exercise was not approved. No failure demonstration is claimed.")

    print("Phase 4/4: Wait for restoration, then independently verify recovery.", flush=True)
    deployment.wait_until_ready(timeout)
    toolbox.last_verification = None
    try:
        recovery_agent = invoke("verification", "Run verify_deployment after restoration. Only report complete when all live checks pass.", {"diagnosis": diagnosis or {}, "failure": failure_report or {}})
    except Exception as exc:
        exercise_error = exercise_error or str(exc)
        audit.emit("orchestrator", "recovery_agent_error", {"error": str(exc)})
        recovery_agent = {"status": "blocked", "summary": "Agent verification could not complete; deterministic recovery evidence follows."}
    recovery = toolbox.last_verification or toolbox.verify_deployment()
    audit.artifact("exercise-recovery.json", recovery)
    complete = (exercise_error is None and failure_report is not None and observed_default_document_failure(failure_report)
                and diagnosis is not None and diagnosis["status"] == "complete" and recovery["status"] == "passed"
                and recovery_agent["status"] == "complete")
    result = {"status": "passed" if complete else "failed", "baseline_artifact": baseline.get("artifact"),
              "failure_artifact": (failure_report or {}).get("artifact"), "recovery_artifact": recovery.get("artifact"),
              "failure_observed": bool(failure_report and observed_default_document_failure(failure_report)),
              "website_restored": recovery["status"] == "passed", "diagnosis": diagnosis,
              "error": exercise_error, "scope": "Actual measured operations only; failed or interrupted phases are not a completed exercise."}
    audit.artifact("exercise-result.json", result)
    return result
