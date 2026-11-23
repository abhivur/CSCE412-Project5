# GBM Cloud Project 6 demonstration

Use actual completed runs. Keep credentials and `.env` closed. Open the private repository,
CodePipeline in `us-east-1`, the public website, and the saved evidence. Prior records
support the presentation; they do not replace a working in-class demonstration.

## Run all remaining experiments

From the repository root, with the virtual environment active:

```bash
python scripts/complete_project6.py --profile default
```

The runner commits and pushes controlled defects to `main`, waits for actual pipeline
results, verifies recovery, restores source, runs real agents, packages evidence, and
refreshes the report. Do not edit the repository or start another release during it.
If interrupted, review its saved summary before running another sequence.

The results directory contains `summary.json`, mapping experiments to actual commits,
pipeline IDs, agent runs, and outcomes. Final artifacts are:

- `submission/project6-execution-evidence.zip`
- `submission/GBM_Cloud_Project6_Report.pdf`

## Opening and architecture

Say: I extended the Project 5 website with a GitHub-triggered release pipeline.
CodeBuild tests source and packages one artifact. The release job deploys that
artifact, refreshes CloudFront, checks the live website, and restores the previous
complete release when a candidate fails.

Show the report diagrams and the Source, BuildAndValidate, and Release stages.
Explain that Release implements deployment, content delivery, verification, and
recovery in one transaction.

## Successful release

Show the visible homepage change in GitHub and its Webhook-triggered execution.
Show passing tests and Release. Open the homepage and public `release.json`.
Match the source SHA to the public release ID. Use the measured test totals.
Explain that all website uploads occurred through the pipeline.

## Defect blocked before production

Open `blocked-source` from the summary. Show removal of `cloud-security.svg`,
the actual failed validation, and the absence of a Release action. Show the
independent healthy verification of the unchanged public release.

Say: The gate rejected this defect before activation. Production continued serving
the previous release. This demonstrates failure prevention.

## Production failure and rollback

Open `production-defect`. Show the hostname-specific planner bug, passing source
validation, failed production browser check, and actual failure screenshot.
Show `rolled_back` in the release result, the restored ID, passing rollback
verification, and the restored browser screenshot. The candidate pipeline must
remain Failed. Show the corrective commit and final successful release.

Say: The production browser found a real missing planner form. Recovery restored
the previous release and its assets, refreshed CloudFront, and independently
verified the restored site.

## Meaningful AI participation

Show actual `resp_` model response IDs, `agent_message` handoffs, tool calls,
evidence references, diagnosis findings, and corrective recommendations.
Explain how the diagnosis passes structured findings to recovery coordination.
Show engineering and testing tools actually executing.

For a fresh read-only agent demonstration:

```bash
python -m cloudcrew.cicd --profile default agents --mode verify
```

Say: Models choose restricted tools and interpret their observations. Deterministic
code enforces validation and release decisions. Unrestricted model-generated
infrastructure changes are not executable. Routine CI/CD needs no LLM.

## Security and final checks

Show real S3 anonymous denial, private policy, HTTPS redirect, and the final working
planner. Explain validation versus release permissions and known-good promotion.

## Submission checklist

- Upload the final PDF under Project 6 Documentation.
- Submit the private repository URL and invite the designated graders.
- Submit the actual evidence ZIP where requested.
- Submit `https://csce412abhivur.me/` as the deployed URL.
- Perform the working demonstration and keep AWS resources available for grading.

Canvas submission, grader invitations, and the presentation remain human actions.
