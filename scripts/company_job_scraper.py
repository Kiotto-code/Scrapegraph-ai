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
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse

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

# URL fragments that frequently appear on careers sites but are not
# individual job detail pages.
_NON_JOB_DETAIL_FRAGMENTS = [
    "jobalerts", "job-alert", "job-alerts", "job-alerts",
    "candidatehome", "talentcommunity", "talent-community",
    "/login", "signin", "sign-in", "/register", "createprofile",
]

_TRACKING_OR_PAGINATION_QUERY_KEYS = {
    "from", "s", "page", "pg", "start", "offset",
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "fbclid",
}

# URL path fragments that indicate an informational page, NOT a job portal.
_NON_PORTAL_PATH_FRAGMENTS = [
    "why-", "/why/", "life-at", "lifeat", "/life/", "life@",
    "about-", "/about/", "people-", "/people/",
    "culture", "benefits", "diversity", "inclusion",
    "students", "graduates", "internship-program",
    "scam", "fraud", "faq", "contact",
]

# Minimum number of job links to consider a portal discovery "confident".
# If fewer links are found, the scraper will continue trying other portals.
MIN_CONFIDENT_JOBS = 3

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


def _canonicalize_discovered_job_url(url: str) -> str:
    """Normalize discovered job URLs to avoid pagination/tracking duplicates."""
    parsed = urlparse(url)
    kept_query_items = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_OR_PAGINATION_QUERY_KEYS
    ]
    normalized_query = urlencode(kept_query_items, doseq=True)
    normalized_path = re.sub(r"/{2,}", "/", parsed.path)
    if normalized_path != "/":
        normalized_path = normalized_path.rstrip("/")
    return parsed._replace(path=normalized_path, query=normalized_query, fragment="").geturl()


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


def _extract_base_domain(url: str) -> Optional[str]:
    """Extract the registrable domain from a URL (e.g. 'www.ppg.com' → 'ppg.com')."""
    try:
        hostname = urlparse(url).hostname or ""
        parts = [p for p in hostname.split(".") if p]
        if len(parts) < 2:
            return None

        # Handle common ccTLD patterns like shell.com.my -> shell.com.my
        # (instead of the incorrect com.my).
        common_cc_slds = {
            "co.uk", "org.uk", "gov.uk", "ac.uk",
            "com.au", "net.au", "org.au",
            "co.nz", "org.nz", "govt.nz",
            "com.my", "com.sg", "com.hk", "com.cn",
            "co.jp", "ne.jp", "or.jp",
            "com.br", "com.mx", "com.tr", "com.sa", "com.ar",
        }
        last_two = ".".join(parts[-2:])
        if last_two in common_cc_slds and len(parts) >= 3:
            return ".".join(parts[-3:])

        return last_two
    except Exception:
        pass
    return None


def _guess_careers_subdomain(source_url: str) -> Optional[str]:
    """Construct a careers.{domain} URL to try when no portal is found.

    Many companies host their job board on a subdomain like
    careers.ppg.com or careers.petronas.com even when the main website
    doesn't link to it.
    """
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
    Filters out links that point to informational (non-portal) pages.
    """
    scored: List[tuple] = []  # (priority, url)
    seen: set = set()
    page_host = urlparse(page.url).hostname or ""

    def _is_non_portal(url: str) -> bool:
        """Return True if the URL path looks like an informational page."""
        path = urlparse(url).path.lower()
        return any(frag in path for frag in _NON_PORTAL_PATH_FRAGMENTS)

    def _add(priority: int, url: str) -> None:
        if url and url not in seen and url != page.url:
            # Penalise informational pages
            if _is_non_portal(url):
                priority = max(priority - 4, 0)
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

    # Priority 4 – external careers subdomain (e.g. careers.petronas.com)
    for anchor in anchors:
        href = anchor.get_attribute("href") or ""
        text = (_clean_text(anchor.inner_text()) or "").lower()
        href_lower = href.lower()
        try:
            href_host = urlparse(href).hostname or ""
        except Exception:
            href_host = ""

        # Careers subdomain that is different from current page
        if href_host and href_host != page_host and href_host.startswith("careers"):
            priority = 8
            # Extra boost if the text contains action words
            if any(kw in text for kw in ["apply", "search", "view", "browse", "open"]):
                priority = 9
            _add(priority, href)

    # Priority 5 – generic careers portal hint in href
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

    # Priority 6 – buttons / link-buttons with portal-like text
    for btn in page.query_selector_all("a, button"):
        text = (_clean_text(btn.inner_text()) or "").lower()
        if any(phrase in text for phrase in [
            "view open positions", "see open positions", "browse jobs",
            "search jobs", "explore careers", "view all jobs",
            "see all jobs", "find a job", "search openings",
            "view opportunities", "current openings",
            "apply for careers", "apply here",
        ]):
            href = btn.get_attribute("href") or ""
            onclick = btn.get_attribute("onclick") or ""
            if href and not href.startswith("#"):
                _add(7, _normalize_url(page.url, href))
            elif onclick:
                # Try to extract URL from onclick="location.href='...'"
                m = re.search(r"""(?:location\.href|window\.open)\s*\(\s*['"]([^'"]+)['"]""", onclick)
                if m:
                    _add(7, _normalize_url(page.url, m.group(1)))

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
        if any(skip in href_lower for skip in _SKIP_HREF_FRAGMENTS):
            continue
        if any(skip in href_lower for skip in _NON_JOB_DETAIL_FRAGMENTS):
            continue

        # Parse the URL to isolate the path and query (ignoring domain)
        parsed = urlparse(href_lower)
        path_and_query = parsed.path + ("?" + parsed.query if parsed.query else "")

        # Skip paths that clearly indicate non-job / informational pages
        if any(frag in path_and_query for frag in _NON_PORTAL_PATH_FRAGMENTS):
            continue

        matched = False

        # Check expanded URL-pattern hints (against the full href to catch ATS domains)
        if any(hint in href_lower for hint in JOB_PORTAL_HINTS):
            matched = True

        # Content-based: link text that looks like a job title or action
        if not matched:
            text = (_clean_text(anchor.inner_text()) or "").lower()
            # Links whose text contains "apply" alongside a non-trivial label
            if len(text) > 10 and any(kw in text for kw in [
                "apply now", "view details", "learn more", "view job",
            ]):
                # Only if the path/query (NOT the domain) also hints at a job
                if any(frag in path_and_query for frag in [
                    "job", "position", "career", "role", "opening", "requisition",
                    "posting", "vacancy", "apply",
                ]):
                    matched = True

        if matched:
            discovered = _normalize_url(page.url, href)
            links.add(_canonicalize_discovered_job_url(discovered))

    return sorted(links)


# ---------------------------------------------------------------------------
# Pagination helpers
# ---------------------------------------------------------------------------

# Phrases that identify a "load more" / "show more" button on the page.
_LOAD_MORE_PHRASES = [
    "load more", "show more", "view more", "see more",
    "more jobs", "more results", "more positions", "more openings",
    "next page", "next results",
    "show all", "view all", "see all",
    "load more jobs", "show more jobs", "view more jobs",
    "load more results", "show more results",
]


def _find_load_more_button(page):
    """Locate a 'Load More' / 'Show More' button or link on the page.

    Returns the first matching Playwright element handle, or *None*.
    Searches ``<button>``, ``<a>``, and common ``<div>``/``<span>`` wrappers
    using both visible text and ``aria-label``.
    """
    # Selectors ordered from most to least specific.
    selectors = ["button", "a", "[role='button']", "div", "span"]

    for selector in selectors:
        elements = page.query_selector_all(selector)
        for el in elements:
            # Skip invisible / zero-size elements
            if not el.is_visible():
                continue

            text = (_clean_text(el.inner_text()) or "").lower()
            aria = (el.get_attribute("aria-label") or "").lower()

            combined = f"{text} {aria}"
            if any(phrase in combined for phrase in _LOAD_MORE_PHRASES):
                # Avoid matching giant containers whose text *contains*
                # the phrase but are not themselves buttons.
                if len(text) > 80:
                    continue
                return el

    return None


def _click_load_more_for_jobs(
    page, company: str, existing_links: List[str], max_jobs: int,
    max_clicks: int = 50, wait_ms: int = 3000,
) -> List[str]:
    """Repeatedly click a 'Load More' button to reveal additional job listings.

    Returns the accumulated list of job links (including *existing_links*).
    Stops when:
    - ``max_jobs`` is reached,
    - ``max_clicks`` is exhausted,
    - the button disappears, or
    - two consecutive clicks yield no new links.
    """
    all_links = list(existing_links)
    all_set = set(all_links)
    no_new_streak = 0

    for click_num in range(1, max_clicks + 1):
        if len(all_links) >= max_jobs:
            break

        btn = _find_load_more_button(page)
        if btn is None:
            print(f"[{company}] No more 'Load More' button found after {click_num - 1} click(s)")
            break

        try:
            # Scroll the button into view and click it.
            btn.scroll_into_view_if_needed(timeout=3000)
            btn.click(timeout=5000)
            page.wait_for_timeout(wait_ms)
        except Exception as exc:
            print(f"[{company}] 'Load More' click failed: {exc}")
            break

        new_links = gather_job_links(page)
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


def _scroll_for_more_jobs(
    page, company: str, existing_links: List[str], max_jobs: int,
    max_scrolls: int = 15, wait_ms: int = 2000,
) -> List[str]:
    """Scroll the page to trigger infinite-scroll / lazy-loaded job listings.

    Unlike the old ``_try_scroll_for_jobs`` this works *incrementally* on top
    of already-discovered links and stops when no new links appear.
    """
    all_links = list(existing_links)
    all_set = set(all_links)
    no_new_streak = 0

    for i in range(1, max_scrolls + 1):
        if len(all_links) >= max_jobs:
            break

        page.mouse.wheel(0, 3000)
        page.wait_for_timeout(wait_ms)

        new_links = gather_job_links(page)
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


def _discover_link_rel_next(page) -> List[str]:
    """Extract pagination URLs from ``<link rel="next">`` tags.

    Many ATS platforms (Phenom, etc.) embed SEO pagination hints as
    ``<link rel="next" href="...">`` in the ``<head>``.  These are a
    very reliable signal for the next page URL.
    """
    urls: List[str] = []
    for link_el in page.query_selector_all('link[rel="next"]'):
        href = link_el.get_attribute("href") or ""
        if href:
            urls.append(_normalize_url(page.url, href))
    return urls


def _detect_phenom_portal(page) -> bool:
    """Return True if the current page appears to be a Phenom People portal."""
    html = page.content() or ""
    return "phenompeople.com" in html or "phenompeople" in html.lower() or "phApp" in html


def discover_pagination_urls(page, max_pages: int = 10) -> List[str]:
    """Find candidate pagination URLs from the current page.

    Looks for:
    - ``<link rel="next">`` tags (highest priority)
    - Anchors containing pagination query params (page, pg, from, offset, start)
    - Numeric page links

    Returns absolute URLs (unique, stable order).
    """
    candidates = []

    # --- High-priority: <link rel="next"> ---
    for url in _discover_link_rel_next(page):
        candidates.append(url)

    # --- Anchor-based discovery ---
    _PAGINATION_PARAMS = [
        "?page=", "&page=", "page=",
        "?pg=", "&pg=",
        "/page/", "/pg/",
        # Phenom People / offset-based portals
        "?from=", "&from=",
        "?offset=", "&offset=",
        "?start=", "&start=",
    ]
    anchors = page.query_selector_all("a[href]")
    for a in anchors:
        href = a.get_attribute("href") or ""
        if not href:
            continue
        h = href.lower()
        if any(p in h for p in _PAGINATION_PARAMS):
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


def construct_query_pages(base_url: str, max_pages: int = 10, page_size: int = 10) -> List[str]:
    """Construct paginated URL variants for a base URL.

    Tries both ``?page=N`` (common) and ``?from=N&s=1`` (Phenom People)
    patterns.  Only used as a fallback when no explicit pagination anchors
    exist.
    """
    pages = []
    sep = "&" if "?" in base_url else "?"

    # Try ?from=N (Phenom People-style offset pagination)
    # This is tried first because ?page= is more generic and more likely
    # to be silently ignored by a Phenom portal.
    for p in range(1, max_pages):
        pages.append(f"{base_url}{sep}from={p * page_size}&s=1")
    # Also try ?page=N as a fallback
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
    """Follow pagination to collect more job URLs.

    Tries four strategies in order:
    1. ``<link rel="next">`` chain (Phenom People and SEO-standard portals).
    2. Traditional URL-based pagination (``?page=N`` anchor links).
    3. Constructed ``?from=N`` / ``?page=N`` URLs as fallback.
    4. 'Load More' / 'Show More' button clicking (JS-driven portals).
    5. Scrolling (infinite-scroll portals).
    """
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

    # --- Strategy 1: Follow <link rel="next"> chain ---
    # Each page may have a <link rel="next"> pointing to the next page.
    # We follow this chain until it ends or max_jobs is reached.
    next_urls = _discover_link_rel_next(page)
    if next_urls:
        print(f"[{company}] Found <link rel=\"next\"> — following pagination chain")
        max_chain = max_jobs // 5  # safety limit on chain length
        chain_count = 0
        while next_urls and len(job_links) < max_jobs and chain_count < max_chain:
            next_url = next_urls[0]
            if next_url in pages_visited:
                break
            pages_visited.add(next_url)
            chain_count += 1
            try:
                print(f"[{company}] Pagination chain [{chain_count}]: {next_url}")
                _goto_and_wait(page, next_url)
                page.wait_for_timeout(2000)  # wait for JS rendering
                new_links = gather_job_links(page)
                added = _add_links(new_links)
                print(f"[{company}]   +{added} new links (total {len(job_links)})")
                if added == 0:
                    # No new links on this page — might be at the end
                    break
                # Discover the next page
                next_urls = _discover_link_rel_next(page)
            except Exception as exc:
                print(f"[{company}] Pagination chain failed: {exc}")
                break

    # --- Strategy 2: Traditional URL pagination (anchors) ---
    if len(job_links) < max_jobs:
        pagination_urls = discover_pagination_urls(page, max_pages=20)
        # Filter out already-visited pages
        pagination_urls = [u for u in pagination_urls if u not in pages_visited]
        if pagination_urls:
            for purl in pagination_urls:
                if len(job_links) >= max_jobs:
                    break
                pages_visited.add(purl)
                try:
                    print(f"[{company}] Following pagination: {purl}")
                    _goto_and_wait(page, purl)
                    page.wait_for_timeout(2000)
                    new_links = gather_job_links(page)
                    added = _add_links(new_links)
                    if added == 0:
                        # If a page returns no new links, stop
                        break
                    page.wait_for_timeout(1000)
                except Exception as exc:
                    print(f"[{company}] Pagination visit failed: {exc}")

    # --- Strategy 3: Construct ?from=N fallback URLs ---
    if len(job_links) < max_jobs and len(job_links) - count_before < 5:
        # Navigate back to portal so we can construct URLs from the base
        try:
            _goto_and_wait(page, portal_url)
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
                _goto_and_wait(page, furl)
                page.wait_for_timeout(2000)
                new_links = gather_job_links(page)
                added = _add_links(new_links)
                if added == 0:
                    no_new_streak += 1
                else:
                    no_new_streak = 0
            except Exception as exc:
                print(f"[{company}] Constructed pagination failed: {exc}")
                no_new_streak += 1

    # --- Strategy 4: Click 'Load More' / 'Show More' buttons ---
    if len(job_links) < max_jobs and len(job_links) - count_before < 5:
        # Navigate back to the portal page
        try:
            _goto_and_wait(page, portal_url)
        except Exception:
            pass

        if _find_load_more_button(page):
            print(f"[{company}] Found 'Load More' button — clicking to load all jobs")
            job_links = _click_load_more_for_jobs(
                page, company, job_links, max_jobs,
            )

    # --- Strategy 5: Scroll for infinite-scroll portals ---
    if len(job_links) < max_jobs and len(job_links) - count_before < 5:
        job_links = _scroll_for_more_jobs(
            page, company, job_links, max_jobs,
        )

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
            if len(job_links) >= MIN_CONFIDENT_JOBS:
                job_links = _follow_pagination(page, job_links, company, max_jobs)
                print(f"[{company}] Discovered {len(job_links)} job links from direct portal")
                break
            elif job_links:
                print(f"[{company}] Only {len(job_links)} link(s) from portal — trying others")
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
            if len(job_links) >= MIN_CONFIDENT_JOBS:
                job_links = _follow_pagination(page, job_links, company, max_jobs)
                print(f"[{company}] Discovered {len(job_links)} job links on careers page")
                break
            elif job_links:
                print(f"[{company}] Only {len(job_links)} link(s) on careers page — looking for portal")

            # Look for portal links on the careers page
            portal_candidates = find_portal_links(page)
            if portal_candidates:
                print(f"[{company}] Found {len(portal_candidates)} portal candidate(s) on careers page")

            for portal_url in portal_candidates[:2]:
                print(f"[{company}] Trying portal: {portal_url}")
                try:
                    _goto_and_wait(page, portal_url)
                    job_links = _try_gather_jobs(page, company)
                    if len(job_links) >= MIN_CONFIDENT_JOBS:
                        job_links = _follow_pagination(page, job_links, company, max_jobs)
                        print(f"[{company}] Discovered {len(job_links)} job links from portal")
                        break
                    elif job_links:
                        print(f"[{company}] Only {len(job_links)} link(s) from portal — trying others")
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
    # Step 4 – Try guessing the careers subdomain
    # ------------------------------------------------------------------
    if len(job_links) < MIN_CONFIDENT_JOBS:
        guessed_url = _guess_careers_subdomain(source.url)
        if guessed_url:
            print(f"[{company}] Trying guessed careers subdomain: {guessed_url}")
            try:
                _goto_and_wait(page, guessed_url)
                guessed_jobs = _try_gather_jobs(page, company)
                if guessed_jobs:
                    # Also look for a portal on the guessed subdomain
                    portal_on_sub = find_portal_links(page)
                    if portal_on_sub:
                        print(f"[{company}] Found {len(portal_on_sub)} portal(s) on careers subdomain")
                        best_jobs = guessed_jobs
                        best_url = guessed_url
                        for p_url in portal_on_sub[:3]:
                            try:
                                _goto_and_wait(page, p_url)
                                sub_jobs = _try_gather_jobs(page, company)
                                if len(sub_jobs) > len(best_jobs):
                                    best_jobs = sub_jobs
                                    best_url = page.url
                            except Exception:
                                pass
                        
                        guessed_jobs = best_jobs
                        if best_url != page.url:
                            try:
                                _goto_and_wait(page, best_url)
                            except Exception:
                                pass
                    guessed_jobs = _follow_pagination(page, guessed_jobs, company, max_jobs)
                    if len(guessed_jobs) > len(job_links):
                        job_links = guessed_jobs
                        print(f"[{company}] Discovered {len(job_links)} job links from careers subdomain")
            except Exception as exc:
                print(f"[{company}] Careers subdomain not reachable: {exc}")

    # ------------------------------------------------------------------
    # Step 5 – Last resort: scroll + Load More on the best page found
    # ------------------------------------------------------------------
    if not job_links:
        # Navigate back to the best portal/careers page found
        best_page_url = (
            all_portal_candidates[0] if all_portal_candidates
            else careers_candidates[0] if careers_candidates
            else source.url
        )
        print(f"[{company}] Last resort: trying Load More + scroll on {best_page_url}")
        try:
            _goto_and_wait(page, best_page_url)
            # Try Load More first
            if _find_load_more_button(page):
                job_links = _click_load_more_for_jobs(
                    page, company, job_links, max_jobs,
                )
            # Then scroll for any remaining
            if not job_links:
                job_links = _try_scroll_for_jobs(page, company)
        except Exception as exc:
            print(f"[{company}] Last-resort failed: {exc}")

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
    parser.add_argument("--max-jobs", type=int, default=100, help="Maximum jobs to collect per company")
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

        for source in sources:
            page = browser.new_page()
            try:
                company_jobs = scrape_company(page, source, max_jobs=args.max_jobs)
                all_jobs.extend(company_jobs)
            except Exception as exc:
                print(f"[{source.company}] Failed: {exc}")
            finally:
                page.close()

        browser.close()

    save_json(args.out, {"jobs": all_jobs})
    if args.out_csv:
        save_csv(args.out_csv, all_jobs)

    print(f"Saved {len(all_jobs)} jobs to {args.out}")


if __name__ == "__main__":
    main()