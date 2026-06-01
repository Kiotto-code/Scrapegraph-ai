"""Compatibility wrapper for the generic company job scraper.

Prefer `scripts/company_job_scraper.py` for new work.
This file remains so earlier JPMorgan-only commands keep working.
"""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path
from typing import List, Optional

from company_job_scraper import main as generic_main


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--start", default="https://www.jpmorgan.com")
    parser.add_argument("--out", default="jpmorgan.json")
    parser.add_argument("--out-csv", default=None)
    parser.add_argument("--headless", action="store_true")
    args, unknown = parser.parse_known_args(argv)

    with tempfile.TemporaryDirectory() as temp_dir:
        sources_path = Path(temp_dir) / "jpmorgan_sources.txt"
        sources_path.write_text(f"JPMorgan | {args.start}\n", encoding="utf-8")

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

        # JPMorgan uses an Oracle Cloud candidate experience portal for job listings.
        forwarded_args.extend([
            "--fallback-portal",
            "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/requisitions",
        ])
        forwarded_args.extend(arg for arg in unknown if arg)

        generic_main(forwarded_args)


if __name__ == "__main__":
    main()
