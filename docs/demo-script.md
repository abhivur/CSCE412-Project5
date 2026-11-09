# Demonstration outline (3–5 minutes)

Record actual behavior with readable text. Never display `.env`, credential files,
authorization headers or tokens. Execute real agents during the recording;
unit fixtures and prewritten success messages do not count as execution.

| Time | Show and explain |
| --- | --- |
| 0:00–0:30 | Name/project; custom HTTPS URL; five-page navigation; mobile layout; solution filters and launch-checklist planner |
| 0:30–1:10 | Private S3, denied anonymous object URL, CloudFront origin/OAC, ACM certificate and Route 53 records |
| 1:10–2:10 | Agent diagram/source roles; execute agents; show structured handoff, selected tools/results and model usage |
| 2:10–2:40 | Live verification: TLS, redirects, pages/assets, CloudFront headers and private origin |
| 2:40–4:20 | Controlled failure, observable effect, agent diagnosis, proposed correction, human approval and recovery verification |
| 4:20–4:45 | Actual challenge/lesson; repository, report and logs; confirm website remains online |

CloudFront propagation can exceed the video length. Clearly label time cuts,
preserving actual timestamps and logs before/after. If exactly three minutes is
required, shorten narration while retaining the required evidence.

Representative commands:

For one continuous real failure-and-recovery session, run
`python3 -m cloudcrew exercise`. Approve each displayed change separately; the
command records the baseline, actual failure, agent diagnosis and recovery.

```bash
python3 -m cloudcrew agents --mode local
python3 -m cloudcrew agents --mode verify
python3 -m cloudcrew plan --stage failure
python3 -m cloudcrew apply --plan ACTUAL_FAILURE_PLAN_PATH
python3 -m cloudcrew agents --mode diagnose
python3 -m cloudcrew apply --plan ACTUAL_RECOVERY_PLAN_PATH
python3 -m cloudcrew agents --mode verify
```

Use real paths printed by the commands. Begin with a passing baseline and restore
the website before ending the demonstration.
