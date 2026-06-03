"""Compatibility wrapper for the generic company job scraper.

Prefer `scripts/company_job_scraper.py` for new work.
This file remains so earlier PPG-only commands keep working.
"""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path
from typing import List, Optional

from company_job_scraper import main as generic_main


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--start", default="https://www.ppg.com/en-US")
    parser.add_argument("--out", default="ppg_jobs.json")
    parser.add_argument("--max-jobs", type=int, default=200, help="Maximum jobs to collect per company")
    parser.add_argument("--out-csv", default=None)
    parser.add_argument("--headless", action="store_true")
    args, unknown = parser.parse_known_args(argv)

    with tempfile.TemporaryDirectory() as temp_dir:
        sources_path = Path(temp_dir) / "ppg_sources.txt"
        sources_path.write_text(f"PPG | {args.start}\n", encoding="utf-8")

        forwarded_args = [
            "--sources-file",
            str(sources_path),
            "--out",
            args.out,
        ]
        if args.out_csv:
            forwarded_args.extend(["--out-csv", args.out_csv])
        if args.headless:
            forwarded_args.append("--headless")
        forwarded_args.extend(["--fallback-portal", "https://careers.ppg.com/us/en/search-results"])
        forwarded_args.extend(arg for arg in unknown if arg)

        generic_main(forwarded_args)


if __name__ == "__main__":
    main()
