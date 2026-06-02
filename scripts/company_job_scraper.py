"""Generic company job scraper for multiple websites.

Usage:
  python scripts/company_job_scraper.py --sources-file scripts/company_jobs_.example.txt \
    --out jobs.json --out-csv jobs.csv

The sources file accepts either:
  - `Company Name | https://example.com`
  - `https://example.com`

The scraper visits each company homepage, looks for a careers/jobs portal,
collects job links, then extracts common fields from each job page.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from dataclasses import dataclass
import sys
from typing import Dict, List, Optional
from urllib.parse import urljoin

from playwright.sync_api import sync_playwright


CAREERS_KEYWORDS = ["career", "careers", "jobs", "job", "join", "opportunities", "vacancy", "open positions"]
CAREERS_PORTAL_HINTS = ["search-results", "job-search", "careers", "jobs", "vacancies", "open-positions"]
JOB_PORTAL_HINTS = ["/job/", "/jobs/", "jobid="]
PREFERRED_PORTAL_HINTS = ["/requisitions", "candidateexperience", "oraclecloud", "search-results", "job-search"]
SECTION_HEADINGS = [
    "required skills",
    "skills",
    "qualifications",
    "requirements",
    "what you need",
    "you have",
    "preferred",
    "nice to have",
]


@dataclass
class Source:
    company: str
    url: str


def _clean_text(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    cleaned = re.sub(r"\s+", " ", value).strip("\n\r\t :;-")
    return cleaned or None


def _normalize_url(base_url: str, href: str) -> str:
    return urljoin(base_url, href).split("#")[0]


def _text_contains_keywords(text: str, keywords: List[str]) -> bool:
    lowered = text.lower()
    return any(keyword in lowered for keyword in keywords)


def load_sources_from_file(path: str) -> List[Source]:
    sources: List[Source] = []
    with open(path, "r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "|" in line:
                company, url = [part.strip() for part in line.split("|", 1)]
            else:
                url = line
                company = re.sub(r"^https?://", "", url).split("/")[0]
            sources.append(Source(company=company or url, url=url))
    return sources


def _goto_and_wait(page, url: str, timeout: int = 60000) -> None:
    page.goto(url, timeout=timeout, wait_until="domcontentloaded")
    try:
        page.wait_for_load_state("networkidle", timeout=min(timeout, 15000))
    except Exception:
        page.wait_for_timeout(2500)


def find_careers_link(page) -> Optional[str]:
    anchors = page.query_selector_all("a[href]")
    for anchor in anchors:
        href = anchor.get_attribute("href") or ""
        text = _clean_text(anchor.inner_text()) or ""
        href_lower = href.lower()
        text_lower = text.lower()

        if _text_contains_keywords(href_lower, CAREERS_PORTAL_HINTS) or _text_contains_keywords(text_lower, CAREERS_KEYWORDS):
            return _normalize_url(page.url, href)
        if "careers" in href_lower or "jobs" in href_lower:
            return _normalize_url(page.url, href)
    return None


def find_portal_link(page) -> Optional[str]:
    anchors = page.query_selector_all("a[href]")

    for anchor in anchors:
        href = anchor.get_attribute("href") or ""
        href_lower = href.lower()
        if any(hint in href_lower for hint in PREFERRED_PORTAL_HINTS):
            return _normalize_url(page.url, href)

    for anchor in anchors:
        href = anchor.get_attribute("href") or ""
        text = (_clean_text(anchor.inner_text()) or "").lower()
        href_lower = href.lower()
        if any(hint in href_lower for hint in CAREERS_PORTAL_HINTS):
            return _normalize_url(page.url, href)
        if "job" in href_lower and any(keyword in text for keyword in ["apply", "view", "open", "job", "search"]):
            return _normalize_url(page.url, href)
    return None


def gather_job_links(page) -> List[str]:
    links = set()
    anchors = page.query_selector_all("a[href]")
    for anchor in anchors:
        href = anchor.get_attribute("href") or ""
        if not href:
            continue
        href_lower = href.lower()
        if not any(hint in href_lower for hint in JOB_PORTAL_HINTS):
            continue
        if "/hvhapply" in href_lower or "jobcart" in href_lower:
            continue
        links.add(_normalize_url(page.url, href))
    return sorted(links)


def discover_pagination_urls(page, max_pages: int = 10) -> List[str]:
    """Find candidate pagination URLs from the current page.

    Looks for anchors containing common pagination query params (page, pg)
    or numeric page links. Returns absolute URLs (unique, stable order).
    """
    anchors = page.query_selector_all("a[href]")
    candidates = []
    for a in anchors:
        href = a.get_attribute("href") or ""
        if not href:
            continue
        h = href.lower()
        if any(p in h for p in ["?page=", "&page=", "page=", "?pg=", "&pg=", "/page/", "/pg/"]):
            candidates.append(_normalize_url(page.url, href))
        else:
            # numeric link text like '2', '3', etc.
            text = (_clean_text(a.inner_text()) or "").strip()
            if re.fullmatch(r"\d{1,3}", text):
                candidates.append(_normalize_url(page.url, href))

    # dedupe while preserving order
    seen = set()
    out = []
    for url in candidates:
        if url in seen:
            continue
        seen.add(url)
        out.append(url)
        if len(out) >= max_pages:
            break
    return out


def construct_query_pages(base_url: str, max_pages: int = 10) -> List[str]:
    """Construct simple ?page=N variants for a base URL if none are found.

    Only used as a fallback when no explicit pagination anchors exist.
    """
    pages = []
    sep = "?"
    if "?" in base_url:
        sep = "&"
    for p in range(2, max_pages + 1):
        pages.append(f"{base_url}{sep}page={p}")
    return pages


def _find_first_heading_index(lines: List[str], candidates: List[str]) -> Optional[int]:
    lowered_candidates = [candidate.lower() for candidate in candidates]
    for index, line in enumerate(lines):
        normalized = line.lower().strip().rstrip(":")
        if any(normalized == candidate or candidate in normalized for candidate in lowered_candidates):
            return index
    return None


def _collect_section_items(lines: List[str], start_index: int) -> List[str]:
    items: List[str] = []
    for line in lines[start_index + 1 :]:
        normalized = line.strip()
        if not normalized:
            if items:
                break
            continue
        if len(normalized) <= 60 and normalized.endswith(":"):
            break
        if re.match(r"^(about us|responsibilities|requirements|skills|qualifications|what you\s+need|nice to have|preferred)\b", normalized.lower()):
            break
        normalized = re.sub(r"^[•\-*\u2022\s]+", "", normalized).strip()
        if normalized:
            items.append(normalized)
    return items


def _extract_required_skills(text: str) -> Optional[List[str]]:
    lines = [_clean_text(line) for line in text.splitlines()]
    lines = [line for line in lines if line]

    for heading in SECTION_HEADINGS:
        index = _find_first_heading_index(lines, [heading])
        if index is None:
            continue
        section_items = _collect_section_items(lines, index)
        if section_items:
            return section_items[:10]

    return None


def extract_job_fields(page, company: str) -> Dict:
    text = page.inner_text("body") or ""
    text = str(text)

    title = None
    for selector in ["h1", "h2", "title"]:
        element = page.query_selector(selector)
        if element:
            candidate = _clean_text(element.inner_text())
            if candidate:
                title = candidate
                break

    role = title
    company_name = company

    location = None
    location_match = re.search(r"Location[:\s]*([A-Za-z0-9,\-\s/()]+)", text, re.IGNORECASE)
    if location_match:
        location = _clean_text(location_match.group(1))

    department = None
    department_match = re.search(
        r"(?:Department|Function|Business Unit|Division|Area|Team)[:\s]*([A-Za-z0-9,&/()\-\s]+)",
        text,
        re.IGNORECASE,
    )
    if department_match:
        department = _clean_text(department_match.group(1))

    posting_date = None
    date_match = re.search(r"(Posted|Posting Date|Date Posted)[:\s]*([A-Za-z0-9,\- ]{3,40})", text, re.IGNORECASE)
    if date_match:
        posting_date = _clean_text(date_match.group(2))

    remote_status = None
    remote_match = re.search(r"\b(remote|hybrid|onsite|on-site)\b", text, re.IGNORECASE)
    if remote_match:
        remote_status = _clean_text(remote_match.group(1).title())

    employment_type = None
    type_match = re.search(r"\b(full[- ]time|part[- ]time|contract|temporary|internship|intern|casual)\b", text, re.IGNORECASE)
    if type_match:
        employment_type = _clean_text(type_match.group(1).title())

    description = None
    paragraphs = page.query_selector_all("p")
    if paragraphs:
        for paragraph in paragraphs:
            candidate = _clean_text(paragraph.inner_text())
            if candidate is not None and len(candidate) > 40:
                description = candidate
                break
    if not description:
        description = _clean_text(text[:800])

    required_skills = _extract_required_skills(text)

    return {
        "role": role,
        "title": title,
        "department": department,
        "company": company_name,
        "location": location,
        "posting_date": posting_date,
        "remote_status": remote_status,
        "employment_type": employment_type,
        "required_skills": required_skills,
        "description": description,
    }


def save_json(path: str, data) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)


def save_csv(path: str, jobs: List[Dict]) -> None:
    fields = [
        "company",
        "role",
        "title",
        "department",
        "location",
        "posting_date",
        "remote_status",
        "employment_type",
        "apply_url",
        "required_skills",
        "description",
    ]
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for job in jobs:
            row = {
                field: (", ".join(job.get(field)) if isinstance(job.get(field), list) else (job.get(field) if job.get(field) is not None else ""))
                for field in fields
            }
            writer.writerow(row)


def scrape_company(page, source: Source, max_jobs: int, portal_fallback: Optional[str] = None) -> List[Dict]:
    _goto_and_wait(page, source.url)

    normalized_source = source.url.lower()
    if portal_fallback is None:
        if "jpmorgan.com" in normalized_source or "jpmorganchase.com" in normalized_source:
            portal_fallback = "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/requisitions"

    portal = find_portal_link(page)
    careers = find_careers_link(page)

    if careers and careers != page.url:
        print(f"[{source.company}] Found careers link: {careers}")
        _goto_and_wait(page, careers)
    else:
        print(f"[{source.company}] No careers link discovered on homepage")

    portal = find_portal_link(page)
    if portal and portal != page.url:
        print(f"[{source.company}] Found careers portal: {portal}")
        _goto_and_wait(page, portal)
    elif portal_fallback and portal_fallback != page.url:
        print(f"[{source.company}] Falling back to portal: {portal_fallback}")
        _goto_and_wait(page, portal_fallback)
    else:
        print(f"[{source.company}] No portal discovered yet; using current page")

    job_links = gather_job_links(page)
    if len(job_links) < 5:
        page.wait_for_timeout(4000)
        job_links = gather_job_links(page)

    # If not enough links found, attempt to follow pagination pages (numeric or ?page=N)
    if len(job_links) < max_jobs:
        pagination_urls = discover_pagination_urls(page, max_pages=8)
        if not pagination_urls:
            # try simple ?page=N construction as a last resort
            pagination_urls = construct_query_pages(page.url, max_pages=8)

        for purl in pagination_urls:
            if len(job_links) >= max_jobs:
                break
            try:
                print(f"[{source.company}] Following pagination: {purl}")
                _goto_and_wait(page, purl)
                new_links = gather_job_links(page)
                for l in new_links:
                    if l not in job_links:
                        job_links.append(l)
                # small delay to let JS render next page
                page.wait_for_timeout(1000)
            except Exception as exc:
                print(f"[{source.company}] Pagination visit failed: {exc}")

    if not job_links and careers and careers != page.url:
        # If the marketing careers page had no jobs, retry that page and re-discover a portal.
        _goto_and_wait(page, careers)
        portal = find_portal_link(page)
        if portal and portal != page.url:
            print(f"[{source.company}] Retrying discovered portal: {portal}")
            _goto_and_wait(page, portal)
        elif portal_fallback and portal_fallback != page.url:
            print(f"[{source.company}] Retrying fallback portal: {portal_fallback}")
            _goto_and_wait(page, portal_fallback)
        job_links = gather_job_links(page)
        if len(job_links) < 5:
            page.wait_for_timeout(4000)
            job_links = gather_job_links(page)

    print(f"[{source.company}] Discovered {len(job_links)} candidate job links")

    jobs: List[Dict] = []
    for index, link in enumerate(job_links[:max_jobs]):
        try:
            print(f"[{source.company}] Visiting [{index + 1}] {link}")
            _goto_and_wait(page, link)
            fields = extract_job_fields(page, source.company)
            fields["apply_url"] = link
            fields["source_url"] = source.url
            jobs.append(fields)
        except Exception as exc:
            print(f"[{source.company}] Error visiting {link}: {exc}")

    return jobs


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sources-file", required=True, help="File with company names and URLs")
    parser.add_argument("--out", default="jobs.json", help="Output JSON path")
    parser.add_argument("--out-csv", default=None, help="Output CSV path")
    parser.add_argument("--max-jobs", type=int, default=50, help="Maximum jobs to collect per company")
    parser.add_argument("--headless", action="store_true", help="Run browser headless")
    parser.add_argument("--fallback-portal", default=None, help="Optional portal URL to try when no portal is discovered")
    return parser.parse_args()


def main(argv: Optional[List[str]] = None) -> None:
    if argv is not None:
        old_argv = sys.argv
        sys.argv = [old_argv[0], *argv]
    try:
        args = parse_args()
    finally:
        if argv is not None:
            sys.argv = old_argv

    sources = load_sources_from_file(args.sources_file)
    if not sources:
        print("No sources found in file.")
        return

    all_jobs: List[Dict] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=args.headless)
        page = browser.new_page()

        for source in sources:
            try:
                company_jobs = scrape_company(page, source, max_jobs=args.max_jobs, portal_fallback=args.fallback_portal)
                all_jobs.extend(company_jobs)
            except Exception as exc:
                print(f"[{source.company}] Failed: {exc}")

        browser.close()

    save_json(args.out, {"jobs": all_jobs})
    if args.out_csv:
        save_csv(args.out_csv, all_jobs)

    print(f"Saved {len(all_jobs)} jobs to {args.out}")


if __name__ == "__main__":
    main()