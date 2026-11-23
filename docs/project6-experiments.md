# Project 6 experiment and demonstration plan

Run these experiments only after setup, baseline adoption, and the GitHub webhook
are working. They affect the real website. Keep the previous known-good release,
collect actual evidence, and wait for each execution to finish before the next
push. Record source commit hashes, pipeline execution IDs, build results, public
release IDs, and recovery results. No manual file uploads to S3 are used.

The assignment lists two experiments in Part H but also requires post-deployment
failure and recovery in its report, evidence, and in-class demonstration. Complete
all three below. Historical Project 5 recovery is not a substitute.

## 1. Successful automatic release

1. Record the public `release.json` and current page content.
2. Edit the Home introduction in `website/content.json` to include a short visible
   change, such as the date or a release announcement. Preserve valid JSON.
3. Commit the change and push the designated `main` branch yourself.
4. Show the push/source commit and the automatically started CodePipeline
   execution. Do not use Start execution as the proof of the GitHub trigger.
5. Show BuildAndValidate passing and Release activating the candidate, invalidating
   the cache, and verifying actual production behavior.
6. Show the public changed text and release identifier matching the pushed commit.
   The previous release remains archived and recorded in private state.

Expected: pipeline succeeds; the public site identifies and serves the new commit.

## 2. Defect blocked before production

1. Record the known-good production release ID and save the good source commit.
2. Deliberately remove `website/assets/cloud-security.svg` from the source using
   Git. This breaks an actual required asset reference.
3. Commit and push this defective source revision.
4. Show the mandatory test/static check failing with the missing asset evidence.
5. Show that Release did not run and public `release.json` still identifies the
   earlier approved commit. Open the public website to confirm it remains usable.
6. Run the diagnosis agents against this actual failed execution and show a
   handoff containing its real test results, affected commit, and failed stage.
7. Restore the file from the good commit, commit the correction, and push. Wait
   for the corrected pipeline to pass before the next experiment.

Expected: the gate rejects the source, no production activation occurs, and the
previous website remains available. Do not create a fake failed log or just
print an error; the required asset is actually missing from the tested artifact.

## 3. Production-only failure and automatic rollback

This controlled bug represents a production-specific feature condition that
local structural and Node unit tests do not exercise. Actual browser verification
must catch it. It does not turn off tests or force a failure status.

1. Start from a clean, known-good revision. Record public release identity and
   planner behavior.
2. Append this deliberate bug to `website/assets/app.js`:

```javascript
// Demo-only bug: the production condition hides the planner form.
if (typeof window !== "undefined" && window.location &&
    window.location.hostname === "csce412abhivur.me") {
  document.addEventListener("DOMContentLoaded", () => {
    const form = document.getElementById("launch-planner");
    if (form) form.remove();
  });
}
```

3. Commit and push the defect. Show pre-deployment tests passing for this commit,
   then the real candidate being deployed.
4. Show the production browser planner check failing. The private
   `production-verification.json` contains the actual failed browser measurement,
   not a fabricated status. The defect can be brief because recovery is automatic;
   retain pipeline logs and browser failure evidence rather than trying to keep
   the site broken for a screenshot.
   The verification script automatically captures the actual failed browser page
   and retains it with the run's evidence for the report.
5. Show the bounded rollback switching to the previously verified complete release,
   its cache invalidation, and passing rollback verification.
6. Show CodePipeline remaining **Failed**, the release result reporting
   `rolled_back`, and public `release.json` matching the earlier known-good commit.
   Show the restored planner working in the browser.
7. Run diagnosis for the failed execution. Show actual failed check evidence,
   source commit and component, the production-only condition as the likely cause,
   and a recommendation to remove the bug and improve pre-deployment coverage.
8. Remove the appended bug, commit the fix, and push. Finish with a healthy public
   website and a successful corrected release. Keep all failed/recovery evidence.

Expected: a real failure after activation, complete automatic rollback, passing
verification of the previous version, and an honestly failed candidate execution.
Do not manually upload an old folder, promote the failed release as known-good,
or describe a source gate failure as a production rollback.

## In-class demonstration

- Explain the extended agent roles, programmatic handoffs, model decisions, and
  restricted executable tools. Run real agents, not unit fixtures.
- Show the actual pipeline configuration/buildspecs and quality gate.
- Show a fresh GitHub push triggering a successful release.
- Show the deliberate pre-deployment defect being blocked with production unchanged.
- Show the production-only defect being detected and rolled back as a complete
  release, with the restored public ID and functional planner.
- Show agent diagnosis using actual logs and an execution ID from a failed run.
- Keep text readable and credentials closed. Preserve actual timestamps and run
  IDs if shortening asynchronous waits in a recording.

## Submission checklist

- Private GitHub repository contains final executable code and actual configuration
  generation/buildspecs, with designated graders invited.
- Nine-section PDF report covers overview, CI/CD architecture, multi-agent
  architecture, automated testing, release management, recovery, all three actual
  experiments, AI usage/decisions, and lessons/limitations.
- Execution evidence includes push triggers, tests, deploy, live verification,
  rejected source, failed production checks, rollback, and real agent handoffs.
- In-class demonstration shows actual execution.
- Submit the existing HTTPS website URL and keep the relevant resources available
  through grading. Missing live experiments remain pending until actually run.
