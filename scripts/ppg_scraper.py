"""PPG careers scraper using Playwright.

Usage:
  python scripts/ppg_scraper.py --out ppg_jobs.json --out-csv ppg_jobs.csv

This script heuristically finds the careers/jobs page from the PPG homepage,
collects job links, and scrapes basic fields (title, location, apply link, summary).
"""
import json
import csv
import argparse
import re
from typing import List, Dict, Optional
from playwright.sync_api import sync_playwright

KEYWORDS = ["career", "careers", "jobs", "join", "opportunities", "careers-at"]


def find_careers_link(page) -> Optional[str]:
    anchors = page.query_selector_all("a[href]")
    for a in anchors:
        href = a.get_attribute("href") or ""
        text = (a.inner_text() or "").lower()
        # check href and text for keywords
        if any(k in href.lower() for k in KEYWORDS) or any(k in text for k in KEYWORDS):
            # normalize
            if href.startswith("/"):
                href = page.url.rstrip("/") + href
            if href.startswith("http"):
                return href
    return None


def gather_job_links(page) -> List[str]:
    links = set()
    anchors = page.query_selector_all("a[href]")
    for a in anchors:
        href = a.get_attribute("href") or ""
        text = (a.inner_text() or "").lower()
        if not href:
            continue
        if any(k in href.lower() for k in ["job", "career", "open-role", "opportunity", "position"]) or any(k in text for k in ["apply", "view job", "open role", "job"]):
            if href.startswith("/"):
                href = page.url.rstrip("/") + href
            if href.startswith("http"):
                links.add(href.split('#')[0])
    return list(links)


def extract_job_fields(page) -> Dict:
    text = page.inner_text("body") or ""
    # title candidates
    title = None
    for sel in ["h1", "h2", "title"]:
        el = page.query_selector(sel)
        if el:
            t = (el.inner_text() or "").strip()
            if t:
                title = t
                break
    # location heuristics
    loc_match = re.search(r"Location[:\s]*([A-Za-z0-9,\-\s]+)", text, re.IGNORECASE)
    location = loc_match.group(1).strip() if loc_match else None
    # posting date
    date_match = re.search(r"(Posted|Posting Date|Date Posted)[:\s]*([A-Za-z0-9,\- ]{3,30})", text, re.IGNORECASE)
    posting_date = date_match.group(2).strip() if date_match else None
    # description: first 400 chars of main content
    desc = None
    p = page.query_selector_all("p")
    if p:
        for el in p:
            t = (el.inner_text() or "").strip()
            if len(t) > 40:
                desc = t
                break
    if not desc:
        desc = (text[:400] + "...") if text else None

    return {"title": title, "location": location, "posting_date": posting_date, "description": desc}


def save_json(path: str, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def save_csv(path: str, jobs: List[Dict]):
    fields = ["title", "location", "posting_date", "apply_url", "description"]
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for j in jobs:
            row = {k: (j.get(k) if j.get(k) is not None else "") for k in fields}
            writer.writerow(row)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", help="Start URL", default="https://www.ppg.com/en-US")
    parser.add_argument("--out", help="Output JSON path", default="ppg_jobs.json")
    parser.add_argument("--out-csv", help="Output CSV path", default=None)
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()

    jobs = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headless)
        page = browser.new_page()
        page.goto(args.start, timeout=60000)

        careers = find_careers_link(page)
        if careers:
            print("Found careers link:", careers)
            page.goto(careers, timeout=60000)
        else:
            print("Careers link not found on homepage, continuing on homepage")

        # gather job links
        job_links = gather_job_links(page)
        print(f"Discovered {len(job_links)} candidate job links")

        # limit to first 50
        for idx, link in enumerate(job_links[:50]):
            try:
                print(f"Visiting [{idx+1}] {link}")
                page.goto(link, timeout=60000)
                fields = extract_job_fields(page)
                fields["apply_url"] = link
                jobs.append(fields)
            except Exception as e:
                print("Error visiting", link, e)

        browser.close()

    save_json(args.out, {"jobs": jobs})
    if args.out_csv:
        save_csv(args.out_csv, jobs)
    print("Saved", args.out)


if __name__ == "__main__":
    main()
