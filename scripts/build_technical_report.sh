#!/usr/bin/env bash
set -euo pipefail

report_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
report_build="$report_root/runtime/report-build"
mkdir -p "$report_build" "$report_root/submission"
cd "$report_root/docs/report"
tectonic --only-cached --untrusted --keep-logs --keep-intermediates \
  --outdir "$report_build" GBM_Cloud_Project5_Report.tex
cp "$report_build/GBM_Cloud_Project5_Report.pdf" \
  "$report_root/submission/GBM_Cloud_Project5_Report.pdf"
echo "Created submission/GBM_Cloud_Project5_Report.pdf"
