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
import sys
import warnings
from dataclasses import dataclass
from typing import Dict, List, Optional
from urllib.parse import urljoin, urlparse

from playwright.sync_api import sync_playwright


# ---------------------------------------------------------------------------
# Keyword / hint constants
# ---------------------------------------------------------------------------

CAREERS_KEYWORDS = [
    "career", "careers", "jobs", "job", "join", "opportunities", "vacancy",
    "open positions", "work with us", "we're hiring", "hiring", "talent",
    "recruitment", "employment", "work here", "join us", "join our team",
    "open roles", "job openings", "current openings",
]

CAREERS_PORTAL_HINTS = [
    "search-results", "job-search", "careers", "jobs", "vacancies",
    "open-positions", "openings", "positions", "opportunities", "hiring",
    "talent", "recruitment", "apply", "current-openings", "job-listing",
    "job-openings", "open-roles",
]

JOB_PORTAL_HINTS = [
    # Generic job URL patterns
    "/job/", "/jobs/", "jobid=", "/position/", "/positions/",
    "/opening/", "/openings/", "/vacancy/", "/vacancies/",
    "/requisition/", "/requisitions/", "/posting/", "/postings/",
    "/role/", "/roles/",
    # Query-param patterns
    "req_id=", "requisitionid=", "job_id=", "positionid=",
    # ATS-specific URL path patterns
    "/gh/", "greenhouse.io/", "lever.co/",
    "myworkdayjobs.com", "myworkday.com",
    "icims.com", "smartrecruiters.com", "jobvite.com",
    "ultipro.com", "successfactors", "taleo",
    "bamboohr.com", "ashbyhq.com", "recruitee.com",
    "applytojob.com", "workable.com", "breezy.hr",
    # Detail page patterns
    "job-detail", "job_detail", "jobdetail",
    "career-detail", "apply/",
]

PREFERRED_PORTAL_HINTS = [
    # Major ATS platform patterns  (high confidence)
    "/requisitions", "candidateexperience", "oraclecloud",
    "search-results", "job-search",
    # Workday
    "myworkdayjobs.com", "wd1.myworkday", "wd3.myworkday", "wd5.myworkday",
    # Greenhouse
    "boards.greenhouse.io", "greenhouse.io/embed",
    # Lever
    "jobs.lever.co",
    # iCIMS
    "icims.com", "careers-", ".icims.",
    # SmartRecruiters
    "smartrecruiters.com/",
    # Taleo
    "taleo.net",
    # SAP SuccessFactors
    "successfactors.com", "successfactors.eu",
    # Jobvite
    "jobvite.com",
    # BambooHR
    "bamboohr.com/careers",
    # Ashby
    "ashbyhq.com",
    # Recruitee
    "recruitee.com",
    # Oracle HCM
    "hcmcloud", "fa.oraclecloud",
    # Other major platforms
    "phenom", "avature", "eightfold",
]

# Well-known ATS external domains.  When we find a link pointing at one of
# these we know it leads to a job board even without any keyword matching.
KNOWN_ATS_DOMAINS = [
    "boards.greenhouse.io", "greenhouse.io",
    "jobs.lever.co", "lever.co",
    "myworkdayjobs.com", "myworkday.com",
    "wd1.myworkday.com", "wd3.myworkday.com", "wd5.myworkday.com",
    "icims.com",
    "smartrecruiters.com",
    "taleo.net",
    "successfactors.com", "successfactors.eu",
    "jobvite.com",
    "bamboohr.com",
    "ashbyhq.com",
    "recruitee.com",
    "ultipro.com",
    "fa.oraclecloud.com",
    "phenom.com", "phenompeople.com",
    "avature.net",
    "eightfold.ai",
    "applytojob.com",
    "breezy.hr",
    "jazz.co", "resumator.com",
    "workable.com",
]

# Links whose href or text match these are almost certainly *not* job pages.
_SKIP_HREF_FRAGMENTS = [
    "login", "sign-in", "signin", "signup", "sign-up", "register",
    "blog/", "/news", "/about-us", "/about/", "contact-us", "contact/",
    "privacy", "terms", "cookie", "legal", "disclaimer", "faq",
    "linkedin.com", "facebook.com", "twitter.com", "instagram.com",
    "youtube.com", "mailto:", "tel:", "javascript:",
]

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


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

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


def _is_same_origin(url_a: str, url_b: str) -> bool:
    """Return True when two URLs share the same registered domain."""
    try:
        ha = urlparse(url_a).hostname or ""
        hb = urlparse(url_b).hostname or ""
        # Compare the last two domain parts (e.g. ppg.com == www.ppg.com)
        return ha.split(".")[-2:] == hb.split(".")[-2:]
    except Exception:
        return False


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


# ---------------------------------------------------------------------------
# Link discovery functions
# ---------------------------------------------------------------------------

def detect_ats_links(page) -> List[str]:
    """Scan all anchors for links pointing to known ATS external domains."""
    ats_links: List[str] = []
    seen: set = set()
    anchors = page.query_selector_all("a[href]")
    for anchor in anchors:
        href = anchor.get_attribute("href") or ""
        if not href:
            continue
        href_lower = href.lower()
        for domain in KNOWN_ATS_DOMAINS:
            if domain in href_lower:
                normalized = _normalize_url(page.url, href)
                if normalized not in seen:
                    seen.add(normalized)
                    ats_links.append(normalized)
                break
    return ats_links


def detect_iframe_job_boards(page) -> List[str]:
    """Check for iframes embedding known ATS job boards."""
    iframe_urls: List[str] = []
    seen: set = set()
    iframes = page.query_selector_all("iframe[src]")
    for iframe in iframes:
        src = iframe.get_attribute("src") or ""
        if not src:
            continue
        src_lower = src.lower()
        # Check known ATS domains
        for domain in KNOWN_ATS_DOMAINS:
            if domain in src_lower:
                if src not in seen:
                    seen.add(src)
                    iframe_urls.append(src)
                break
        else:
            # Also check for generic job-related iframes
            if any(hint in src_lower for hint in CAREERS_PORTAL_HINTS):
                if src not in seen:
                    seen.add(src)
                    iframe_urls.append(src)
    return iframe_urls


def find_careers_links(page) -> List[str]:
    """Find career/jobs links on the page, scored and ranked by relevance.

    Returns a list of URLs ranked from most to least likely to be a careers page.
    """
    candidates: List[tuple] = []  # (score, url)
    seen_urls: set = set()
    anchors = page.query_selector_all("a[href]")

    for anchor in anchors:
        href = anchor.get_attribute("href") or ""
        if not href or href.startswith("#"):
            continue

        text = _clean_text(anchor.inner_text()) or ""
        aria = anchor.get_attribute("aria-label") or ""
        title_attr = anchor.get_attribute("title") or ""

        href_lower = href.lower()
        text_lower = text.lower()
        label_lower = f"{aria} {title_attr}".lower()

        # Skip obvious non-career links
        if any(skip in href_lower for skip in _SKIP_HREF_FRAGMENTS):
            continue

        score = 0

        # --- Score based on href path ---
        if any(hint in href_lower for hint in CAREERS_PORTAL_HINTS):
            score += 3
        if "careers" in href_lower or "jobs" in href_lower:
            score += 2

        # --- Score based on visible text ---
        if _text_contains_keywords(text_lower, CAREERS_KEYWORDS):
            score += 3

        # --- Score based on aria-label / title attribute ---
        if _text_contains_keywords(label_lower, CAREERS_KEYWORDS):
            score += 1

        # --- Bonus for matching both href and text ---
        if score >= 5:
            score += 2

        # --- High priority: ATS domain links ---
        for domain in KNOWN_ATS_DOMAINS:
            if domain in href_lower:
                score += 5
                break

        if score > 0:
            normalized = _normalize_url(page.url, href)
            if normalized not in seen_urls and normalized != page.url:
                seen_urls.add(normalized)
                candidates.append((score, normalized))

    # Sort by score descending
    candidates.sort(key=lambda x: x[0], reverse=True)
    return [url for _, url in candidates]


def find_portal_links(page) -> List[str]:
    """Find job-portal links on the current (careers) page.

    Checks ATS domain links, iframes, preferred portal hints, and
    keyword-based heuristics.  Returns a ranked list of candidate portal URLs.
    """
    scored: List[tuple] = []  # (priority, url)
    seen: set = set()

    def _add(priority: int, url: str) -> None:
        if url and url not in seen and url != page.url:
            seen.add(url)
            scored.append((priority, url))

    # Priority 1 – links pointing to a known ATS domain
    for url in detect_ats_links(page):
        _add(10, url)

    # Priority 2 – iframe-embedded job boards
    for url in detect_iframe_job_boards(page):
        _add(9, url)

    anchors = page.query_selector_all("a[href]")

    # Priority 3 – preferred portal hint in href
    for anchor in anchors:
        href = anchor.get_attribute("href") or ""
        href_lower = href.lower()
        if any(hint in href_lower for hint in PREFERRED_PORTAL_HINTS):
            _add(8, _normalize_url(page.url, href))

    # Priority 4 – generic careers portal hint in href
    for anchor in anchors:
        href = anchor.get_attribute("href") or ""
        text = (_clean_text(anchor.inner_text()) or "").lower()
        href_lower = href.lower()
        if any(hint in href_lower for hint in CAREERS_PORTAL_HINTS):
            _add(5, _normalize_url(page.url, href))
        # Links with job-related text + action keywords
        if "job" in href_lower and any(kw in text for kw in [
            "apply", "view", "open", "job", "search", "browse",
            "see all", "view all", "explore",
        ]):
            _add(4, _normalize_url(page.url, href))

    # Priority 5 – buttons / link-buttons with portal-like text
    for btn in page.query_selector_all("a, button"):
        text = (_clean_text(btn.inner_text()) or "").lower()
        if any(phrase in text for phrase in [
            "view open positions", "see open positions", "browse jobs",
            "search jobs", "explore careers", "view all jobs",
            "see all jobs", "find a job", "search openings",
            "view opportunities", "current openings",
        ]):
            href = btn.get_attribute("href") or ""
            onclick = btn.get_attribute("onclick") or ""
            if href and not href.startswith("#"):
                _add(6, _normalize_url(page.url, href))
            elif onclick:
                # Try to extract URL from onclick="location.href='...'"
                m = re.search(r"""(?:location\.href|window\.open)\s*\(\s*['"]([^'"]+)['"]""", onclick)
                if m:
                    _add(6, _normalize_url(page.url, m.group(1)))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [url for _, url in scored]


def gather_job_links(page) -> List[str]:
    """Collect individual job-detail links from the current page.

    Uses expanded URL-pattern matching and content-based heuristics.
    """
    links: set = set()
    anchors = page.query_selector_all("a[href]")
    for anchor in anchors:
        href = anchor.get_attribute("href") or ""
        if not href:
            continue
        href_lower = href.lower()

        # Skip non-job links
        if "/hvhapply" in href_lower or "jobcart" in href_lower:
            continue

        matched = False

        # Check expanded URL-pattern hints
        if any(hint in href_lower for hint in JOB_PORTAL_HINTS):
            matched = True

        # Content-based: link text that looks like a job title or action
        if not matched:
            text = (_clean_text(anchor.inner_text()) or "").lower()
            # Links whose text contains "apply" alongside a non-trivial label
            if len(text) > 10 and any(kw in text for kw in [
                "apply now", "view details", "learn more", "view job",
            ]):
                # But only if the href also hints at a job
                if any(frag in href_lower for frag in [
                    "job", "position", "career", "role", "opening", "requisition",
                    "posting", "vacancy", "apply",
                ]):
                    matched = True

        if matched:
            links.add(_normalize_url(page.url, href))

    return sorted(links)


# ---------------------------------------------------------------------------
# Pagination helpers
# ---------------------------------------------------------------------------

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
    seen: set = set()
    out: List[str] = []
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


# ---------------------------------------------------------------------------
# Job-field extraction helpers
# ---------------------------------------------------------------------------

def _find_first_heading_index(lines: List[str], candidates: List[str]) -> Optional[int]:
    lowered_candidates = [candidate.lower() for candidate in candidates]
    for index, line in enumerate(lines):
        normalized = line.lower().strip().rstrip(":")
        if any(normalized == candidate or candidate in normalized for candidate in lowered_candidates):
            return index
    return None


def _collect_section_items(lines: List[str], start_index: int) -> List[str]:
    items: List[str] = []
    for line in lines[start_index + 1:]:
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


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Core scraping logic
# ---------------------------------------------------------------------------

def _try_gather_jobs(page, company: str, wait_ms: int = 4000) -> List[str]:
    """Attempt to gather job links from the current page with a retry.

    First tries immediately, then waits ``wait_ms`` for JS rendering and
    tries again if the first attempt returned fewer than 5 links.
    """
    job_links = gather_job_links(page)
    if len(job_links) < 5:
        page.wait_for_timeout(wait_ms)
        job_links = gather_job_links(page)
    return job_links


def _follow_pagination(page, job_links: List[str], company: str, max_jobs: int) -> List[str]:
    """Follow pagination links to collect more job URLs."""
    if len(job_links) >= max_jobs:
        return job_links

    pagination_urls = discover_pagination_urls(page, max_pages=8)
    if not pagination_urls:
        pagination_urls = construct_query_pages(page.url, max_pages=8)

    for purl in pagination_urls:
        if len(job_links) >= max_jobs:
            break
        try:
            print(f"[{company}] Following pagination: {purl}")
            _goto_and_wait(page, purl)
            new_links = gather_job_links(page)
            for link in new_links:
                if link not in job_links:
                    job_links.append(link)
            page.wait_for_timeout(1000)
        except Exception as exc:
            print(f"[{company}] Pagination visit failed: {exc}")

    return job_links


def _try_scroll_for_jobs(page, company: str, max_scrolls: int = 5) -> List[str]:
    """Scroll the page to trigger lazy-loaded job listings.

    Only used as a last-resort strategy when other approaches found nothing.
    """
    print(f"[{company}] Scrolling page to trigger lazy-loaded content...")
    for i in range(max_scrolls):
        page.mouse.wheel(0, 3000)
        page.wait_for_timeout(1500)
        links = gather_job_links(page)
        if links:
            print(f"[{company}] Found {len(links)} job links after {i + 1} scroll(s)")
            return links
    return []


def scrape_company(page, source: Source, max_jobs: int) -> List[Dict]:
    """Scrape jobs from a company website using multi-level discovery.

    Navigation strategy (each step tries the next only when the previous
    yielded no job links):

    1. Visit homepage → look for ATS links / careers links
    2. Visit up to 3 candidate careers pages
    3. On each careers page → look for portal links (ATS domains, iframes,
       keyword hints)
    4. Visit up to 2 candidate portals per careers page
    5. On each portal page → gather job links + paginate
    6. Last resort: scroll the most promising page for lazy-loaded content
    """
    _goto_and_wait(page, source.url)
    company = source.company

    # ------------------------------------------------------------------
    # Step 1 – Discover careers links from homepage
    # ------------------------------------------------------------------
    careers_candidates = find_careers_links(page)
    ats_direct = detect_ats_links(page)
    iframe_direct = detect_iframe_job_boards(page)

    # If the homepage itself has ATS links, try them first
    all_portal_candidates = list(dict.fromkeys(ats_direct + iframe_direct))

    if all_portal_candidates:
        print(f"[{company}] Found {len(all_portal_candidates)} ATS/iframe link(s) on homepage")

    if careers_candidates:
        print(f"[{company}] Found {len(careers_candidates)} careers link candidate(s) on homepage")
    else:
        print(f"[{company}] No careers link discovered on homepage")

    # ------------------------------------------------------------------
    # Step 2 – Try portal links discovered directly on the homepage
    # ------------------------------------------------------------------
    job_links: List[str] = []

    for portal_url in all_portal_candidates[:2]:
        print(f"[{company}] Trying ATS portal from homepage: {portal_url}")
        try:
            _goto_and_wait(page, portal_url)
            job_links = _try_gather_jobs(page, company)
            if job_links:
                job_links = _follow_pagination(page, job_links, company, max_jobs)
                print(f"[{company}] Discovered {len(job_links)} job links from direct portal")
                break
        except Exception as exc:
            print(f"[{company}] Failed to load ATS portal {portal_url}: {exc}")

    # ------------------------------------------------------------------
    # Step 3 – Visit candidate careers pages and look for portals + jobs
    # ------------------------------------------------------------------
    if not job_links:
        for careers_url in careers_candidates[:3]:
            print(f"[{company}] Visiting careers page: {careers_url}")
            try:
                _goto_and_wait(page, careers_url)
            except Exception as exc:
                print(f"[{company}] Failed to load careers page {careers_url}: {exc}")
                continue

            # Check if the careers page itself lists jobs
            job_links = _try_gather_jobs(page, company)
            if job_links:
                job_links = _follow_pagination(page, job_links, company, max_jobs)
                print(f"[{company}] Discovered {len(job_links)} job links on careers page")
                break

            # Look for portal links on the careers page
            portal_candidates = find_portal_links(page)
            if portal_candidates:
                print(f"[{company}] Found {len(portal_candidates)} portal candidate(s) on careers page")

            for portal_url in portal_candidates[:2]:
                print(f"[{company}] Trying portal: {portal_url}")
                try:
                    _goto_and_wait(page, portal_url)
                    job_links = _try_gather_jobs(page, company)
                    if job_links:
                        job_links = _follow_pagination(page, job_links, company, max_jobs)
                        print(f"[{company}] Discovered {len(job_links)} job links from portal")
                        break
                except Exception as exc:
                    print(f"[{company}] Failed to load portal {portal_url}: {exc}")

            if job_links:
                break

            # Depth-2: check sub-links on the careers page for a deeper portal
            sub_links = find_careers_links(page)
            for sub_url in sub_links[:2]:
                if sub_url == careers_url:
                    continue
                print(f"[{company}] Trying depth-2 sub-link: {sub_url}")
                try:
                    _goto_and_wait(page, sub_url)
                    job_links = _try_gather_jobs(page, company)
                    if job_links:
                        job_links = _follow_pagination(page, job_links, company, max_jobs)
                        print(f"[{company}] Discovered {len(job_links)} job links at depth-2")
                        break

                    # One more portal check
                    deep_portals = find_portal_links(page)
                    for dp_url in deep_portals[:1]:
                        print(f"[{company}] Trying depth-2 portal: {dp_url}")
                        try:
                            _goto_and_wait(page, dp_url)
                            job_links = _try_gather_jobs(page, company)
                            if job_links:
                                job_links = _follow_pagination(page, job_links, company, max_jobs)
                                print(f"[{company}] Discovered {len(job_links)} job links at depth-2 portal")
                                break
                        except Exception as exc:
                            print(f"[{company}] Failed depth-2 portal {dp_url}: {exc}")
                except Exception as exc:
                    print(f"[{company}] Failed depth-2 sub-link {sub_url}: {exc}")

                if job_links:
                    break

            if job_links:
                break

    # ------------------------------------------------------------------
    # Step 4 – Last resort: scroll the page for lazy-loaded content
    # ------------------------------------------------------------------
    if not job_links:
        # Navigate back to the best portal/careers page found
        best_page_url = (
            all_portal_candidates[0] if all_portal_candidates
            else careers_candidates[0] if careers_candidates
            else source.url
        )
        print(f"[{company}] Last resort: scrolling {best_page_url}")
        try:
            _goto_and_wait(page, best_page_url)
            job_links = _try_scroll_for_jobs(page, company)
        except Exception as exc:
            print(f"[{company}] Last-resort scroll failed: {exc}")

    # ------------------------------------------------------------------
    # Step 5 – Extract fields from each job page
    # ------------------------------------------------------------------
    print(f"[{company}] Total discovered: {len(job_links)} candidate job links")

    jobs: List[Dict] = []
    for index, link in enumerate(job_links[:max_jobs]):
        try:
            print(f"[{company}] Visiting [{index + 1}] {link}")
            _goto_and_wait(page, link)
            fields = extract_job_fields(page, source.company)
            fields["apply_url"] = link
            fields["source_url"] = source.url
            jobs.append(fields)
        except Exception as exc:
            print(f"[{company}] Error visiting {link}: {exc}")

    return jobs


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scrape job listings from company websites without needing manual fallback portals."
    )
    parser.add_argument("--sources-file", required=True, help="File with company names and URLs")
    parser.add_argument("--out", default="jobs.json", help="Output JSON path")
    parser.add_argument("--out-csv", default=None, help="Output CSV path")
    parser.add_argument("--max-jobs", type=int, default=50, help="Maximum jobs to collect per company")
    parser.add_argument("--headless", action="store_true", help="Run browser headless")
    # Kept for backward compatibility; silently ignored.
    parser.add_argument("--fallback-portal", default=None, help=argparse.SUPPRESS)
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

    if args.fallback_portal:
        warnings.warn(
            "--fallback-portal is deprecated and will be ignored. "
            "The scraper now auto-discovers job portals.",
            DeprecationWarning,
            stacklevel=2,
        )

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
                company_jobs = scrape_company(page, source, max_jobs=args.max_jobs)
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