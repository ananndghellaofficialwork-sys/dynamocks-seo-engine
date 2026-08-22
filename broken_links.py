"""
Finds dead links on the live dynamocks.us storefront, for manual cleanup.

Read-only against the live site and against data/seo.db. Writes nothing to
either. It is not part of the SEO pipeline proper -- it does not touch
products, proposals or pushes -- and it earns no write access as a result.

It answers one question: "which URLs does this store publish, or link to,
that no longer resolve?" A URL can die two ways this store cares about:
  - the sitemap still lists a product/collection page that 404s, because the
    row was deleted rather than unpublished
  - a live page's own on-page links (nav, footer, body copy) point at a page
    that no longer exists

The output is a CSV of broken URLs with where each was found, cross-checked
against the local `products`/`collections` mirror so a broken product URL
can be traced to a handle you can go delete in Shopify admin by hand. Deletion
itself stays manual and outside this script, deliberately -- a link-checker
is the wrong tool to also be a deleter, for the same reason push.py is the
only module allowed to write to the store.
"""
import csv
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

import db

BASE = "https://dynamocks.us"
REPORT = Path("broken_links.csv")
_TIMEOUT = 15
_PAUSE = 0.3  # seconds between requests -- polite to your own storefront
_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "Mozilla/5.0 (dynamocks broken-link audit)"})


def main():
    """
    WHAT IT DOES:
      Runs the whole audit end to end: pulls every URL out of the sitemap,
      pulls every on-page link out of the handful of pages a shopper actually
      navigates through (home, each collection, a sample of product pages),
      checks each unique URL once, cross-references failures against the
      local database, and writes one CSV.

      Called by: the operator, from the terminal, on demand. Not scheduled,
                 not called by anything else in the pipeline.

      In the pipeline: this script stands alone. It reads data/seo.db (filled
                        by fetch.py) for the cross-reference step only; it does
                        not write back to it.

    WHY IT IS ITS OWN FUNCTION:
      The alternative was no main() at all -- a flat script. Every other
      module in this pipeline is imported by something else (regenerate.py
      imports generate.py, verify.py imports db.py); this one genuinely is
      not, so a bare top-level script would be consistent with that. It gets
      a main() anyway so the run order (collect -> check -> cross-reference
      -> write) reads as one paragraph instead of scattered module-level
      statements, matching the shape of every other script in this repo.

    WHAT IT RETURNS, AND WHO CONSUMES IT:
      None. Prints a running count to the terminal and leaves broken_links.csv
      on disk. The CSV is for a person to read in Excel and go delete listings
      by hand in Shopify admin -- nothing downstream in this codebase reads it.
    """
    conn = db.connect()

    print(f"reading sitemap: {BASE}/sitemap.xml")
    sitemap_urls = collect_sitemap_urls(f"{BASE}/sitemap.xml")
    print(f"  {len(sitemap_urls)} URLs in sitemap")

    print("reading on-page links (home + collections)")
    page_links = collect_page_links(sitemap_urls)
    print(f"  {len(page_links)} additional on-page links found")

    all_urls = dict(sitemap_urls)
    for url, source in page_links.items():
        all_urls.setdefault(url, source)

    print(f"checking {len(all_urls)} unique URLs — this takes a while, one at a time")
    broken = []
    for i, (url, source) in enumerate(sorted(all_urls.items()), start=1):
        status = check_url(url)
        if status is None or status >= 400:
            note = cross_reference(conn, url)
            broken.append({"url": url, "status": status or "no response", "found_via": source, "note": note})
            print(f"  [{i}/{len(all_urls)}] {status or 'ERR'}  {url}")
        time.sleep(_PAUSE)

    write_report(broken)
    print("-" * 92)
    print(f"done — {len(broken)} broken URLs out of {len(all_urls)} checked")
    print(f"report written: {REPORT.resolve()}")


def collect_sitemap_urls(sitemap_url, seen=None):
    """
    WHAT IT DOES:
      Downloads one sitemap XML file. If it is a sitemap INDEX (a list of
      other sitemaps, which is what Shopify serves at /sitemap.xml), it
      recurses into each child sitemap. If it is a URL SET, it returns the
      page URLs themselves, tagged with the sitemap file they came from.

      Called by: main(), once, at the very start of a run.

      In the pipeline: BASE/sitemap.xml (live, on Shopify's CDN)
                         -> collect_sitemap_urls() [recursive]
                         -> main()'s all_urls dict
                         -> check_url() for each one

    WHY IT IS ITS OWN FUNCTION:
      The rejected alternative was assuming /sitemap.xml lists pages directly.
      Shopify never does this -- it always serves an index pointing at
      sitemap_products_1.xml, sitemap_collections_1.xml, sitemap_pages_1.xml
      and so on, sharded by type and page number. A flat parse would silently
      collect zero URLs and the script would report a false "0 broken links."
      Recursing is the only way to reach the actual page list.

    WHAT IT RETURNS, AND WHO CONSUMES IT:
      A dict of {url: source_label}, e.g. {"https://dynamocks.us/products/x":
      "sitemap_products_1.xml"}. main() merges this with on-page links and
      hands the combined dict to check_url() for every entry.
    """
    if seen is None:
        seen = {}

    try:
        resp = _SESSION.get(sitemap_url, timeout=_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as error:
        print(f"  ! could not read {sitemap_url}: {error}")
        return seen

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as error:
        print(f"  ! could not parse {sitemap_url}: {error}")
        return seen

    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    child_sitemaps = root.findall("sm:sitemap/sm:loc", ns)
    page_urls = root.findall("sm:url/sm:loc", ns)

    label = sitemap_url.rsplit("/", 1)[-1]

    if child_sitemaps:
        for node in child_sitemaps:
            if node.text and node.text not in seen:
                collect_sitemap_urls(node.text, seen)
    else:
        for node in page_urls:
            if node.text:
                seen[node.text] = label

    return seen


def collect_page_links(sitemap_urls):
    """
    WHAT IT DOES:
      Fetches the homepage and every collection page already found in the
      sitemap, and pulls every internal <a href> out of each one's HTML. This
      catches the failure the sitemap cannot: a nav menu, footer, or a
      product's body copy linking to a page that was deleted and so was
      NEVER in the sitemap to begin with, or was removed from it.

      Called by: main(), once, after the sitemap is collected.

      In the pipeline: sitemap_urls (from collect_sitemap_urls) -> this
                        function fetches the collection pages within it ->
                        raw HTML -> regex link extraction -> a dict merged
                        into main()'s all_urls before checking.

    WHY IT IS ITS OWN FUNCTION:
      The rejected alternative was trusting the sitemap alone. A sitemap only
      ever lists pages Shopify still knows about; it cannot list a stale link
      sitting inside a product's descriptionHtml pointing at a product that
      was deleted six months ago. That is precisely the kind of broken link a
      shopper actually clicks and a sitemap audit alone would never surface.
      Splitting this into its own pass keeps the two evidence sources (what
      Shopify says exists vs. what pages actually link to) visibly separate
      in the output rather than blurred into one list.

    WHAT IT RETURNS, AND WHO CONSUMES IT:
      A dict of {url: source_label} for internal links not already covered by
      the sitemap. Merged into main()'s all_urls with sitemap entries taking
      precedence (setdefault), then handed to check_url() the same way.
    """
    found = {}
    to_scan = [BASE + "/"] + [u for u, src in sitemap_urls.items() if "/collections/" in u]

    for page_url in to_scan:
        try:
            resp = _SESSION.get(page_url, timeout=_TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException:
            continue  # this page's own brokenness is caught by check_url() separately

        for href in re.findall(r'href="([^"]+)"', resp.text):
            absolute = urljoin(BASE, href)
            if urlparse(absolute).netloc != urlparse(BASE).netloc:
                continue  # external links are not this store's cleanup job
            absolute = absolute.split("#")[0]
            if absolute not in found:
                found[absolute] = f"linked from {page_url.replace(BASE, '')}"

        time.sleep(_PAUSE)

    return found


def check_url(url):
    """
    WHAT IT DOES:
      One HTTP request. Tries HEAD first since it is cheaper for the server;
      falls back to GET because some Shopify pages reject HEAD with a 405
      even though the page itself is fine.

      Called by: main(), once per unique URL -- so once per product page,
                 once per collection page, once per static page. The single
                 largest source of request volume in this script.

    WHY IT IS ITS OWN FUNCTION:
      The rejected alternative was inlining the request in main()'s loop.
      The HEAD-then-GET fallback and the try/except around network failures
      are the part most likely to need tuning after seeing real results (a
      timeout that is too short, a status code Shopify uses unexpectedly) --
      isolating it means that tuning happens here, not inside the loop that
      also does cross-referencing and reporting.

    WHAT IT RETURNS, AND WHO CONSUMES IT:
      An int status code, or None if the request failed outright (DNS,
      timeout, connection refused). main() treats both None and >=400 as
      broken and passes the url to cross_reference().
    """
    try:
        resp = _SESSION.head(url, timeout=_TIMEOUT, allow_redirects=True)
        if resp.status_code == 405:  # method not allowed -- try GET instead
            resp = _SESSION.get(url, timeout=_TIMEOUT)
        return resp.status_code
    except requests.RequestException:
        return None


def cross_reference(conn, url):
    """
    WHAT IT DOES:
      For a broken /products/<handle> or /collections/<handle> URL, looks up
      the handle in the local mirror and says whether it is marked delisted,
      or was never seen at all (deleted before fetch.py ever mirrored it).

      Called by: main(), once per broken URL found -- the last step before
                 that URL is written to the report.

    WHY IT IS ITS OWN FUNCTION:
      The rejected alternative was a bare URL list with no context, which
      would leave the person doing cleanup to manually search Shopify admin
      for every dead handle. One query against a table this pipeline already
      maintains turns "here is a 404" into "here is a 404, and it is the
      product your wife delisted three weeks ago" -- the fact that makes the
      cleanup decision obvious instead of a lookup.

    WHAT IT RETURNS, AND WHO CONSUMES IT:
      A short human-readable string, e.g. "delisted 2026-08-01" or "not in
      local mirror" or "". Written straight into the CSV's note column by
      write_report() -- no further processing.
    """
    match = re.search(r"/(products|collections)/([^/?#]+)", url)
    if not match:
        return ""

    kind, handle = match.group(1), match.group(2)
    table = "products" if kind == "products" else "collections"

    row = conn.execute(
        f"SELECT delisted_at FROM {table} WHERE handle = ?", (handle,)
    ).fetchone()

    if row is None:
        return "not in local mirror — deleted before fetch.py last ran, or never a product/collection page"
    if row["delisted_at"]:
        return f"delisted {row['delisted_at']}"
    return "in local mirror as live — Shopify may just be slow, worth a manual re-check"


def write_report(rows):
    """
    WHAT IT DOES:
      Writes broken_links.csv, one row per dead URL, sorted so product/
      collection pages you can actually go delete come before generic 404s.

      Called by: main(), once, at the end of a run.

    WHY IT IS ITS OWN FUNCTION:
      The rejected alternative was writing rows inline as they're found. That
      would mean a crashed run partway through leaves a half-written,
      unsorted CSV. Collecting everything in memory first and writing once at
      the end means the file on disk is always either absent or complete.

    WHAT IT RETURNS, AND WHO CONSUMES IT:
      None. The CSV is the terminal output of this script -- opened by the
      operator in Excel or Numbers, not read by any other file here.
    """
    if not rows:
        return

    rows.sort(key=lambda r: (r["note"] == "", r["url"]))

    with open(REPORT, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["url", "status", "found_via", "note"])
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    main()
