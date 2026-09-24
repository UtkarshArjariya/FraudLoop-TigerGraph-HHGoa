"""Exercise the running local console without live investigations or approvals.

Run: PLAYWRIGHT_BROWSERS_PATH=/tmp/fraudloop-playwright .venv/bin/python -m scripts.browser_smoke
Requires the optional Playwright package and Chromium; start make demo first.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re


def run(base_url: str, report: Path):
    from playwright.sync_api import sync_playwright, expect

    results = {"errors": [], "checks": []}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1600, "height": 1080})
            page.on("pageerror", lambda error: results["errors"].append(str(error)))
            page.on("console", lambda message: results["errors"].append(message.text) if message.type == "error" else None)
            page.goto(base_url)
            page.get_by_role("searchbox", name="Search cases").fill("HHG-")
            expect(page.locator("tbody tr")).to_have_count(20)
            page.get_by_role("combobox", name="Filter verdict").select_option("fraud")
            expect(page.locator("tbody tr")).to_have_count(9)
            page.get_by_role("button", name="Reset", exact=True).click()
            page.get_by_role("searchbox", name="Search cases").fill("HHG-014")
            expect(page.locator("tbody tr")).to_have_count(1)
            page.get_by_role("link", name="HHG-014", exact=True).click()
            expect(page.get_by_role("heading", name="HHG-014", exact=True)).to_be_visible()
            page.get_by_role("button", name="Replay", exact=True).click()
            expect(page.get_by_role("button", name="Replaying…", exact=True)).to_be_disabled()
            expect(page.locator(".timeline-done")).to_be_visible(timeout=30000)
            expect(page.get_by_role("button", name="Replay", exact=True)).to_be_enabled()
            results["checks"].append("Queue filters/search and HHG-014 SSE replay complete")

            page.goto(base_url + "/cases/HHG-002")
            page.get_by_role("button", name="Replay", exact=True).click()
            expect(page.locator(".timeline-request.received")).to_be_visible(timeout=30000)
            expect(page.locator(".timeline-done")).to_be_visible(timeout=30000)
            expect(page.locator(".probability-value strong")).to_have_text("4.0%")
            results["checks"].append("HHG-002 simulated verification replay settles at 4%")
            page.get_by_role("tab", name="Precedents", exact=True).click()
            expect(page.locator(".precedent-card")).to_have_count(5)
            page.get_by_role("tab", name="Raw JSON", exact=True).click()
            expect(page.locator(".raw-json")).to_contain_text("HHG-002")
            results["checks"].append("Five precedents and raw JSON load")

            page.goto(base_url + "/approvals")
            approve = page.get_by_role("button", name=re.compile(r"^Approve "))
            expect(approve.first).to_be_disabled()
            page.get_by_role("combobox", name="Demo role", exact=True).select_option("analyst_l1")
            expect(approve.first).to_be_enabled()
            l2 = page.get_by_role("button", name="Approve File report for HHG-014", exact=True)
            expect(l2).to_be_disabled()
            page.get_by_role("combobox", name="Demo role", exact=True).select_option("supervisor_l2")
            expect(l2).to_be_enabled()
            assert all(button.is_enabled() for button in approve.all())
            results["checks"].append("Viewer/L1/L2 approval controls enforce route permissions")
            page.goto(base_url + "/memory")
            expect(page.get_by_text("BAAI/bge-base-en-v1.5", exact=True)).to_be_visible(timeout=30000)
            results["checks"].append("Memory coverage loads")

            for width in (375, 768, 1280, 1600):
                page.set_viewport_size({"width": width, "height": 1000})
                for path in ("/", "/cases/HHG-014", "/approvals", "/memory"):
                    page.goto(base_url + path)
                    expect(page.locator("h1")).to_be_visible()
                    page.locator(".skeleton").first.wait_for(state="hidden", timeout=30000)
                    size = page.evaluate("({width: innerWidth, scroll: document.documentElement.scrollWidth})")
                    assert size["scroll"] <= width + 1, (path, width, size)
                results["checks"].append(f"No horizontal page overflow at {width}px")
            page.set_viewport_size({"width": 1600, "height": 1080})
            page.goto(base_url)
            page.get_by_role("button", name="Switch to light theme", exact=True).click()
            assert page.locator("html").get_attribute("data-theme") == "light"
            page.get_by_role("button", name="New investigation", exact=True).click()
            expect(page.get_by_role("dialog")).to_be_visible()
            page.keyboard.press("Escape")
            expect(page.get_by_role("dialog")).not_to_be_visible()
            expect(page.get_by_role("button", name="New investigation", exact=True)).to_be_focused()
            results["checks"].append("Light theme and keyboard dialog focus restoration")
        finally:
            browser.close()
    assert not results["errors"], results
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:5173")
    parser.add_argument("--report", type=Path, default=Path("data/runs/browser_smoke.json"))
    args = parser.parse_args()
    run(args.base_url.rstrip("/"), args.report)
