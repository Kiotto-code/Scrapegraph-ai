"""Generic company job scraper for multiple websites (Async & Stealth Refactor).

Usage:
  python scripts/company_job_scraper.py --sources-file scripts/company_jobs_.example.txt \
    --out jobs.json --out-csv jobs.csv --concurrency 5

The scraper visits each company homepage asynchronously, looks for a careers/jobs portal,
collects job links, then extracts common fields from each job page.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import random
import re
import sys
import warnings
from dataclasses import dataclass
from typing import Dict, List, Optional
from urllib.parse import urljoin, urlparse

from playwright.async_api import async_playwright
from playwright_stealth import stealth_async

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
    "/job/", "/jobs/", "jobid=", "/position/", "/positions/",
    "/opening/", "/openings/", "/vacancy/", "/vacancies/",
    "/requisition/", "/requisitions/", "/posting/", "/postings/",
    "/role/", "/roles/", "req_id=", "requisitionid=", "job_id=", "positionid=",
    "/gh/", "greenhouse.io/", "lever.co/", "myworkdayjobs.com", "myworkday.com",
    "icims.com", "smartrecruiters.com", "jobvite.com", "ultipro.com", "successfactors",
    "taleo", "bamboohr.com", "ashbyhq.com", "recruitee.com", "applytojob.com",
    "workable.com", "breezy.hr", "job-detail", "job_detail", "jobdetail",
    "career-detail", "apply/",
]

PREFERRED_PORTAL_HINTS = [
    "/requisitions", "candidateexperience", "oraclecloud", "search-results", "job-search",
    "myworkdayjobs.com", "wd1.myworkday", "wd3.myworkday", "wd5.myworkday",
    "boards.greenhouse.io", "greenhouse.io/embed", "jobs.lever.co",
    "icims.com", "careers-", ".icims.", "smartrecruiters.com/", "taleo.net",
    "successfactors.com", "successfactors.eu", "jobvite.com", "bamboohr.com/careers",
    "ashbyhq.com", "recruitee.com", "hcmcloud", "fa.oraclecloud", "phenom", "avature", "eightfold",
]

KNOWN_ATS_DOMAINS = [
    "boards.greenhouse.io", "greenhouse.io", "jobs.lever.co", "lever.co",
    "myworkdayjobs.com", "myworkday.com", "wd1.myworkday.com", "wd3.myworkday.com", "wd5.myworkday.com",
    "icims.com", "smartrecruiters.com", "taleo.net", "successfactors.com", "successfactors.eu",
    "jobvite.com", "bamboohr.com", "ashbyhq.com", "recruitee.com", "ultipro.com",
    "fa.oraclecloud.com", "phenom.com", "phenompeople.com", "avature.net", "eightfold.ai",
    "applytojob.com", "breezy.hr", "jazz.co", "resumator.com", "workable.com",
]

_SKIP_HREF_FRAGMENTS = [
    "login", "sign-in", "signin", "signup", "sign-up", "register",
    "blog/", "/news", "/about-us", "/about/", "contact-us", "contact/",
    "privacy", "terms", "cookie", "legal", "disclaimer", "faq",
    "linkedin.com", "facebook.com", "twitter.com", "instagram.com",
    "youtube.com", "mailto:", "tel:", "javascript:",
]

_NON_PORTAL_PATH_FRAGMENTS = [
    "why-", "/why/", "life-at", "lifeat", "/life/", "life@",
    "about-", "/about/", "people-", "/people/",
    "culture", "benefits", "diversity", "inclusion",
    "students", "graduates", "internship-program",
    "scam", "fraud", "faq", "contact",
]

MIN_CONFIDENT_JOBS = 3
SECTION_HEADINGS = [
    "required skills", "skills", "qualifications", "requirements",
    "what you need", "you have", "preferred", "nice to have",
]
_LOAD_MORE_PHRASES = [
    "load more", "show more", "view more", "see more",
    "more jobs", "more results", "more positions", "more openings",
    "next page", "next results", "show all", "view all", "see all",
    "load more jobs", "show more jobs", "view more jobs",
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

def _extract_base_domain(url: str) -> Optional[str]:
    try:
        hostname = urlparse(url).hostname or ""
        parts = hostname.split(".")
        if len(parts) >= 2:
            return ".".join(parts[-2:])
    except Exception:
        pass
    return None

def _guess_careers_subdomain(source_url: str) -> Optional[str]:
    base = _extract_base_domain(source_url)
    if base:
        return f"https://careers.{base}"
    return None

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

async def _goto_and_wait(page, url: str, timeout: int = 60000) -> None:
    await page.goto(url, timeout=timeout, wait_until="domcontentloaded")
    try:
        await page.wait_for_load_state("networkidle", timeout=min(timeout, 15000))
    except Exception:
        # Added random jitter to evasion
        await page.wait_for_timeout(2500 + random.randint(100, 700))

# ---------------------------------------------------------------------------
# Link discovery functions (Async)
# ---------------------------------------------------------------------------

async def detect_ats_links(page) -> List[str]:
    ats_links: List[str] = []
    seen: set = set()
    anchors = await page.query_selector_all("a[href]")
    for anchor in anchors:
        href = await anchor.get_attribute("href") or ""
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

async def detect_iframe_job_boards(page) -> List[str]:
    iframe_urls: List[str] = []
    seen: set = set()
    iframes = await page.query_selector_all("iframe[src]")
    for iframe in iframes:
        src = await iframe.get_attribute("src") or ""
        if not src:
            continue
        src_lower = src.lower()
        for domain in KNOWN_ATS_DOMAINS:
            if domain in src_lower:
                if src not in seen:
                    seen.add(src)
                    iframe_urls.append(src)
                break
        else:
            if any(hint in src_lower for hint in CAREERS_PORTAL_HINTS):
                if src not in seen:
                    seen.add(src)
                    iframe_urls.append(src)
    return iframe_urls

async def find_careers_links(page) -> List[str]:
    candidates: List[tuple] = []
    seen_urls: set = set()
    anchors = await page.query_selector_all("a[href]")

    for anchor in anchors:
        href = await anchor.get_attribute("href") or ""
        if not href or href.startswith("#"):
            continue

        text = _clean_text(await anchor.inner_text()) or ""
        aria = await anchor.get_attribute("aria-label") or ""
        title_attr = await anchor.get_attribute("title") or ""

        href_lower = href.lower()
        text_lower = text.lower()
        label_lower = f"{aria} {title_attr}".lower()

        if any(skip in href_lower for skip in _SKIP_HREF_FRAGMENTS):
            continue

        score = 0
        if any(hint in href_lower for hint in CAREERS_PORTAL_HINTS):
            score += 3
        if "careers" in href_lower or "jobs" in href_lower:
            score += 2
        if _text_contains_keywords(text_lower, CAREERS_KEYWORDS):
            score += 3
        if _text_contains_keywords(label_lower, CAREERS_KEYWORDS):
            score += 1
        if score >= 5:
            score += 2

        for domain in KNOWN_ATS_DOMAINS:
            if domain in href_lower:
                score += 5
                break

        if score > 0:
            normalized = _normalize_url(page.url, href)
            if normalized not in seen_urls and normalized != page.url:
                seen_urls.add(normalized)
                candidates.append((score, normalized))

    candidates.sort(key=lambda x: x[0], reverse=True)
    return [url for _, url in candidates]

async def find_portal_links(page) -> List[str]:
    scored: List[tuple] = []
    seen: set = set()
    page_host = urlparse(page.url).hostname or ""

    def _is_non_portal(url: str) -> bool:
        path = urlparse(url).path.lower()
        return any(frag in path for frag in _NON_PORTAL_PATH_FRAGMENTS)

    def _add(priority: int, url: str) -> None:
        if url and url not in seen and url != page.url:
            if _is_non_portal(url):
                priority = max(priority - 4, 0)
            seen.add(url)
            scored.append((priority, url))

    for url in await detect_ats_links(page):
        _add(10, url)

    for url in await detect_iframe_job_boards(page):
        _add(9, url)

    anchors = await page.query_selector_all("a[href]")

    for anchor in anchors:
        href = await anchor.get_attribute("href") or ""
        href_lower = href.lower()
        if any(hint in href_lower for hint in PREFERRED_PORTAL_HINTS):
            _add(8, _normalize_url(page.url, href))

    for anchor in anchors:
        href = await anchor.get_attribute("href") or ""
        text = (_clean_text(await anchor.inner_text()) or "").lower()
        try:
            href_host = urlparse(href).hostname or ""
        except Exception:
            href_host = ""

        if href_host and href_host != page_host and href_host.startswith("careers"):
            priority = 8
            if any(kw in text for kw in ["apply", "search", "view", "browse", "open"]):
                priority = 9
            _add(priority, href)

    for anchor in anchors:
        href = await anchor.get_attribute("href") or ""
        text = (_clean_text(await anchor.inner_text()) or "").lower()
        href_lower = href.lower()
        if any(hint in href_lower for hint in CAREERS_PORTAL_HINTS):
            _add(5, _normalize_url(page.url, href))
        if "job" in href_lower and any(kw in text for kw in ["apply", "view", "open", "job", "search", "browse", "see all"]):
            _add(4, _normalize_url(page.url, href))

    for btn in await page.query_selector_all("a, button"):
        text = (_clean_text(await btn.inner_text()) or "").lower()
        if any(phrase in text for phrase in ["view open positions", "see open positions", "browse jobs", "search jobs", "explore careers"]):
            href = await btn.get_attribute("href") or ""
            onclick = await btn.get_attribute("onclick") or ""
            if href and not href.startswith("#"):
                _add(7, _normalize_url(page.url, href))
            elif onclick:
                m = re.search(r"""(?:location\.href|window\.open)\s*\(\s*['"]([^'"]+)['"]""", onclick)
                if m:
                    _add(7, _normalize_url(page.url, m.group(1)))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [url for _, url in scored]

async def gather_job_links(page) -> List[str]:
    links: set = set()
    anchors = await page.query_selector_all("a[href]")
    for anchor in anchors:
        href = await anchor.get_attribute("href") or ""
        if not href:
            continue
        href_lower = href.lower()

        if "/hvhapply" in href_lower or "jobcart" in href_lower:
            continue

        parsed = urlparse(href_lower)
        path_and_query = parsed.path + ("?" + parsed.query if parsed.query else "")

        if any(frag in path_and_query for frag in _NON_PORTAL_PATH_FRAGMENTS):
            continue

        matched = False
        if any(hint in href_lower for hint in JOB_PORTAL_HINTS):
            matched = True

        if not matched:
            text = (_clean_text(await anchor.inner_text()) or "").lower()
            if len(text) > 10 and any(kw in text for kw in ["apply now", "view details", "learn more", "view job"]):
                if any(frag in path_and_query for frag in ["job", "position", "career", "role", "opening", "requisition", "posting", "vacancy", "apply"]):
                    matched = True

        if matched:
            links.add(_normalize_url(page.url, href))

    return sorted(links)

# ---------------------------------------------------------------------------
# Pagination helpers (Async)
# ---------------------------------------------------------------------------

async def _find_load_more_button(page):
    selectors = ["button", "a", "[role='button']", "div", "span"]
    for selector in selectors:
        elements = await page.query_selector_all(selector)
        for el in elements:
            if not await el.is_visible():
                continue
            text = (_clean_text(await el.inner_text()) or "").lower()
            aria = (await el.get_attribute("aria-label") or "").lower()
            combined = f"{text} {aria}"
            if any(phrase in combined for phrase in _LOAD_MORE_PHRASES):
                if len(text) > 80:
                    continue
                return el
    return None

async def _click_load_more_for_jobs(page, company: str, existing_links: List[str], max_jobs: int, max_clicks: int = 50, wait_ms: int = 3000) -> List[str]:
    all_links = list(existing_links)
    all_set = set(all_links)
    no_new_streak = 0

    for click_num in range(1, max_clicks + 1):
        if len(all_links) >= max_jobs:
            break

        btn = await _find_load_more_button(page)
        if btn is None:
            print(f"[{company}] No more 'Load More' button found after {click_num - 1} click(s)")
            break

        try:
            await btn.scroll_into_view_if_needed(timeout=3000)
            await btn.click(timeout=5000)
            # Add anti-bot jitter
            await page.wait_for_timeout(wait_ms + random.randint(100, 700))
        except Exception as exc:
            print(f"[{company}] 'Load More' click failed: {exc}")
            break

        new_links = await gather_job_links(page)
        added = 0
        for link in new_links:
            if link not in all_set:
                all_set.add(link)
                all_links.append(link)
                added += 1

        print(f"[{company}] 'Load More' click #{click_num}: +{added} new links (total {len(all_links)})")

        if added == 0:
            no_new_streak += 1
            if no_new_streak >= 2:
                print(f"[{company}] No new links after 2 consecutive clicks — stopping")
                break
        else:
            no_new_streak = 0

    return all_links

async def _scroll_for_more_jobs(page, company: str, existing_links: List[str], max_jobs: int, max_scrolls: int = 15, wait_ms: int = 2000) -> List[str]:
    all_links = list(existing_links)
    all_set = set(all_links)
    no_new_streak = 0

    for i in range(1, max_scrolls + 1):
        if len(all_links) >= max_jobs:
            break

        await page.mouse.wheel(0, 3000)
        await page.wait_for_timeout(wait_ms + random.randint(100, 500))

        new_links = await gather_job_links(page)
        added = 0
        for link in new_links:
            if link not in all_set:
                all_set.add(link)
                all_links.append(link)
                added += 1

        if added:
            print(f"[{company}] Scroll #{i}: +{added} new links (total {len(all_links)})")
            no_new_streak = 0
        else:
            no_new_streak += 1
            if no_new_streak >= 3:
                print(f"[{company}] No new links after 3 consecutive scrolls — stopping")
                break

    return all_links

async def _discover_link_rel_next(page) -> List[str]:
    urls: List[str] = []
    for link_el in await page.query_selector_all('link[rel="next"]'):
        href = await link_el.get_attribute("href") or ""
        if href:
            urls.append(_normalize_url(page.url, href))
    return urls

async def discover_pagination_urls(page, max_pages: int = 10) -> List[str]:
    candidates = []
    for url in await _discover_link_rel_next(page):
        candidates.append(url)

    _PAGINATION_PARAMS = [
        "?page=", "&page=", "page=", "?pg=", "&pg=", "/page/", "/pg/",
        "?from=", "&from=", "?offset=", "&offset=", "?start=", "&start=",
    ]
    anchors = await page.query_selector_all("a[href]")
    for a in anchors:
        href = await a.get_attribute("href") or ""
        if not href:
            continue
        h = href.lower()
        if any(p in h for p in _PAGINATION_PARAMS):
            candidates.append(_normalize_url(page.url, href))
        else:
            text = (_clean_text(await a.inner_text()) or "").strip()
            if re.fullmatch(r"\d{1,3}", text):
                candidates.append(_normalize_url(page.url, href))

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

def construct_query_pages(base_url: str, max_pages: int = 10, page_size: int = 10) -> List[str]:
    pages = []
    sep = "&" if "?" in base_url else "?"
    for p in range(1, max_pages):
        pages.append(f"{base_url}{sep}from={p * page_size}&s=1")
    for p in range(2, max_pages + 1):
        pages.append(f"{base_url}{sep}page={p}")
    return pages


# ---------------------------------------------------------------------------
# Job-field extraction helpers (Async)
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

async def extract_job_fields(page, company: str) -> Dict:
    # NARROWING EXTRACTION SCOPE to avoid huge headers/footers
    content_element = await page.query_selector("main, article, #content, .job-description, [data-ui='job-description']")
    if not content_element:
        content_element = await page.query_selector("body")

    text = await content_element.inner_text() if content_element else ""
    text = str(text)

    title = None
    for selector in ["h1", "h2", "title"]:
        element = await page.query_selector(selector)
        if element:
            candidate = _clean_text(await element.inner_text())
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
        text, re.IGNORECASE,
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
    paragraphs = await page.query_selector_all("p")
    if paragraphs:
        for paragraph in paragraphs:
            candidate = _clean_text(await paragraph.inner_text())
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
# Core scraping logic (Async)
# ---------------------------------------------------------------------------

async def _try_gather_jobs(page, company: str, wait_ms: int = 4000) -> List[str]:
    job_links = await gather_job_links(page)
    if len(job_links) < 5:
        await page.wait_for_timeout(wait_ms + random.randint(100, 500))
        job_links = await gather_job_links(page)
    return job_links

async def _follow_pagination(page, job_links: List[str], company: str, max_jobs: int) -> List[str]:
    if len(job_links) >= max_jobs:
        return job_links

    count_before = len(job_links)
    portal_url = page.url
    job_set = set(job_links)
    pages_visited: set = set()

    def _add_links(new_links: List[str]) -> int:
        added = 0
        for link in new_links:
            if link not in job_set:
                job_set.add(link)
                job_links.append(link)
                added += 1
        return added

    next_urls = await _discover_link_rel_next(page)
    if next_urls:
        print(f"[{company}] Found <link rel=\"next\"> — following pagination chain")
        max_chain = max_jobs // 5
        chain_count = 0
        while next_urls and len(job_links) < max_jobs and chain_count < max_chain:
            next_url = next_urls[0]
            if next_url in pages_visited:
                break
            pages_visited.add(next_url)
            chain_count += 1
            try:
                print(f"[{company}] Pagination chain [{chain_count}]: {next_url}")
                await _goto_and_wait(page, next_url)
                await page.wait_for_timeout(2000 + random.randint(100, 500))
                new_links = await gather_job_links(page)
                added = _add_links(new_links)
                print(f"[{company}]   +{added} new links (total {len(job_links)})")
                if added == 0:
                    break
                next_urls = await _discover_link_rel_next(page)
            except Exception as exc:
                print(f"[{company}] Pagination chain failed: {exc}")
                break

    if len(job_links) < max_jobs:
        pagination_urls = await discover_pagination_urls(page, max_pages=20)
        pagination_urls = [u for u in pagination_urls if u not in pages_visited]
        if pagination_urls:
            for purl in pagination_urls:
                if len(job_links) >= max_jobs:
                    break
                pages_visited.add(purl)
                try:
                    print(f"[{company}] Following pagination: {purl}")
                    await _goto_and_wait(page, purl)
                    await page.wait_for_timeout(2000 + random.randint(100, 500))
                    new_links = await gather_job_links(page)
                    added = _add_links(new_links)
                    if added == 0:
                        break
                except Exception as exc:
                    print(f"[{company}] Pagination visit failed: {exc}")

    if len(job_links) < max_jobs and len(job_links) - count_before < 5:
        try:
            await _goto_and_wait(page, portal_url)
        except Exception:
            pass
        fallback_urls = construct_query_pages(portal_url, max_pages=20)
        fallback_urls = [u for u in fallback_urls if u not in pages_visited]
        no_new_streak = 0
        for furl in fallback_urls:
            if len(job_links) >= max_jobs or no_new_streak >= 2:
                break
            pages_visited.add(furl)
            try:
                print(f"[{company}] Trying constructed pagination: {furl}")
                await _goto_and_wait(page, furl)
                await page.wait_for_timeout(2000 + random.randint(100, 500))
                new_links = await gather_job_links(page)
                added = _add_links(new_links)
                if added == 0:
                    no_new_streak += 1
                else:
                    no_new_streak = 0
            except Exception as exc:
                print(f"[{company}] Constructed pagination failed: {exc}")
                no_new_streak += 1

    if len(job_links) < max_jobs and len(job_links) - count_before < 5:
        try:
            await _goto_and_wait(page, portal_url)
        except Exception:
            pass
        if await _find_load_more_button(page):
            print(f"[{company}] Found 'Load More' button — clicking to load all jobs")
            job_links = await _click_load_more_for_jobs(page, company, job_links, max_jobs)

    if len(job_links) < max_jobs and len(job_links) - count_before < 5:
        job_links = await _scroll_for_more_jobs(page, company, job_links, max_jobs)

    return job_links

async def _try_scroll_for_jobs(page, company: str, max_scrolls: int = 5) -> List[str]:
    print(f"[{company}] Scrolling page to trigger lazy-loaded content...")
    for i in range(max_scrolls):
        await page.mouse.wheel(0, 3000)
        await page.wait_for_timeout(1500 + random.randint(100, 400))
        links = await gather_job_links(page)
        if links:
            print(f"[{company}] Found {len(links)} job links after {i + 1} scroll(s)")
            return links
    return []

async def scrape_company(page, source: Source, max_jobs: int) -> List[Dict]:
    await _goto_and_wait(page, source.url)
    company = source.company

    careers_candidates = await find_careers_links(page)
    ats_direct = await detect_ats_links(page)
    iframe_direct = await detect_iframe_job_boards(page)

    all_portal_candidates = list(dict.fromkeys(ats_direct + iframe_direct))

    if all_portal_candidates:
        print(f"[{company}] Found {len(all_portal_candidates)} ATS/iframe link(s) on homepage")
    if careers_candidates:
        print(f"[{company}] Found {len(careers_candidates)} careers link candidate(s) on homepage")
    else:
        print(f"[{company}] No careers link discovered on homepage")

    job_links: List[str] = []

    for portal_url in all_portal_candidates[:2]:
        print(f"[{company}] Trying ATS portal from homepage: {portal_url}")
        try:
            await _goto_and_wait(page, portal_url)
            job_links = await _try_gather_jobs(page, company)
            if len(job_links) >= MIN_CONFIDENT_JOBS:
                job_links = await _follow_pagination(page, job_links, company, max_jobs)
                print(f"[{company}] Discovered {len(job_links)} job links from direct portal")
                break
            elif job_links:
                print(f"[{company}] Only {len(job_links)} link(s) from portal — trying others")
        except Exception as exc:
            print(f"[{company}] Failed to load ATS portal {portal_url}: {exc}")

    if not job_links:
        for careers_url in careers_candidates[:3]:
            print(f"[{company}] Visiting careers page: {careers_url}")
            try:
                await _goto_and_wait(page, careers_url)
            except Exception as exc:
                print(f"[{company}] Failed to load careers page {careers_url}: {exc}")
                continue

            job_links = await _try_gather_jobs(page, company)
            if len(job_links) >= MIN_CONFIDENT_JOBS:
                job_links = await _follow_pagination(page, job_links, company, max_jobs)
                print(f"[{company}] Discovered {len(job_links)} job links on careers page")
                break

            portal_candidates = await find_portal_links(page)
            if portal_candidates:
                print(f"[{company}] Found {len(portal_candidates)} portal candidate(s) on careers page")

            for portal_url in portal_candidates[:2]:
                print(f"[{company}] Trying portal: {portal_url}")
                try:
                    await _goto_and_wait(page, portal_url)
                    job_links = await _try_gather_jobs(page, company)
                    if len(job_links) >= MIN_CONFIDENT_JOBS:
                        job_links = await _follow_pagination(page, job_links, company, max_jobs)
                        print(f"[{company}] Discovered {len(job_links)} job links from portal")
                        break
                except Exception as exc:
                    print(f"[{company}] Failed to load portal {portal_url}: {exc}")

            if job_links: break

            sub_links = await find_careers_links(page)
            for sub_url in sub_links[:2]:
                if sub_url == careers_url: continue
                print(f"[{company}] Trying depth-2 sub-link: {sub_url}")
                try:
                    await _goto_and_wait(page, sub_url)
                    job_links = await _try_gather_jobs(page, company)
                    if job_links:
                        job_links = await _follow_pagination(page, job_links, company, max_jobs)
                        break
                    
                    deep_portals = await find_portal_links(page)
                    for dp_url in deep_portals[:1]:
                        print(f"[{company}] Trying depth-2 portal: {dp_url}")
                        try:
                            await _goto_and_wait(page, dp_url)
                            job_links = await _try_gather_jobs(page, company)
                            if job_links:
                                job_links = await _follow_pagination(page, job_links, company, max_jobs)
                                break
                        except Exception: pass
                except Exception: pass

                if job_links: break
            if job_links: break

    if len(job_links) < MIN_CONFIDENT_JOBS:
        guessed_url = _guess_careers_subdomain(source.url)
        if guessed_url:
            print(f"[{company}] Trying guessed careers subdomain: {guessed_url}")
            try:
                await _goto_and_wait(page, guessed_url)
                guessed_jobs = await _try_gather_jobs(page, company)
                if guessed_jobs:
                    portal_on_sub = await find_portal_links(page)
                    if portal_on_sub:
                        for p_url in portal_on_sub[:2]:
                            try:
                                await _goto_and_wait(page, p_url)
                                sub_jobs = await _try_gather_jobs(page, company)
                                if len(sub_jobs) > len(guessed_jobs):
                                    guessed_jobs = sub_jobs
                                if len(guessed_jobs) >= MIN_CONFIDENT_JOBS:
                                    break
                            except Exception: pass
                    guessed_jobs = await _follow_pagination(page, guessed_jobs, company, max_jobs)
                    if len(guessed_jobs) > len(job_links):
                        job_links = guessed_jobs
            except Exception as exc:
                print(f"[{company}] Careers subdomain not reachable: {exc}")

    if not job_links:
        best_page_url = all_portal_candidates[0] if all_portal_candidates else careers_candidates[0] if careers_candidates else source.url
        print(f"[{company}] Last resort: trying Load More + scroll on {best_page_url}")
        try:
            await _goto_and_wait(page, best_page_url)
            if await _find_load_more_button(page):
                job_links = await _click_load_more_for_jobs(page, company, job_links, max_jobs)
            if not job_links:
                job_links = await _try_scroll_for_jobs(page, company)
        except Exception: pass

    print(f"[{company}] Total discovered: {len(job_links)} candidate job links")
    jobs: List[Dict] = []
    
    for index, link in enumerate(job_links[:max_jobs]):
        try:
            print(f"[{company}] Visiting [{index + 1}] {link}")
            await _goto_and_wait(page, link)
            fields = await extract_job_fields(page, source.company)
            fields["apply_url"] = link
            fields["source_url"] = source.url
            jobs.append(fields)
        except Exception as exc:
            print(f"[{company}] Error visiting {link}: {exc}")

    return jobs

# ---------------------------------------------------------------------------
# Concurrency / I/O helpers
# ---------------------------------------------------------------------------

async def process_company(browser, source: Source, max_jobs: int, sem: asyncio.Semaphore) -> List[Dict]:
    """Wraps scrape_company inside a semaphore with context isolation."""
    async with sem:
        # BROWSER ISOLATION: A clean context (new cookies/session) for every company
        context = await browser.new_context()
        page = await context.new_page()
        # ANTI-BOT: Apply playwright-stealth to the new page
        await stealth_async(page)
        
        try:
            jobs = await scrape_company(page, source, max_jobs)
            return jobs
        except Exception as exc:
            print(f"[{source.company}] Failed during processing: {exc}")
            return []
        finally:
            await context.close()

def save_json(path: str, data) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)

def save_csv(path: str, jobs: List[Dict]) -> None:
    fields = [
        "company", "role", "title", "department", "location", "posting_date",
        "remote_status", "employment_type", "apply_url", "required_skills", "description",
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
# CLI (Async Main)
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape job listings asynchronously with stealth capabilities.")
    parser.add_argument("--sources-file", required=True, help="File with company names and URLs")
    parser.add_argument("--out", default="jobs.json", help="Output JSON path")
    parser.add_argument("--out-csv", default=None, help="Output CSV path")
    parser.add_argument("--max-jobs", type=int, default=100, help="Maximum jobs per company")
    parser.add_argument("--headless", action="store_true", help="Run browser headless")
    parser.add_argument("--concurrency", type=int, default=3, help="Number of companies to scrape concurrently")
    parser.add_argument("--fallback-portal", default=None, help=argparse.SUPPRESS)
    return parser.parse_args()


async def async_main(args: argparse.Namespace) -> None:
    if args.fallback_portal:
        warnings.warn("--fallback-portal is deprecated.", DeprecationWarning, stacklevel=2)

    sources = load_sources_from_file(args.sources_file)
    if not sources:
        print("No sources found in file.")
        return

    # Initialize semaphore to limit concurrent open browsers
    sem = asyncio.Semaphore(args.concurrency)
    all_jobs: List[Dict] = []

    async with async_playwright() as playwright:
        # CONCURRENCY FIX: Use one browser instance, but multi-thread the contexts
        browser = await playwright.chromium.launch(headless=args.headless)
        
        # Schedule all company tasks
        tasks = [
            asyncio.create_task(process_company(browser, source, args.max_jobs, sem))
            for source in sources
        ]
        
        # Await them as they complete
        results = await asyncio.gather(*tasks)
        
        for company_jobs in results:
            if company_jobs:
                all_jobs.extend(company_jobs)

        await browser.close()

    save_json(args.out, {"jobs": all_jobs})
    if args.out_csv:
        save_csv(args.out_csv, all_jobs)

    print(f"Saved {len(all_jobs)} jobs to {args.out}")


def main(argv: Optional[List[str]] = None) -> None:
    if argv is not None:
        old_argv = sys.argv
        sys.argv = [old_argv[0], *argv]
    try:
        args = parse_args()
    finally:
        if argv is not None:
            sys.argv = old_argv
            
    asyncio.run(async_main(args))

if __name__ == "__main__":
    main()