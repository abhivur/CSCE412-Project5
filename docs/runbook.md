# Deployment and failure-recovery runbook

## Prerequisites and human steps

Use Python 3.10+ (on this Mac, `/opt/homebrew/bin/python3`), AWS CLI v2 and a model
available to your OpenAI API account. The application uses the Python standard
library and requires no Python package installation when run from this folder.

1. Register the instructor-approved domain with Namecheap. Do not automatically buy a domain.
2. Authenticate a project-scoped AWS CLI profile. Use the permission template in `infra/` as a starting point; attach it through the account's approved human administration process.
3. Run `python3 -m cloudcrew init` and fill in `.env` locally with the API key. Set `OPENAI_MODEL` to an available Responses tool-capable model.
4. Edit `config/project.json`: project ID, lowercase domain, unique bucket name, region and AWS profile. If reusing an existing public zone for this exact domain, set `hosted_zone_id` **before** the first deployment. Configuration changes cannot silently repoint existing state.
5. Run `python3 -m cloudcrew doctor`, `python3 -m cloudcrew build`, and `python3 -m cloudcrew check`.

Never send keys in chat or include them in video, source, configuration examples,
or log archives. AWS credentials are supplied through the AWS CLI credential
chain, rather than stored by this project.

## Real agent execution

```bash
python3 -m cloudcrew agents --mode local
```

This calls real models for planning, website work, architecture planning and
local verification. It is billable API use. There is no simulated agent mode.
The website agent may revise page introductions; check the resulting changes.

For agent-assisted deployment, use the following for each stage:

```bash
python3 -m cloudcrew agents --mode deploy --stage foundation --interactive-approvals
```

The infrastructure agent can present the plan and wait for a human to type the
exact displayed approval phrase. Omitting `--interactive-approvals` produces
a plan without executing cloud writes. A verification failure before all stages
finish is expected and is recorded as a failure, never claimed as a pass.

## Deployment stages

For a single terminal command that prepares a plan and asks for the same exact
approval before applying it, use `python3 -m cloudcrew deploy --stage STAGE`.
It retains the saved plan, state checks, and approval records. The separate
`plan` and `apply` commands remain available for reviewing a plan in advance.

Each stage also has a deterministic CLI path for review, retries, and explaining
what the agent's tools do. Use actual paths printed by the planning command:

```bash
python3 -m cloudcrew plan --stage foundation
python3 -m cloudcrew apply --plan runtime/runs/RUN_ID/plan-foundation.json
python3 -m cloudcrew status
```

The foundation creates or reuses this deployment's private bucket, hosted zone,
DNS-validated ACM certificate in `us-east-1`, and always-signing OAC. After it
finishes, copy the four `nameservers` from the recorded state/status into
Namecheap's custom nameservers setting. This is a human domain-ownership action.

```bash
python3 -m cloudcrew plan --stage certificate-dns
python3 -m cloudcrew apply --plan runtime/runs/RUN_ID/plan-certificate-dns.json
python3 -m cloudcrew status
```

The second stage adds the ACM validation CNAME. If ACM has not supplied its record,
retry planning later. Wait for the certificate to become `ISSUED`; delegation
and DNS validation are asynchronous. A certificate that remains pending usually
requires checking the delegated nameservers and validation record.

```bash
python3 -m cloudcrew plan --stage delivery
python3 -m cloudcrew apply --plan runtime/runs/RUN_ID/plan-delivery.json
python3 -m cloudcrew status
python3 -m cloudcrew agents --mode verify
```

Delivery uploads the files, creates CloudFront, scopes S3 reads to its distribution
ARN, writes apex A/AAAA aliases, and invalidates cache. Wait for CloudFront to be
`Deployed` and DNS to resolve. Run verification again after propagation. Each
invocation is bounded; the tool does not hide propagation behind indefinite waits.

To publish later website changes:

```bash
python3 -m cloudcrew build
python3 -m cloudcrew check
python3 -m cloudcrew plan --stage publish
python3 -m cloudcrew apply --plan runtime/runs/RUN_ID/plan-publish.json
python3 -m cloudcrew agents --mode verify
```

No bucket synchronization uses `--delete`. Website uploads and an explicit
invalidation are recorded. Old remote objects are not automatically removed.
Keep `runtime/state.json` locally: it contains IDs and recovery material, not
credentials. Back it up securely, and do not run concurrent deployments.

## Controlled failure and approved recovery

The complete workflow can be run with one command after deployment:

```bash
python3 -m cloudcrew exercise
```

This runs real verification and diagnosis agents, waits for CloudFront deployment
and invalidation at each phase, and asks for approval before introducing the
failure and before restoring the saved configuration. It refuses to introduce
a failure unless the live baseline passes. Recovery still requires measured
passing checks; a restored configuration alone does not establish completion.
If the model/API fails after the failure, the script presents the deterministic
recorded restoration for human approval. A denied approval is not requested
again in the same run. If interrupted, run `python3 -m cloudcrew deploy --stage
recovery` when ready, then wait and verify. Keep this terminal session open until
the website is restored. No unit-test fixture is used by this command.

The separate commands below expose each step for review or a shorter recording.

If the baseline fails on DNS resolution, run `python3 -m cloudcrew dns` first.
It records the Mac/system lookup, public recursive replies, the authoritative
Route 53 reply, parent nameserver delegation and available DNSSEC evidence.
It changes no DNS records, resolver settings or hosts files. Inspect
`dns-diagnostic.json` in that run before proposing a correction. An authoritative
answer alone does not prove public or local resolution; a failing baseline remains
a failure until normal live access succeeds.

First obtain a fully passing live verification. Use only this project's tagged
distribution. The failure stage refuses to operate without a recorded baseline.

```bash
python3 -m cloudcrew agents --mode verify
python3 -m cloudcrew plan --stage failure
python3 -m cloudcrew apply --plan runtime/runs/RUN_ID/plan-failure.json
python3 -m cloudcrew status
```

The real failure changes the default root document to a confirmed nonexistent
key. The full original distribution configuration is saved **before** the update.
After CloudFront deploys, the root URL returns 404 while `/index.html` remains
available. A unique missing key and cache invalidation make the effect observable.

```bash
python3 -m cloudcrew agents --mode diagnose
```

This runs measured verification, passes its results to the diagnosis agent, and
generates a corrective plan. Show the recorded diagnosis and plan in the demo.
Then approve the saved recovery plan yourself:

```bash
python3 -m cloudcrew apply --plan runtime/runs/RUN_ID/plan-recovery.json
python3 -m cloudcrew status
python3 -m cloudcrew agents --mode verify
```

Alternatively, the diagnosis workflow accepts `--interactive-approvals` so the
agent can request the same human approval before applying the restoration.
Recovery restores the recorded configuration using an ETag; unrelated external
changes cause refusal rather than an automatic overwrite. Save the failing and
passing runs, approval, AWS update, and diagnosis together.

## Submission and costs

```bash
python3 -m cloudcrew report
python3 -m cloudcrew bundle --run RUN_ID_1 --run RUN_ID_2
```

The report generator labels unperformed cloud or AI work as pending. Add your
name, repository URL, actual screenshots, personal explanations and lessons
learned before submission. Read both cloud and agent diagrams in `architecture.md`.
The ZIP contains selected real execution records and a chronological index;
review it for sensitive data before submission. Unit-test fixtures are never
included as agent evidence.

Monitor AWS account billing in the console and LLM usage/billing in the provider
dashboard. Logs record API token usage; website traffic and AWS costs are not
automatically queried because additional service APIs are outside this project's
tool allowlist. Avoid repeated distribution/zone creation and unnecessary
invalidations. Keep the website online through grading. Resource teardown is a
separate human-controlled activity after grading; agents have no deletion tools.
