#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<EOF
Usage: $0 --sources FILE [--out-dir DIR] [--headless] [--dry-run]

Runs `scripts/company_job_scraper.py` for each line in a sources file.
Sources file format: one entry per line: "Company | https://start-url"

Options:
  --sources FILE   Path to sources file (required)
  --out-dir DIR    Directory to write outputs (default: ./outputs)
  --headless       Run browser in headless mode
  --dry-run        Print commands instead of executing
  -h, --help       Show this help
EOF
  exit 1
}

OUT_DIR=./outputs
DRY_RUN=0
HEADLESS_FLAG=""
SOURCES=""

# Allow a positional first argument for the sources file for convenience:
if [[ $# -gt 0 && "${1:0:1}" != "-" ]]; then
  SOURCES="$1"
  shift
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --sources)
      SOURCES="$2"; shift 2;;
    --out-dir)
      OUT_DIR="$2"; shift 2;;
    --headless)
      HEADLESS_FLAG="--headless"; shift;;
    --dry-run)
      DRY_RUN=1; shift;;
    -h|--help)
      usage;;
    *)
      echo "Unknown option: $1"; usage;;
  esac
done

if [[ -z "$SOURCES" ]]; then
  echo "Error: --sources is required."; usage
fi

mkdir -p "$OUT_DIR"

while IFS= read -r line || [[ -n "$line" ]]; do
  # skip empty lines and comments
  [[ -z "$line" || "${line:0:1}" == "#" ]] && continue

  if [[ "$line" == *"|"* ]]; then
    company=$(printf "%s" "$line" | awk -F'|' '{print $1}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    url=$(printf "%s" "$line" | awk -F'|' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
  else
    url="$line"
    # derive company from hostname
    company=$(echo "$url" | sed -E 's~^https?://~~' | cut -d'/' -f1)
  fi

  if [[ -z "$url" ]]; then
    echo "Skipping malformed line: $line"
    continue
  fi

  # normalize filename-safe company id
  id=$(echo "$company" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]/_/g' | sed 's/_\+/_/g' | sed 's/^_\|_\$//g')
  out_json="$OUT_DIR/${id}.json"
  out_csv="$OUT_DIR/${id}.csv"

  tmpfile=$(mktemp)
  printf '%s | %s\n' "$company" "$url" > "$tmpfile"

  cmd=(".venv/bin/python" "scripts/company_job_scraper.py" "--sources-file" "$tmpfile" "--out" "$out_json" "--out-csv" "$out_csv" "$HEADLESS_FLAG")

  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "DRY-RUN: would run for '$company' -> $url"
    printf 'Command: %s\n' "${cmd[*]}"
    rm -f "$tmpfile"
    continue
  fi

  echo "Running scraper for '$company' -> $url"
  "${cmd[@]}"
  rc=$?
  if [[ $rc -ne 0 ]]; then
    echo "Scraper failed for $company (exit $rc)"
  fi

  rm -f "$tmpfile"
done < "$SOURCES"

echo "All done. Outputs in: $OUT_DIR"
