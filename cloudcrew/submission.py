"""Create submission drafts from actual recorded evidence, never synthetic agent logs."""
from __future__ import annotations

import hashlib
import html
import json
import re
import textwrap
import zipfile
from pathlib import Path

from .core import ROOT, Config, now, redact, write_json

REFERENCES = [
    ("AWS: Restrict access to an S3 origin (OAC)", "https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/private-content-restricting-access-to-s3.html"),
    ("AWS: CloudFront certificate requirements", "https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/cnames-and-https-requirements.html"),
    ("AWS: Custom error responses", "https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/custom-error-pages-procedure.html"),
    ("AWS: Route 53 record permissions", "https://docs.aws.amazon.com/Route53/latest/DeveloperGuide/specifying-conditions-route53.html"),
    ("OpenAI: Function calling", "https://developers.openai.com/api/docs/guides/function-calling"),
    ("Namecheap: Change domain DNS", "https://www.namecheap.com/support/knowledgebase/article.aspx/767/10/how-to-change-dns-for-a-domain/"),
]

DIAGRAMS = {
    "2.": [["Visitor", "CloudFront / HTTPS", "Private S3 / signed OAC"],
           ["Namecheap delegation", "Route 53 / A + AAAA", "CloudFront domain"],
           ["ACM / us-east-1", "CloudFront certificate"]],
    "3.": [["Planner", "Website", "Infrastructure"],
           ["Measured verification", "Failure diagnosis", "Human-approved recovery"]],
}


def diagram_svg(rows: list[list[str]]) -> str:
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Architecture data flow" viewBox="0 0 840 {len(rows) * 85}">']
    for row, labels in enumerate(rows):
        width = (840 - (len(labels) - 1) * 40) / len(labels)
        for column, label in enumerate(labels):
            x, y = column * (width + 40), row * 85
            parts.append(f'<rect x="{x}" y="{y + 5}" width="{width}" height="55" rx="4" fill="#edf1e7" stroke="#7b8a76"/>')
            parts.append(f'<text x="{x + width / 2}" y="{y + 37}" text-anchor="middle" font-size="14" fill="#263d32">{html.escape(label)}</text>')
            if column < len(labels) - 1:
                end = x + width + 35
                parts.append(f'<path d="M{x + width + 5} {y + 32}H{end}m-6-4 6 4-6 4" fill="none" stroke="#263d32"/>')
    parts.append('</svg>')
    return ''.join(parts)


def runs() -> list[Path]:
    root = ROOT / "runtime/runs"
    return sorted([p for p in root.iterdir() if p.is_dir() and re.fullmatch(r"\d{8}T\d{6}-[a-f0-9]{8}", p.name)]) if root.exists() else []


def collect_evidence() -> dict:
    result = {"local": None, "live": None, "dns": None, "agents": {}, "failures": [], "recovery": [], "approvals": [], "run_count": 0}
    for run in runs():
        events = run / "events.jsonl"
        if not events.exists():
            continue
        result["run_count"] += 1
        for line in events.read_text().splitlines():
            event = json.loads(line)
            data = event.get("data", {})
            if event["event"] == "agent_completed":
                result["agents"][event["agent"]] = {"run": run.name, "result": data}
            if event["event"] == "approval_granted":
                result["approvals"].append({"run": run.name, **data})
            if event["event"] == "operation_completed" and data.get("operation") == "introduce_failure":
                result["failures"].append(run.name)
            if event["event"] == "operation_completed" and data.get("operation") == "restore_configuration":
                result["recovery"].append(run.name)
        for name, key in [("local-site-check.json", "local"), ("verification.json", "live"), ("dns-diagnostic.json", "dns")]:
            artifact = run / name
            if artifact.exists():
                result[key] = {"run": run.name, "artifact": str(artifact.relative_to(ROOT)), "result": json.loads(artifact.read_text())}
    return result


def bundle_logs(selected: list[str]) -> dict:
    available = {run.name: run for run in runs()}
    available = {name: run for name, run in available.items() if (run / "events.jsonl").exists()
                 and any(json.loads(line).get("event") in ("run_completed", "run_failed")
                         for line in (run / "events.jsonl").read_text().splitlines())}
    ids = selected or list(available)
    if not ids:
        raise ValueError("No actual execution records exist yet")
    if any(run_id not in available for run_id in ids):
        raise ValueError("Select existing run IDs from runtime/runs; paths are not accepted")
    output = ROOT / "submission/project5-agent-logs.zip"
    output.parent.mkdir(exist_ok=True)
    index = []
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for run_id in sorted(set(ids)):
            for path in sorted(available[run_id].iterdir()):
                if path.suffix not in (".json", ".jsonl") or not path.is_file() or path.is_symlink():
                    continue
                if path.suffix == ".jsonl":
                    value = "\n".join(json.dumps(redact(json.loads(line))) for line in path.read_text().splitlines()) + "\n"
                else:
                    value = json.dumps(redact(json.loads(path.read_text())), indent=2) + "\n"
                name = f"project5-agent-logs/{run_id}/{path.name}"
                archive.writestr(name, value)
                index.append({"file": name, "sha256": hashlib.sha256(value.encode()).hexdigest()})
        readme = """Project 5 actual execution records

Runs are ordered by UTC start time in their directory names. events.jsonl contains
timestamped, sequential task assignments, agent messages, model usage, requested
tools, real tool results, approvals, failures and completion events where executed.
JSON artifacts contain plans, structured agent decisions and measured checks.

Local build/check records are not evidence of live deployment or LLM execution.
A missing agent, failure, recovery or live verification record means that work
has not been demonstrated in these runs. Unit-test fixtures are excluded.
Known credential patterns and configured environment secrets are redacted during
capture and export. Review this archive before submission.
"""
        archive.writestr("project5-agent-logs/README.txt", readme)
        archive.writestr("project5-agent-logs/manifest.json", json.dumps(index, indent=2))
    return {"status": "created", "path": str(output), "runs": sorted(set(ids)), "files": len(index),
            "note": "Logs represent only the actual actions recorded in these runs; review for submission completeness and sensitive data."}


def report_sections(config: Config, evidence: dict, metadata: dict) -> list[tuple[str, list[str]]]:
    local = evidence["local"]
    live = evidence["live"]
    local_text = f"Local static checks: {local['result']['status']}; evidence: {local['artifact']}." if local else "Local static checks: PENDING."
    live_text = (f"Latest live verification: {live['result']['status']}, {live['result']['passed']}/{live['result']['total']} checks; evidence: {live['artifact']}."
                 if live else "Live deployment, HTTPS, DNS, private-origin access and CloudFront delivery evidence: PENDING. No cloud success is claimed.")
    agent_text = "; ".join(f"{role}: {entry['run']} ({entry['result']['status']})" for role, entry in evidence["agents"].items()) or "PENDING: no real LLM agent completion records have been captured."
    failure_text = (f"Recorded failure operations: {evidence['failures']}. Recorded restoration operations: {evidence['recovery']}."
                    if evidence["failures"] else "PENDING: a real controlled failure and recovery have not yet been recorded.")
    dns = evidence.get("dns")
    dns_text = (f"Observed DNS troubleshooting: {dns['result']['diagnosis']['classification']}. "
                f"{dns['result']['diagnosis']['finding']} Evidence: {dns['artifact']}. "
                "The measured baseline remains failed until normal DNS and live endpoint verification succeed."
                if dns else "No separate DNS diagnostic has been recorded.")
    measured = []
    if live:
        for check in live["result"]["checks"]:
            observed = check.get("observation", {})
            status = f"; HTTP {observed['status_code']}" if isinstance(observed, dict) and "status_code" in observed else ""
            measured.append(f"{'PASS' if check['passed'] else 'FAIL'}: {check['name']}{status}" + ("; " + check["error"] if "error" in check else ""))
    return [
        ("1. Project summary and website design", [
            "GBM Cloud is a student-built cloud learning website created for CSCE 412 Project 5, with a Texas A&M-inspired maroon-and-white identity. The project combines a static website with a custom executable multi-agent system for restricted AWS deployment, security verification and failure diagnosis.",
            "Five interconnected pages: Home (index.html), Solutions, Resources, About, and Launch planner. Every page shares navigation and responsive styling. A sixth page provides the custom 404 response. Original cloud architecture SVG illustrations are stored locally. Earlier page URLs remain available as compatibility pages with current branding.",
            "Client-side features include combined solution filters, mobile navigation, and a launch-checklist planner with completion tracking, browser persistence, reset and text download. Selected-solution links retain saved checks on reload; clearing also removes the selection query. The browser stores the plan on the visitor's device; no backend runs continuously.",
            f"Configured domain: https://{config['domain']}. Registration and public availability must be verified before submission. Repository: {metadata['repository_url']}", local_text]),
        ("2. Cloud architecture", [
            "Namecheap registers the domain. Route 53 provides the public hosted zone and apex A/AAAA aliases. CloudFront terminates HTTPS and serves content worldwide. ACM issues a DNS-validated certificate in us-east-1. S3 stores the static files privately.",
            "Content path: Visitor -> HTTPS CloudFront -> signed HTTPS OAC -> private S3 REST origin. DNS path: Namecheap nameserver delegation -> Route 53 -> CloudFront aliases. ACM -> CloudFront viewer certificate.",
            "The origin uses S3's REST endpoint, not its public website endpoint. CloudFront's OAC signs every request with SigV4. The bucket policy permits reads only for the recorded distribution ARN and denies insecure transport. Missing-object responses use /404.html and retain HTTP 404."]),
        ("3. Multi-agent architecture", [
            "Five specialized roles share a custom Python orchestrator: requirements/planning, website, infrastructure, verification, and diagnosis/recovery. Each role has separate system instructions and a restricted function-tool set.",
            "The planner creates structured tasks. The orchestrator sends each task with upstream results to the appropriate role, records message IDs, and passes model-selected tool observations back to that model. The verification result triggers diagnosis when blocked. A structured finish function records the final brief decision.",
            f"Configured provider: OpenAI Responses API. Configured model: {config['model']}. Actual model IDs, response IDs, token usage, tool calls and decisions are recorded when API calls execute. Recorded agent completions: {agent_text}",
            "Model reasoning interprets requirements, selects approved tools, reviews observations and diagnoses failures. Deterministic Python constructs infrastructure configurations, executes commands, enforces approvals, builds HTML, hashes files and evaluates checks. No model-generated shell commands are executed."]),
        ("4. Agent implementation and deployment process", [
            "The website agent can update bounded plain-text introductions, render the static website and check its links. The infrastructure agent produces staged plans and may request terminal approval for applying them. The verification agent executes actual anonymous HTTP/TLS requests and AWS reads. The diagnosis agent proposes restoring a recorded configuration.",
            "Deployment stages: foundation creates the private bucket, zone, certificate and OAC; certificate-dns writes the validation CNAME; delivery uploads files, creates CloudFront, installs the origin policy and writes DNS aliases; publish updates files and invalidates cache.",
            "Human steps: domain registration, account authentication, restricted permission setup, Namecheap nameserver delegation, and exact-plan approval for consequential changes. Plans bind the configuration, saved state, uploaded file hashes and relevant live configuration. Stale or edited plans are refused.",
            "Tools record real arguments and results in chronological JSONL. Cloud writes save resumable resource state. Role allowlists, argument validation, finite model-call limits and output-token limits constrain the agent loop. Failures are returned as observations for analysis rather than converted into success messages."]),
        ("5. Security implementation and verification", [
            "S3 blocks public ACLs and policies, enforces bucket ownership, and uses AES256 server-side encryption. CloudFront uses OAC, HTTPS redirection and a TLS 1.2 minimum. IAM templates restrict services and project resources; model/API keys and AWS credentials are excluded from Git and redacted from logs.",
            "Verification checks DNS, certificate hostname and chain validation, negotiated TLS, HTTP redirects, each required page, uploaded asset hashes, CloudFront headers, custom error behavior, anonymous S3 denial, public-access blocks, policy scope and actual distribution/OAC configuration.",
            live_text, *measured,
            "Screenshots for final submission: add readable browser HTTPS/certificate evidence, Route 53 records, ACM status, private S3 settings and CloudFront OAC configuration after deployment. No screenshots or cloud results are fabricated in this draft."]),
        ("6. Failure diagnosis and recovery", [
            "The controlled experiment replaces index.html as the default root document with a confirmed nonexistent object key. It preserves the complete working distribution configuration before the update. After propagation and invalidation, the root URL should return 404 while /index.html remains available.",
            "The verification agent records this real discrepancy and the actual distribution configuration. The diagnosis agent interprets the evidence and proposes a recovery plan. A human approves the exact plan. The deterministic repair restores the saved configuration using an ETag, then verification must establish recovery.",
            failure_text, f"Recorded approval receipts: {len(evidence['approvals'])}. Add the specific observed failure, diagnosis, corrective plan and passing recovery run once the experiment is complete."]),
        ("7. AI usage and engineering choices", [
            "Development assistance: OpenAI Codex generated the initial Python implementation, website generator, original code-native SVG assets, deployment tools, tests and documentation. The supplied starter agent served as an introductory reference; this implementation adds role isolation, model-selected functions, structured communication, restricted AWS actions and verification artifacts.",
            "Runtime AI: OpenAI Responses function calling provides agent reasoning. The system uses a custom orchestrator and the Python standard library rather than an external agent framework. Tool definitions and outputs are validated locally. Unit-test doubles are used only in tests and are not submitted as real agent execution.",
            "Student review required: inspect the code, run the real workflows, disclose subsequent assistance, document your own customizations and be prepared to explain or modify an agent during grading."]),
        ("8. Challenges, limitations, lessons and references", [
            "Engineering challenges addressed in the implementation include asynchronous DNS/certificate/CloudFront propagation, private S3 authorization, stale-plan prevention, API/tool failures, credential redaction, and safe deterministic rollback. These are design decisions; observed deployment challenges must be documented after actual execution.",
            dns_text,
            "Limits: local static checks do not prove browser behavior or deployed security. No real AI execution or cloud validation can be claimed without authenticated calls. The program does not buy domains, create IAM users, query additional AWS billing services or perform automatic teardown. Monitor costs manually and keep the site available through grading.",
            metadata["personal_lessons"], *[title + ": " + url for title, url in REFERENCES]]),
    ]


def simple_pdf(path: Path, title: str, subtitle: list[str], sections: list[tuple[str, list[str]]]) -> None:
    """Portable, searchable PDF draft using standard PDF fonts, without external dependencies."""
    pages = []
    operations = []
    y = 740

    def literal(value):
        return value.encode("cp1252", errors="replace").decode("latin1").replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    def flush():
        nonlocal operations, y
        if operations:
            pages.append("\n".join(operations).encode("latin1"))
        operations, y = [], 740

    def text(value, size=10, bold=False):
        nonlocal y
        width = 92 if size == 10 else 64
        for line in textwrap.wrap(value, width=width, break_long_words=True, break_on_hyphens=False) or [""]:
            if y < 60:
                flush()
            operations.append(f"BT /{'F2' if bold else 'F1'} {size} Tf 50 {y} Td ({literal(line)}) Tj ET")
            y -= size + 5
        y -= 8

    text(title, 20, True)
    for line in subtitle:
        text(line, 12)
    text("EVIDENCE-AWARE DRAFT — complete pending work and personal explanations before submission.", 12, True)
    flush()
    for heading, paragraphs in sections:
        if y < 180:
            flush()
        text(heading, 14, True)
        rows = next((value for prefix, value in DIAGRAMS.items() if heading.startswith(prefix)), [])
        for labels in rows:
            if y < 140:
                flush()
            box_width = (512 - (len(labels) - 1) * 22) / len(labels)
            for column, label in enumerate(labels):
                x = 50 + column * (box_width + 22)
                bottom = y - 38
                operations.append(f"0.93 0.95 0.9 rg {x:.2f} {bottom} {box_width:.2f} 35 re f 0.3 0.4 0.3 RG {x:.2f} {bottom} {box_width:.2f} 35 re S 0 0 0 rg")
                operations.append(f"BT /F1 8 Tf {x + 6:.2f} {bottom + 14} Td ({literal(label)}) Tj ET")
                if column < len(labels) - 1:
                    start = x + box_width + 3
                    end = start + 16
                    mid = bottom + 17
                    operations.append(f"{start:.2f} {mid} m {end:.2f} {mid} l {end - 4:.2f} {mid + 3} m {end:.2f} {mid} l {end - 4:.2f} {mid - 3} l S")
            y -= 57
        for paragraph in paragraphs:
            text(paragraph)
    flush()
    # Catalog=1, page tree=2, Helvetica=3, Helvetica-Bold=4; page/content pairs follow.
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>"]
    page_ids = []
    for index, stream in enumerate(pages):
        page_id = len(objects) + 1
        page_ids.append(page_id)
        footer = f"\nBT /F1 8 Tf 50 30 Td (GBM Cloud / CSCE 412 / Draft    Page {index + 1}) Tj ET".encode()
        stream += footer
        objects += [f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents {page_id + 1} 0 R >>".encode(),
                    f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream"]
    objects[1] = f"<< /Type /Pages /Count {len(page_ids)} /Kids [{' '.join(str(i) + ' 0 R' for i in page_ids)}] >>".encode()
    data = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f"{index} 0 obj\n".encode() + obj + b"\nendobj\n")
    start = len(data)
    data.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        data.extend(f"{offset:010d} 00000 n \n".encode())
    data.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode())
    path.write_bytes(data)


def build_report(config: Config) -> dict:
    output = ROOT / "submission"
    output.mkdir(exist_ok=True)
    metadata_path = ROOT / "config/report.json"
    if not metadata_path.exists():
        metadata_path = ROOT / "config/report.example.json"
    metadata = json.loads(metadata_path.read_text())
    evidence = collect_evidence()
    sections = report_sections(config, evidence, metadata)
    escape = html.escape
    body_parts = []
    for title, paragraphs in sections:
        rows = next((value for prefix, value in DIAGRAMS.items() if title.startswith(prefix)), [])
        body_parts.append("<section><h2>" + escape(title) + "</h2>" + (diagram_svg(rows) if rows else "")
                          + "".join("<p>" + escape(p) + "</p>" for p in paragraphs) + "</section>")
    body = "".join(body_parts)
    document = f'''<!doctype html><html lang="en"><meta charset="utf-8"><title>GBM Cloud technical report draft</title><style>
    body{{font:15px/1.65 system-ui,sans-serif;color:#263d32;max-width:900px;margin:50px auto;padding:0 30px}}h1{{font:48px/1.1 Georgia}}h2{{font-size:22px;border-bottom:1px solid #ccd4c8;padding-bottom:10px}}p{{overflow-wrap:anywhere}}.draft{{background:#f3e5bc;padding:18px}}.cover{{min-height:600px}}section{{margin:40px 0}}@media print{{.cover{{break-after:page}}h2{{break-after:avoid}}p{{orphans:3;widows:3}}body{{margin:0;font-size:11pt}}}}
    </style><div class="cover"><p>CSCE 412 / PROJECT 5</p><h1>GBM Cloud<br>Secure cloud deployment<br>with specialized AI agents</h1><p>{escape(metadata['student_name'])}<br>NetID: {escape(metadata['netid'])}</p><p>Generated {escape(now())}</p><p class="draft">Evidence-aware draft. Pending deployment, agent execution, screenshots and personal explanations must be completed before submission.</p></div>{body}</html>'''
    (output / "technical-report.html").write_text(document, encoding="utf-8")
    simple_pdf(output / "technical-report.pdf", "GBM Cloud: Secure Cloud Website Using Multi-Agent AI",
               ["CSCE 412 / Project 5", metadata["student_name"], "NetID: " + metadata["netid"], "Generated: " + now()], sections)
    write_json(output / "evidence-index.json", evidence)
    return {"status": "draft_created", "html": str(output / "technical-report.html"), "pdf": str(output / "technical-report.pdf"),
            "evidence_index": str(output / "evidence-index.json"),
            "pending": ["Student identity and repository URL", "Actual cloud and LLM evidence if not recorded", "Actual screenshots and final layout review", "Student's own lessons and final review"]}
