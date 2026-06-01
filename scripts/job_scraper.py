"""Scrape job offers from a list of careers pages using ScrapeGraphAI.

Usage:
  python scripts/job_scraper.py --sources-file job_scraper_companies_example.txt --out results.json

Requires:
  - SCRAPEGRAPHAI and Playwright installed
  - Set `OPENAI_APIKEY` (or other LLM creds) in environment or .env
"""

import os
import json
import csv
import argparse
from typing import List, Optional

from dotenv import load_dotenv
from pydantic import BaseModel, Field
import sys
from company_job_scraper import main as fallback_main

# Prefer local repository package to avoid mismatched installed versions
repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

try:
    from scrapegraphai.graphs import SmartScraperMultiGraph
except Exception:
    SmartScraperMultiGraph = None

load_dotenv()


class JobOffer(BaseModel):
    title: str = Field(description="Job title")
    department: Optional[str] = Field(default=None, description="Team or department")
    location: Optional[str] = Field(default=None, description="Job location")
    remote_status: Optional[str] = Field(default=None, description="Remote, hybrid, or onsite")
    employment_type: Optional[str] = Field(default=None, description="Full-time, part-time, contract, internship")
    posting_date: Optional[str] = Field(default=None, description="Date the job was posted")
    apply_url: Optional[str] = Field(default=None, description="Application link")
    description: Optional[str] = Field(default=None, description="Short description")
    company_name: Optional[str] = Field(default=None, description="Company name")


class JobOffers(BaseModel):
    jobs: List[JobOffer]


DEFAULT_PROMPT = """
Extract all open job offers from this careers page.
Return a JSON array of job objects with these fields exactly:
title, department, location, remote_status, employment_type, posting_date, apply_url, description, company_name
Only include currently open positions.
If a field is not available, return null for that field.
"""


def load_sources_from_file(path: str) -> List[str]:
    with open(path, "r", encoding="utf-8") as f:
        lines = [l.strip() for l in f if l.strip() and not l.startswith("#")]
    return lines


def normalize_result(raw) -> List[dict]:
    """Convert graph output to a flat list of job dicts."""
    jobs = []
    if raw is None:
        return jobs
    # If top-level has 'jobs'
    if isinstance(raw, dict) and "jobs" in raw and isinstance(raw["jobs"], list):
        jobs.extend(raw["jobs"])
        return jobs
    # If raw is list of jobs
    if isinstance(raw, list):
        # Items could be job dicts or dicts with 'jobs'
        for item in raw:
            if isinstance(item, dict) and "jobs" in item and isinstance(item["jobs"], list):
                jobs.extend(item["jobs"])
            elif isinstance(item, dict) and any(k in item for k in ("title", "apply_url")):
                jobs.append(item)
    # If raw is a mapping of source->jobs
    if isinstance(raw, dict):
        for v in raw.values():
            if isinstance(v, list):
                jobs.extend(v)
            elif isinstance(v, dict) and "jobs" in v and isinstance(v["jobs"], list):
                jobs.extend(v["jobs"])
    return jobs


def save_json(out_path: str, data):
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def save_csv(out_path: str, jobs: List[dict]):
    fields = [
        "title",
        "department",
        "location",
        "remote_status",
        "employment_type",
        "posting_date",
        "apply_url",
        "company_name",
        "description",
    ]
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for j in jobs:
            row = {k: (j.get(k) if j.get(k) is not None else "") for k in fields}
            writer.writerow(row)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sources-file", help="Path to file with one careers URL per line", required=True)
    parser.add_argument("--out", help="Output JSON path", default="results.json")
    parser.add_argument("--out-csv", help="Output CSV path (optional)")
    parser.add_argument("--headless", action="store_true", help="Run browser headless (Playwright)")
    parser.add_argument("--model", help="LLM model to use", default=None)
    parser.add_argument("--force-graph", action="store_true", help="Fail if graph imports are unavailable")
    parser.add_argument("--fallback-portal", help="Optional portal URL for the Playwright fallback path", default=None)
    args = parser.parse_args()

    sources = load_sources_from_file(args.sources_file)
    if not sources:
        print("No sources found in file.")
        return

    if SmartScraperMultiGraph is None:
        if args.force_graph:
            raise ImportError(
                "scrapegraphai graph imports are unavailable in this environment. "
                "Install compatible langchain_community/langchain-ollama packages or remove --force-graph."
            )

        print("Graph stack unavailable; falling back to the Playwright company scraper.")
        fallback_args = ["--sources-file", args.sources_file, "--out", args.out]
        if args.out_csv:
            fallback_args.extend(["--out-csv", args.out_csv])
        if args.headless:
            fallback_args.append("--headless")
        if args.fallback_portal:
            fallback_args.extend(["--fallback-portal", args.fallback_portal])
        fallback_main(fallback_args)
        return

    llm_conf = {"api_key": os.getenv("OPENAI_APIKEY")}
    if args.model:
        llm_conf["model"] = args.model

    graph_config = {
        "llm": llm_conf,
        "verbose": True,
        "headless": not args.headless,
    }

    # Build and run the graph
    graph = SmartScraperMultiGraph(
        prompt=DEFAULT_PROMPT,
        source=sources,
        schema=JobOffers,
        config=graph_config,
    )

    print(f"Running scraper against {len(sources)} sources...")
    raw = graph.run()

    jobs = normalize_result(raw)
    print(f"Found {len(jobs)} job entries (raw). Writing {args.out}...")

    save_json(args.out, {"jobs": jobs})
    if args.out_csv:
        save_csv(args.out_csv, jobs)

    print("Done.")


if __name__ == "__main__":
    main()
