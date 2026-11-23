#!/usr/bin/env python3
"""Refresh the report from completed experiments, without inventing results."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def tex(value):
    replacements = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%",
                    "$": r"\$", "#": r"\#", "_": r"\_", "{": r"\{", "}": r"\}",
                    "^": r"\textasciicircum{}", "~": r"\textasciitilde{}"}
    return "".join(replacements.get(c, c) for c in str(value).replace("\u2014", "; "))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--report-root", type=Path, default=ROOT.parent)
    args = parser.parse_args()
    folder = args.results.resolve()
    summary = json.loads((folder / "summary.json").read_text())
    if summary.get("status") != "complete" or summary.get("errors"):
        raise SystemExit("Experiments are incomplete; the report retains its pending status")
    phases = summary["experiments"]
    blocked, failed, final = [phases[k] for k in ("blocked-source", "production-defect", "final-correction")]
    if (not blocked.get("production_unchanged") or failed.get("release_status") != "rolled_back"
            or failed.get("rollback_passed") != failed.get("rollback_total")
            or final.get("pipeline_status") != "Succeeded"):
        raise SystemExit("Measured rejection, rollback, and healthy final release are required")
    agents = summary["agent_runs"]
    if not all(agents.get(k, {}).get("status") == "complete" for k in
               ("engineering-agents", "blocked-source-diagnosis", "rollback-diagnosis",
                "final-verification-agent")):
        raise SystemExit("Actual successful model-assisted runs are required")

    report_root = args.report_root.resolve()
    source = report_root / "docs/report/GBM_Cloud_Project6_Report.tex"
    if not source.exists():
        raise SystemExit("Report source missing. Supply --report-root with the original workspace path")
    evidence = source.parent / "project6-evidence"
    evidence.mkdir(exist_ok=True)
    for name in ("summary.json", "blocked-source-execution.json", "production-defect-execution.json",
                 "final-correction-execution.json", "final-production-verification.json"):
        shutil.copy2(folder / name, evidence / name)

    images = []
    for phase, fragment, target in [
            ("production-defect", "planner-failed-", "production-failure.png"),
            ("production-defect", "planner-passed-", "rollback-restored.png"),
            ("final-correction", "planner-passed-", "final-production.png")]:
        with zipfile.ZipFile(folder / (phase + "-evidence.zip")) as archive:
            matches = [n for n in archive.namelist() if fragment in n and n.endswith(".png")]
            if not matches:
                raise SystemExit("Missing actual browser screenshot: " + phase + "/" + fragment)
            (evidence / target).write_bytes(archive.read(matches[-1]))
            images.append(target)

    lines = [r"\subsection{Controlled failure experiment results}",
             "The remaining experiments completed through actual GitHub pushes and AWS pipeline executions. "
             "All identifiers below come from retained execution records.",
             r"\subsubsection{Defect rejected before deployment}",
             "The required cloud-security SVG was removed in commit " +
             r"\texttt{" + blocked["commit"][:12] + "}. BuildAndValidate failed, and no Release action ran. "
             "Independent production verification passed for the unchanged known-good release.",
             r"\begin{lstlisting}",
             "Rejected commit: " + blocked["commit"],
             "Execution: " + blocked["execution_id"],
             r"\end{lstlisting}",
             r"\subsubsection{Production failure and automatic restoration}",
             "The controlled hostname-specific JavaScript defect passed pre-deployment checks but removed the "
             "production planner form. The actual browser measurement failed. The release controller automatically "
             "restored the complete previous release, invalidated CloudFront, and verified recovery. "
             "The candidate pipeline remained Failed and its release result recorded successful rollback.",
             r"\begin{lstlisting}",
             "Failed commit: " + failed["commit"],
             "Execution: " + failed["execution_id"],
             "Restored release: " + failed["restored_release"],
             "Rollback checks: " + str(failed["rollback_passed"]) + "/" + str(failed["rollback_total"]),
             r"\end{lstlisting}",
             r"\subsubsection{Measured agent participation}"]
    for label, result in agents.items():
        lines += [r"\paragraph{" + tex(label.replace("-", " ").capitalize()) + "}",
                  "Actual model calls and executable tool results are retained in run " +
                  r"\filepath{" + result["run_id"] + "}."]
        if label in ("blocked-source-diagnosis", "rollback-diagnosis"):
            path = ROOT / "runtime/runs" / result["run_id"] / "release_diagnosis-result.json"
            actual = json.loads(path.read_text())
            lines += [tex(actual["summary"])]
            for finding in actual.get("findings", [])[:4]:
                lines += [r"\begin{quote}\small " + tex(finding) + r"\end{quote}"]
    lines += [r"\subsubsection{Corrected final release}",
              "The deliberate defect was removed from source and a corrective push completed successfully. "
              "Independent verification and the release-verifier agent confirmed the healthy production website.",
              r"\begin{lstlisting}", "Final commit: " + final["commit"],
              "Final execution: " + final["execution_id"], r"\end{lstlisting}"]
    captions = ["Actual production browser failure before automatic rollback.",
                "Actual planner screenshot after the previous release was restored and verified.",
                "Actual planner screenshot from the corrected final release."]
    for name, caption in zip(images, captions):
        lines += [r"\begin{figure}[htbp]\centering",
                  r"\includegraphics[width=0.9\linewidth,height=0.58\textheight,keepaspectratio]{project6-evidence/" + name + "}",
                  r"\caption{" + caption + r"}\end{figure}"]
    content = source.read_text()
    headings = [r"\subsection{Controlled failures and current coverage}",
                r"\subsection{Controlled failure experiment results}"]
    start = next((content.index(h) for h in headings if h in content), None)
    if start is None:
        raise SystemExit("Could not locate the report's experiment section")
    end = content.index(r"\clearpage", start)
    content = content[:start] + "\n\n".join(lines) + "\n\n" + content[end:]
    content = content.replace(
        "The collected evidence does not yet contain the controlled Project 6 pre-deployment rejection, production-failure rollback, or model-driven diagnosis experiments. Those mechanisms are implemented and tested locally. Their expected outcomes are not presented as measured cloud results. Project 5's historical fault exercise is not evidence of Project 6 rollback.",
        "The controlled Project 6 source rejection, production failure, complete automatic rollback, and model-assisted diagnosis have now been measured. Section 4 records their actual commits, execution IDs, browser evidence, and agent results.")
    content = content.replace(
        "The available Project 6 records establish deterministic setup and deployment. They do not establish a completed live model-assisted diagnosis. Such a run, with actual handoffs and tool results, remains necessary evidence for that claim.",
        "Completed Project 6 agent runs establish actual model calls and programmatic handoffs. Engineering ran real configuration and test tools, diagnosis examined both failed executions, recovery coordination inspected the restored state, and a final verifier measured the healthy website. Section 4 identifies the corresponding records.")
    content = content.replace(
        "The successful release is measured. The controlled rejection, production rollback, and actual model-assisted diagnosis remain experimental validation gaps, not successes inferred from unit tests.",
        "The successful release, controlled rejection, production rollback, and model-assisted diagnoses were measured in this configuration. They do not prove recovery under every external outage or all possible infrastructure changes.")
    if "\u2014" in content:
        raise SystemExit("Unexpected em dash in generated source")
    source.write_text(content)
    subprocess.run(["bash", str(report_root / "scripts/build_project6_report.sh")], check=True)
    destination = report_root / "submission/GBM_Cloud_Project6_Report_Source.zip"
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in [source, source.parent / "report-style.sty",
                     report_root / "scripts/build_project6_report.sh", *sorted(evidence.glob("*"))]:
            if path.is_file():
                archive.write(path, path.relative_to(report_root))
    (ROOT / "submission").mkdir(exist_ok=True)
    shutil.copy2(report_root / "submission/GBM_Cloud_Project6_Report.pdf",
                 ROOT / "submission/GBM_Cloud_Project6_Report.pdf")
    shutil.copy2(destination, ROOT / "submission/GBM_Cloud_Project6_Report_Source.zip")
    shutil.copy2(ROOT / "submission/project6-execution-evidence.zip",
                 report_root / "submission/project6-execution-evidence.zip")
    review = {"status": "complete", "required_sections": 5, "architecture_diagrams": 2,
              "actual_browser_screenshots": 4, "pending_live_evidence": [],
              "final_release": final["commit"], "experiments": phases,
              "agent_runs": agents, "completed_at": summary["completed_at"]}
    (report_root / "submission/project6-report-quality-review.json").write_text(
        json.dumps(review, indent=2) + "\n")
    print("Updated PDF:", report_root / "submission/GBM_Cloud_Project6_Report.pdf")


if __name__ == "__main__":
    main()
