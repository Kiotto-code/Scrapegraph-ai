import sys
from playwright.sync_api import sync_playwright
import re

JOB_PORTAL_HINTS = [
    "/job/", "/jobs/", "jobid=", "/position/", "/positions/",
    "/opening/", "/openings/", "/vacancy/", "/vacancies/",
    "/requisition/", "/requisitions/", "/posting/", "/postings/",
    "/role/", "/roles/", "req_id=", "requisitionid=", "job_id=", "positionid=",
    "/gh/", "greenhouse.io/", "lever.co/", "myworkdayjobs.com", "myworkday.com",
    "icims.com", "smartrecruiters.com", "jobvite.com", "ultipro.com", "successfactors", "taleo",
    "bamboohr.com", "ashbyhq.com", "recruitee.com", "applytojob.com", "workable.com", "breezy.hr",
    "job-detail", "job_detail", "jobdetail", "career-detail", "apply/",
]

def _clean_text(t): return re.sub(r'\s+', ' ', t).strip()

def gather_job_links(page):
    links = set()
    anchors = page.query_selector_all("a[href]")
    for anchor in anchors:
        href = anchor.get_attribute("href") or ""
        if not href: continue
        href_lower = href.lower()
        if "/hvhapply" in href_lower or "jobcart" in href_lower: continue
        
        matched = False
        if any(hint in href_lower for hint in JOB_PORTAL_HINTS):
            matched = True
            
        if not matched:
            text = (_clean_text(anchor.inner_text()) or "").lower()
            if len(text) > 10 and any(kw in text for kw in ["apply now", "view details", "learn more", "view job"]):
                if any(frag in href_lower for frag in ["job", "position", "career", "role", "opening", "requisition", "posting", "vacancy", "apply"]):
                    matched = True
        
        if matched:
            links.add(href)
    return sorted(links)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto('https://careers.ppg.com/us/en/search-results', timeout=30000, wait_until='domcontentloaded')
    page.wait_for_timeout(5000)
    links = gather_job_links(page)
    print(f"Found {len(links)} links:")
    for l in links: print(" ", l)
    browser.close()
