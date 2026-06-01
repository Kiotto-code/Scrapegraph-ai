#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<EOF
Usage: $0 SOURCES_FILE [--out-dir DIR] [--headless] [--dry-run]

Convenience wrapper that activates the local `.venv` (if present) and runs
the scripts/run_company_scraper.sh wrapper.

Example:
  ./script.sh scripts/company_sources.example.txt --out-dir /tmp/scrapes --headless

EOF
  exit 1
}

if [[ $# -lt 1 ]]; then
  usage
fi

SOURCES="$1"
shift || true

# Activate local venv if present
if [[ -f ".venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

exec bash scripts/run_company_scraper.sh --sources "$SOURCES" "$@"
