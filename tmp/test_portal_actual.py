import sys
from playwright.sync_api import sync_playwright
import company_job_scraper

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto('https://careers.ppg.com', timeout=30000, wait_until='domcontentloaded')
    page.wait_for_timeout(5000)
    links = company_job_scraper.find_portal_links(page)
    print(f"Found {len(links)} links. Top 5:")
    for l in links[:5]: print(" ", l)
    browser.close()
