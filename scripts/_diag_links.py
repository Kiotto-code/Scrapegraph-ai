"""Diagnostic: dump all links from careers pages to understand what the scraper sees."""
from playwright.sync_api import sync_playwright
import re

PAGES = [
    ("Petronas", "https://www.petronas.com/careers/career-opportunities"),
    ("PPG", "https://www.ppg.com/en-US/about-ppg/careers-at-ppg"),
]

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True)
    page = browser.new_page()

    for company, url in PAGES:
        print(f"\n{'='*70}")
        print(f"  {company}: {url}")
        print(f"{'='*70}")
        page.goto(url, timeout=30000, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            page.wait_for_timeout(3000)

        # Dump all anchors
        anchors = page.query_selector_all("a[href]")
        print(f"\n  Total anchors: {len(anchors)}")

        # Filter for potentially interesting links
        print(f"\n  --- Career/Job/Portal related links ---")
        keywords = ["career", "job", "position", "opening", "hiring", "apply",
                     "talent", "recruit", "vacanc", "requisition", "portal",
                     "search", "opportunity", "work"]
        for a in anchors:
            href = a.get_attribute("href") or ""
            text = (a.inner_text() or "").strip()[:80]
            href_lower = href.lower()
            text_lower = text.lower()
            if any(k in href_lower or k in text_lower for k in keywords):
                print(f"  href={href}")
                print(f"    text='{text}'")
                print()

        # Check iframes
        iframes = page.query_selector_all("iframe[src]")
        if iframes:
            print(f"\n  --- Iframes ({len(iframes)}) ---")
            for iframe in iframes:
                print(f"  src={iframe.get_attribute('src')}")
        
        # Check buttons with onclick
        buttons = page.query_selector_all("button[onclick], a[onclick]")
        if buttons:
            print(f"\n  --- Buttons with onclick ({len(buttons)}) ---")
            for btn in buttons:
                onclick = btn.get_attribute("onclick") or ""
                text = (btn.inner_text() or "").strip()[:60]
                print(f"  onclick='{onclick}' text='{text}'")

    browser.close()
