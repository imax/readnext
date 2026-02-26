#!/usr/bin/env python3
"""
ReadNext crawler — checks sources for new content since a cutoff date.
Uses RSS feeds when available, falls back to headless screenshots.

Usage:
    python crawl.py --cutoff 2025-01-01
    python crawl.py  # defaults to 30 days ago
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse

import feedparser
import requests
from bs4 import BeautifulSoup

DATA_DIR = Path(__file__).parent / "data"
SCREENSHOTS_DIR = DATA_DIR / "screenshots"
STATE_FILE = DATA_DIR / "crawl_state.json"

SKIP_DOMAINS = {"nitter.net"}

WELL_KNOWN_FEED_PATHS = ["/feed", "/rss", "/rss.xml", "/atom.xml", "/feed.xml", "/index.xml"]

# Platform-specific feed URL patterns
PLATFORM_FEED_PATTERNS = {
    "medium.com": lambda url: url.rstrip("/") + "/feed",
    "substack.com": lambda url: url.rstrip("/") + "/feed",
}

HEADERS = {
    "User-Agent": "ReadNext Crawler/1.0 (https://readnext.exe.xyz)"
}

REQUEST_TIMEOUT = 15


def parse_links_file(path: str) -> list[dict]:
    """Parse links.txt into a list of {name, urls}."""
    entries = []
    current = None

    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                if current and current["urls"]:
                    entries.append(current)
                current = None
                continue
            if line.startswith("http://") or line.startswith("https://"):
                if current is None:
                    current = {"name": "", "urls": []}
                current["urls"].append(line)
            else:
                current = {"name": line, "urls": []}

    if current and current["urls"]:
        entries.append(current)

    return entries


def domain_of(url: str) -> str:
    return urlparse(url).netloc.removeprefix("www.")


def should_skip(url: str) -> bool:
    return domain_of(url) in SKIP_DOMAINS


def discover_feed_from_html(url: str) -> str | None:
    """Look for <link rel='alternate'> feed tags in page HTML."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  [!] Failed to fetch {url}: {e}")
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    for link in soup.find_all("link", rel="alternate"):
        link_type = (link.get("type") or "").lower()
        if "rss" in link_type or "atom" in link_type:
            href = link.get("href", "")
            if href and not href.startswith("http"):
                parsed = urlparse(url)
                if href.startswith("/"):
                    href = f"{parsed.scheme}://{parsed.netloc}{href}"
                else:
                    href = f"{parsed.scheme}://{parsed.netloc}/{href}"
            if href:
                return href
    return None


def discover_feed_well_known(url: str) -> str | None:
    """Try well-known feed paths."""
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"

    for path in WELL_KNOWN_FEED_PATHS:
        feed_url = base + path
        try:
            resp = requests.head(feed_url, headers=HEADERS, timeout=REQUEST_TIMEOUT, allow_redirects=True)
            content_type = resp.headers.get("content-type", "").lower()
            if resp.status_code == 200 and ("xml" in content_type or "rss" in content_type or "atom" in content_type):
                return feed_url
        except requests.RequestException:
            continue

    return None


def discover_feed_platform(url: str) -> str | None:
    """Try platform-specific feed patterns (Medium, Substack, etc.)."""
    domain = domain_of(url)
    for platform_domain, make_feed_url in PLATFORM_FEED_PATTERNS.items():
        if platform_domain in domain:
            feed_url = make_feed_url(url)
            try:
                resp = requests.head(feed_url, headers=HEADERS, timeout=REQUEST_TIMEOUT, allow_redirects=True)
                if resp.status_code == 200:
                    return feed_url
            except requests.RequestException:
                pass
    return None


def discover_feed(url: str) -> str | None:
    """Try all feed discovery strategies."""
    feed_url = discover_feed_from_html(url)
    if feed_url:
        return feed_url

    feed_url = discover_feed_platform(url)
    if feed_url:
        return feed_url

    feed_url = discover_feed_well_known(url)
    if feed_url:
        return feed_url

    return None


def parse_feed_date(entry) -> datetime | None:
    """Extract a datetime from a feed entry."""
    for attr in ("published_parsed", "updated_parsed"):
        t = getattr(entry, attr, None)
        if t:
            try:
                from time import mktime
                return datetime.fromtimestamp(mktime(t), tz=timezone.utc)
            except (ValueError, OverflowError):
                pass
    return None


def fetch_feed_entries(feed_url: str, cutoff: datetime) -> list[dict]:
    """Parse an RSS/Atom feed and return entries newer than cutoff."""
    feed = feedparser.parse(feed_url, agent=HEADERS["User-Agent"])
    if feed.bozo and not feed.entries:
        return []

    entries = []
    for entry in feed.entries:
        entry_date = parse_feed_date(entry)
        if entry_date and entry_date < cutoff:
            continue

        entries.append({
            "title": entry.get("title", "Untitled"),
            "url": entry.get("link", ""),
            "date": entry_date.isoformat() if entry_date else None,
            "summary": _clean_summary(entry.get("summary", "")),
        })

    return entries


def _clean_summary(text: str) -> str:
    """Strip HTML tags and truncate summary."""
    clean = BeautifulSoup(text, "html.parser").get_text(separator=" ").strip()
    clean = re.sub(r"\s+", " ", clean)
    if len(clean) > 300:
        clean = clean[:300] + "..."
    return clean


def take_screenshot(url: str) -> str | None:
    """Take a headless screenshot of a URL. Returns the file path or None."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  [!] Playwright not installed, skipping screenshot")
        return None

    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    domain = domain_of(url)
    date_str = datetime.now().strftime("%Y-%m-%d")
    filename = f"{domain}_{date_str}.png"
    filepath = SCREENSHOTS_DIR / filename

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.goto(url, wait_until="networkidle", timeout=30000)
            page.screenshot(path=str(filepath), full_page=False)
            browser.close()
        print(f"  [+] Screenshot saved: {filepath}")
        return str(filepath.relative_to(Path(__file__).parent))
    except Exception as e:
        print(f"  [!] Screenshot failed for {url}: {e}")
        return None


def crawl_source(name: str, urls: list[str], cutoff: datetime) -> dict:
    """Crawl a single source (may have multiple URLs)."""
    result = {
        "name": name,
        "urls": urls,
        "method": None,
        "feed_url": None,
        "new_entries": [],
        "screenshots": [],
    }

    for url in urls:
        if should_skip(url):
            print(f"  [-] Skipping {url} (blocked domain)")
            continue

        print(f"  Trying RSS for {url}...")
        feed_url = discover_feed(url)

        if feed_url:
            print(f"  [+] Found feed: {feed_url}")
            entries = fetch_feed_entries(feed_url, cutoff)
            if entries:
                result["method"] = "rss"
                result["feed_url"] = feed_url
                result["new_entries"].extend(entries)
                print(f"  [+] {len(entries)} new entries since {cutoff.date()}")
                continue
            else:
                print(f"  [~] Feed found but no entries after cutoff")

        # Fallback to screenshot
        print(f"  [~] No usable feed, taking screenshot...")
        screenshot_path = take_screenshot(url)
        if screenshot_path:
            result["screenshots"].append(screenshot_path)
            if not result["method"]:
                result["method"] = "screenshot"

    if not result["method"]:
        result["method"] = "failed"

    return result


def main():
    parser = argparse.ArgumentParser(description="ReadNext crawler")
    parser.add_argument(
        "--cutoff",
        type=str,
        default=None,
        help="Cutoff date in YYYY-MM-DD format (default: 30 days ago)",
    )
    parser.add_argument(
        "--no-screenshots",
        action="store_true",
        help="Skip screenshots (RSS only mode)",
    )
    args = parser.parse_args()

    if args.cutoff:
        cutoff = datetime.strptime(args.cutoff, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    else:
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)

    print(f"ReadNext Crawler")
    print(f"Cutoff date: {cutoff.date()}")
    print(f"---")

    links_file = Path(__file__).parent / "links.txt"
    sources = parse_links_file(str(links_file))
    print(f"Found {len(sources)} sources in links.txt\n")

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    results = []
    for source in sources:
        name = source["name"]
        urls = source["urls"]
        print(f"[{name}]")

        if args.no_screenshots:
            # RSS-only mode: skip screenshot fallback
            result = {
                "name": name,
                "urls": urls,
                "method": None,
                "feed_url": None,
                "new_entries": [],
                "screenshots": [],
            }
            for url in urls:
                if should_skip(url):
                    print(f"  [-] Skipping {url}")
                    continue
                print(f"  Trying RSS for {url}...")
                feed_url = discover_feed(url)
                if feed_url:
                    print(f"  [+] Found feed: {feed_url}")
                    entries = fetch_feed_entries(feed_url, cutoff)
                    if entries:
                        result["method"] = "rss"
                        result["feed_url"] = feed_url
                        result["new_entries"].extend(entries)
                        print(f"  [+] {len(entries)} new entries since {cutoff.date()}")
                    else:
                        print(f"  [~] Feed found but no entries after cutoff")
                else:
                    print(f"  [~] No feed found")
            if not result["method"]:
                result["method"] = "no_feed"
            results.append(result)
        else:
            results.append(crawl_source(name, urls, cutoff))
        print()

    state = {
        "cutoff_date": str(cutoff.date()),
        "crawled_at": datetime.now(timezone.utc).isoformat(),
        "sources": results,
    }

    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

    print(f"---")
    print(f"Results written to {STATE_FILE}")

    # Summary
    rss_count = sum(1 for r in results if r["method"] == "rss")
    screenshot_count = sum(1 for r in results if r["method"] == "screenshot")
    failed_count = sum(1 for r in results if r["method"] in ("failed", "no_feed"))
    total_entries = sum(len(r["new_entries"]) for r in results)

    print(f"RSS: {rss_count} sources | Screenshots: {screenshot_count} | Failed: {failed_count}")
    print(f"Total new entries found: {total_entries}")


if __name__ == "__main__":
    main()
