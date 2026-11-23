# GBM Cloud video recording guide

Author: Sai Abhinav Vurakaranam | Course: CSCE 412 | Project 5

This guide follows `context/project5-demo-video.txt`. The rubric totals **80
points** and recommends **3 to 5 minutes**. Aim for a **4:55 edited video**. Record
the actual workflows from start to finish, then shorten idle waits. Mark each
time cut, for example, “CloudFront propagation wait shortened.” Keep the real run
IDs, approvals, failure, and recovery visible. Full marks remain the grader's
decision; this plan covers every listed demonstration.

## 1. Prepare before recording

### Use the working deployment folder

Use the original **Project 5** folder for the live demonstration. It contains the
working `.env`, project configuration, and matching deployment state. The nested
`CSCE412-Project5` Git clone deliberately excludes those private files. Run the
commands below from the original folder, not from that clone:

```bash
cd '/Users/abhivur/Documents/TAMU/4th Year/Fall/CSCE 412/Project 5'
/opt/homebrew/bin/python3 -m cloudcrew doctor
/opt/homebrew/bin/python3 -m cloudcrew wait
/opt/homebrew/bin/python3 -m cloudcrew verify
```

Do this preflight before recording. Confirm the API key is configured, AWS
authentication works, and the live report actually passes, normally 23/23 checks
for the current deployment. Do not begin the fault demonstration with a failing
baseline. If DNS fails, run `python3 -m cloudcrew dns`, resolve the recorded issue,
and repeat verification. Complete AWS sign-in off camera if needed.

### Arrange your windows

- **Browser:** Open `https://csce412abhivur.me/` and prepare the five website pages.
- **AWS console:** Use the website's account, `812040916945`. Prepare the S3,
  CloudFront, ACM, and Route 53 views listed below. Loading tabs beforehand is fine;
  refresh the actual resource state when recording.
- **Code editor:** Open `cloudcrew/agents.py`, `cloudcrew/llm.py`, and
  `cloudcrew/exercise.py`. Keep `.env` and credential files closed.
- **Terminal A:** Run the actual agents and the controlled exercise here.
- **Terminal B:** Use the same original working directory for anonymous HTTP
  checks and the read-only evidence viewer. This second terminal lets you inspect
  the fault while Terminal A waits for recovery approval.

Use a clean desktop, hide notifications, enlarge terminal/editor text to about
18 to 22 points, and record at 1080p or better. Check a short microphone sample.
Keep secret keys, authorization headers, sign-in tokens, and credential files out
of the recording. Resource IDs and model response IDs are not credentials.

### Prepare these AWS resource views

| Service | Resource to show | Visible evidence |
| --- | --- | --- |
| S3 | `csce412abhivur-me-project5` | HTML files/assets under Objects; all four Block Public Access settings enabled under Permissions |
| CloudFront | `E1YB5Q5M7QMQA9` | Custom domain, regional S3 REST origin, and attached OAC |
| OAC | `E2CYAZF3BPI52` | Always-sign behavior and SigV4 signing |
| ACM | Certificate for `csce412abhivur.me` in `us-east-1` | Issued status and matching domain |
| Route 53 | Public zone `csce412abhivur.me` | Apex A/AAAA aliases to `d110ckj7cjf58d.cloudfront.net` and certificate-validation CNAME |

Do not change console settings for this tour. The controlled fault uses the
approved CLI workflow later.

## 2. Timeline and spoken script

Read the quoted narration naturally. Follow the actions beside it. Pauses spent
waiting for cloud propagation or a model response are additional raw recording
time; shorten those waits in the edit rather than rushing the explanation.

| Edited time | Scene | Rubric points |
| --- | --- | --- |
| 0:00 to 0:15 | Introduction | 5 |
| 0:15 to 0:55 | Five pages, interaction, responsive layout | 10 |
| 0:55 to 1:50 | Actual AWS infrastructure and security | 15 |
| 1:50 to 2:55 | Agent source, real execution, structured handoff, tools | 25 |
| 2:55 to 4:40 | Real fault, diagnosis, approvals, verified recovery | 20 |
| 4:40 to 4:55 | Conclusion and lesson | 5 |

### Scene 1: Introduction, 0:00 to 0:15

**Show:** The homepage with its custom HTTPS address visible.

**Say:**

> I'm Sai Abhinav Vurakaranam, and this is GBM Cloud for CSCE 412 Project 5. It
> combines a five-page learning website with five specialized AI agents. I'll
> demonstrate the website, secure AWS delivery, live agent communication, and an
> approved failure and recovery.

### Scene 2: Website functionality, 0:15 to 0:55

**Do:**

1. Start on **Home**. Click **Solutions**, choose a category and difficulty, and
   show that the cards change. Reset the filters.
2. Click **Resources**, then **About**, then **Launch planner**. This visibly
   visits all five pages, rather than only listing their names.
3. In the planner, enter `Demo Launch`, build a checklist, and check one item.
   Refresh to show persistence. Click **Download checklist** and show the download
   starts. Close or minimize the download prompt before continuing.
4. Switch the browser's responsive mode to roughly 390 pixels wide. Open and close
   the mobile navigation and briefly show the stacked planner layout. Return to
   desktop width.

**Say:**

> The five pages share navigation and a responsive maroon-and-white design.
> Solutions can be filtered, and the launch planner creates a personalized
> checklist. Completion persists in this browser, and the checklist can be
> downloaded. At mobile width, the navigation and content adapt to the screen.
> These features run entirely in client-side JavaScript.

### Scene 3: AWS and security, 0:55 to 1:50

**Do:** Use the prepared resource tabs in this order:

1. Show S3 **Objects** with the website files, then **Permissions** with public
   access blocked. Briefly show the policy scoped to the CloudFront distribution.
2. Show the CloudFront distribution's S3 origin and OAC attachment, then its
   always-sign/SigV4 setting. The REST origin is important, not a public S3 website
   endpoint.
3. Show the ACM certificate's **Issued** status and domain, with `us-east-1`
   selected. Show Route 53's A/AAAA aliases and validation CNAME.
4. In Terminal B, execute the following real requests. Keep the command URL and
   output readable:

```bash
curl -sS -o /dev/null -w 'Anonymous S3 HTTP %{http_code}\n' \
  https://csce412abhivur-me-project5.s3.us-east-1.amazonaws.com/index.html

curl -sSI http://csce412abhivur.me/
```

The first request is unsigned and should return **403**. The second should show
**301** and `location: https://csce412abhivur.me/`. Do not add `-L`, which would
follow the redirect and hide the initial response. The browser may automatically
upgrade HTTP because of HTTPS protections, so use this terminal request as the
redirect evidence. The HTTPS browser address is already visible in Scene 2.

**Say:**

> S3 stores the files with public access blocked. CloudFront reads them through
> always-signing Origin Access Control, and the bucket policy permits the specific
> distribution. This unsigned direct S3 request returns 403. ACM supplies the
> issued domain certificate in us-east-1, while Route 53 points the domain to
> CloudFront. HTTP returns a 301 redirect to HTTPS.

### Scene 4: Agent architecture and live execution, 1:50 to 2:55

**Show source:**

- In `cloudcrew/agents.py`, find `ROLES` and `choices`. Point to all five role
  definitions and their tool allowlists.
- In `cloudcrew/llm.py`, show `ResponsesModel.respond`, the model and function
  tools in the request payload, and `validate`.
- In `cloudcrew/agents.py`, show `Agent.run` selecting and validating calls,
  executing `tool.function(**args)`, and returning `function_call_output`.
- Find `message =` in `workflow` to show task IDs, context, and upstream results.

**Say while showing source:**

> The planner decomposes requirements. The website agent builds and checks the
> site. Infrastructure prepares restricted plans, verification measures behavior,
> and diagnosis analyzes failures. OpenAI gpt-4.1-mini selects allowed tools and
> produces structured decisions. Python validates the arguments, executes trusted
> functions, and returns observations. Cloud writes require human approval.

**Run in Terminal A during the recording:**

```bash
/opt/homebrew/bin/python3 -m cloudcrew agents --mode local \
  --request 'Inspect the existing GBM Cloud implementation. Preserve page introductions. Assign website build/check, an offline infrastructure architecture specification, and local verification. Do not publish or request AWS changes.'
```

This runs the planner, website, infrastructure, and verification agents through
real API calls. If a role is blocked, it may invoke diagnosis. Record the start
and the actual role outputs. Shorten quiet API waits in the edit, keeping the
sequence intact. The local website tools can change local source if selected by
the model; review any edits afterward. Local mode does not publish to AWS.

Once the command ends, show the data it just created:

```bash
/opt/homebrew/bin/python3 scripts/demo_evidence.py --latest agents --section handoffs
/opt/homebrew/bin/python3 scripts/demo_evidence.py --latest agents --section tools
```

**Point out:**

- A planner-to-website or planner-to-infrastructure message with `sender`,
  `recipient`, `message_id`, and upstream planner findings.
- `build_site` and `check_site` requests and their actual results. These satisfy
  the meaningful executable-tool demonstration, rather than a printed promise.
- A model name/response ID and a corresponding tool call/result pair, linked by
  `call_id`. Scroll to those records instead of reading the whole output.

**Say:**

> These agents have just executed. This handoff carries the planner's structured
> output into the next task. Here are the website build and check requests and
> their recorded results. The response IDs identify actual model calls. This
> read-only viewer formats the new run's evidence; it does not execute agents or
> manufacture results. Local architecture planning does not deploy AWS resources.

The fifth role executes on the real cloud failure in the next scene. Do not use
old runs as a substitute for the fresh execution shown in this recording.

### Scene 5: Failure and recovery, 2:55 to 4:40

**Run in Terminal A:**

```bash
/opt/homebrew/bin/python3 -m cloudcrew exercise
```

This is a real AWS experiment. Use the following recording sequence, including
both approvals:

1. **Baseline:** Show Phase 1 and the successful live verification. Do not approve
   a fault unless the command itself establishes a passing baseline.
2. **Fault plan and first approval:** Show the plan changing `DefaultRootObject`
   from `index.html` to `controlled-missing-...html`. Type the exact `approve`
   phrase displayed by this fresh run. Never copy a digest from an older run or
   from this guide. Record that approval before the update.
3. **Wait:** Keep recording until CloudFront deployment and cache invalidation
   complete. Label any shortened waiting interval in the edit.
4. **Hold at recovery approval:** The command automatically measures the fault
   and invokes diagnosis. When it reaches the **second approval prompt**, leave
   it waiting. At this point, the fault is still active, diagnosis has inspected
   the infrastructure and prepared a recovery plan, and you can demonstrate the
   failure before restoring it. Do not press Ctrl+C.
5. **Show the observable failure:** In the browser, reload the root address and
   show the custom error page. Open `/index.html` to show that explicit content
   still works. Disable browser caching or use a hard reload if necessary. In
   Terminal B, obtain status-code proof with the commands below.
6. **Show the handoff and proposed correction:** In Terminal B, use the evidence
   viewer to show the failed report passed to diagnosis and the actual recovery
   plan's before/after default document. The run header must match the current
   exercise. A final diagnosis result may not exist until you approve restoration;
   show the recorded inspection/tool requests and plan while approval is pending.
7. **Second approval:** Return to Terminal A. Show the actual plan and type its
   newly displayed recovery approval phrase. Wait for restoration and final live
   verification. Shorten only idle waiting in the edit.
8. **Prove recovery:** Show the final `status: passed`, `failure_observed: true`,
   `website_restored: true`, and `error: null`. Show the actual three phase reports,
   then reload the root website and obtain HTTP 200 again.

**At the recovery prompt, use Terminal B:**

```bash
curl -sS -o /dev/null -w 'Root HTTP %{http_code}\n' \
  https://csce412abhivur.me/
curl -sS -o /dev/null -w 'Explicit index HTTP %{http_code}\n' \
  https://csce412abhivur.me/index.html

/opt/homebrew/bin/python3 scripts/demo_evidence.py --latest exercise --section verification
/opt/homebrew/bin/python3 scripts/demo_evidence.py --latest exercise --section handoffs
/opt/homebrew/bin/python3 scripts/demo_evidence.py --latest exercise --section recovery
```

Expected during the active fault: root **404**, explicit index **200**. The
verification projection normally shows baseline **23/23** and fault **21/23**;
the recovery report will only appear after actual restoration. In the handoff,
point to `sender: verification`, `recipient: diagnosis`, and upstream
`failure.status: failed`. The recovery projection shows the real change back to
`index.html`. Use only curl and the viewer in Terminal B while the approval is
pending; do not run concurrent deployment commands.

**Say across the fault and recovery clips:**

> The live baseline passes. I approve one reversible change: CloudFront's default
> document points to a nonexistent key. The root now returns 404, while the
> explicit index still returns 200. Verification passes that measured failure to
> diagnosis. The agent inspects the real configuration and prepares restoration
> of the saved working configuration. I approve the exact recovery plan. After
> deployment and invalidation, independent verification passes and the root works
> again.

**After the exercise finishes, use Terminal B:**

```bash
/opt/homebrew/bin/python3 scripts/demo_evidence.py --latest exercise --section verification
/opt/homebrew/bin/python3 scripts/demo_evidence.py --latest exercise --section recovery
curl -sS -o /dev/null -w 'Restored root HTTP %{http_code}\n' \
  https://csce412abhivur.me/
```

Show the actual measured counts. **23/23, 21/23, and 23/23** are the current
expected counts, not text to overlay if the new run reports something different.
The two failed checks during the fault should be the root page and default-root
configuration. All normal live health checks must pass after restoration.

### Scene 6: Conclusion, 4:40 to 4:55

**Show:** The restored HTTPS homepage, followed briefly by the repository URL.

**Say:**

> GBM Cloud demonstrates a responsive website, a private S3 origin, secure HTTPS
> delivery, and five coordinated agents using real tools. The controlled failure
> was diagnosed and restored after approval. A key lesson was separating a
> successfully observed fault from a healthy deployment, while requiring measured
> verification before declaring recovery complete.

## 3. Recording and editing order

1. Prepare the console tabs and run the off-camera preflight.
2. Start recording before the introduction and website tour.
3. Record the actual source walkthrough, local agent command, and new evidence.
4. Record the complete exercise, both approvals, and observable fault/recovery.
5. Record the conclusion after the website has actually recovered.
6. Trim loading and idle waits to make the final cut around five minutes. Add a
   short visible label for each time cut. Preserve the ordering and the same run
   IDs. Do not replace current execution with the earlier successful log run.
7. Watch the export once with sound and verify all required proof is readable.

The raw recording can be much longer than five minutes because CloudFront is
asynchronous. The assignment's duration is a recommendation. Preserve complete
evidence if a small overrun is necessary rather than removing a required item.

## 4. If something fails during recording

- **No matching viewer run:** Execute the actual workflow first, and ensure both
  terminals are in the original working folder. `--latest` reads records under
  that folder only. If necessary, use `--run` with the exact run ID printed by
  the workflow to select it explicitly.
- **Baseline failure:** Do not introduce a fault. Resolve the recorded issue and
  obtain a healthy baseline before trying again.
- **Root appears cached:** After the program reports readiness, use the anonymous
  curl checks and a hard browser reload. Do not claim a 404 if you only observe
  200, or recovery if the root still fails.
- **Model error after fault injection:** Review and approve the saved recovery
  plan offered by the command. If the final exercise says `failed`, keep that
  result honest. Restore and verify first, then record a fresh complete run.
- **Interrupted exercise:** Restore with the commands below and wait for passing
  verification before retaking the scene. Do not start a second fault while one
  is active.

```bash
/opt/homebrew/bin/python3 -m cloudcrew deploy --stage recovery
/opt/homebrew/bin/python3 -m cloudcrew wait
/opt/homebrew/bin/python3 -m cloudcrew verify
```

This guide does not authorize skipping the application's approval prompts.
Successful repair is supported by observed HTTP behavior and a passing final
report, not just a successful AWS configuration update.

## 5. Final rubric checklist

- [ ] **Introduction, 5 points:** Full name, CSCE 412 Project 5, website and agent
  purpose, and demo scope are stated.
- [ ] **Website, 10 points:** The custom HTTPS domain is visible; Home, Solutions,
  Resources, About, and Launch planner are actually visited; responsive behavior
  is shown. Filtering and planner interaction are demonstrated.
- [ ] **AWS/security, 15 points:** Actual S3 objects and private settings, unsigned
  object access returning 403, CloudFront distribution and S3/OAC origin, issued
  ACM certificate, Route 53 records, HTTPS, and the HTTP redirect are visible.
- [ ] **Agents, 25 points:** At least four roles are explained in actual source;
  the model's role is explained; real execution occurs on camera; a structured
  handoff, tool arguments, and actual tool results from that run are readable.
- [ ] **Failure/recovery, 20 points:** The fault is approved and introduced, its
  effect is measured, the diagnosis workflow runs, the correction is shown and
  approved, and restored functionality is independently verified.
- [ ] **Conclusion/presentation, 5 points:** A real challenge or lesson is stated;
  narration, text, and transitions are clear. Any time cuts are transparent.
- [ ] No credentials are exposed, and the site is restored before the recording
  ends.

## 6. Submit the video

Export a readable video, for example `GBM_Cloud_Project5_Demo.mp4`. Canvas accepts
a video file or a shareable URL according to the available submission options.
If submitting a URL, check its viewing permissions for the teaching staff before
submitting. Keep the video and deployed website available through grading. A
GitHub link, source walkthrough, or old logs alone do not replace the live demo.
