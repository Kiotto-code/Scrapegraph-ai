import sys
from playwright.sync_api import sync_playwright
import re

CAREERS_PORTAL_HINTS = [
    "search-results", "job-search", "careers", "jobs", "vacancies",
    "open-positions", "openings", "positions", "opportunities", "hiring",
    "talent", "recruitment", "apply", "current-openings", "job-listing",
    "job-openings", "open-roles",
]
_NON_PORTAL_PATH_FRAGMENTS = [
    "why-", "/why/", "life-at", "lifeat", "/life/", "life@",
    "about-", "/about/", "people-", "/people/",
    "culture", "benefits", "diversity", "inclusion",
    "students", "graduates", "internship-program",
]

def find_portal_links(page):
    links = set()
    anchors = page.query_selector_all("a[href]")
    for anchor in anchors:
        href = anchor.get_attribute("href") or ""
        if not href: continue
        href_lower = href.lower()
        if "/hvhapply" in href_lower or "jobcart" in href_lower: continue
        
        # Skip if path has non-portal fragments
        from urllib.parse import urlparse
        parsed = urlparse(href_lower)
        if any(f in parsed.path for f in _NON_PORTAL_PATH_FRAGMENTS):
            continue
            
        text = (re.sub(r'\s+', ' ', anchor.inner_text()) or "").lower()
        
        if any(hint in href_lower for hint in CAREERS_PORTAL_HINTS) or \
           any(hint in text for hint in CAREERS_PORTAL_HINTS):
            links.add(href)
    return list(links)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto('https://careers.ppg.com', timeout=30000, wait_until='domcontentloaded')
    page.wait_for_timeout(5000)
    links = find_portal_links(page)
    print(f"Found {len(links)} links:")
    for l in links[:5]: print(" ", l)
    browser.close()
