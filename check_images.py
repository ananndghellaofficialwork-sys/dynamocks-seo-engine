"""Throwaway harness for the products.images column. Not part of the pipeline.

Reads back what fetch.py stored and prints it in columns, because a 9-image JSON
blob on one terminal line cannot be reviewed by a human, and an artifact nobody
can read is an artifact nobody has verified.

Usage:
    python3 check_images.py            health summary + only the rows worth looking at
    python3 check_images.py all        every product, one line each
    python3 check_images.py TRIOS      per-image detail for products matching TRIOS
"""
import sys
import json
import collections

from db import connect

import fetch   # for _MAX_MEDIA, so the cap check can never drift from the fetcher

ARG = sys.argv[1] if len(sys.argv) > 1 else None

conn = connect()
rows = conn.execute("SELECT gid, title, images FROM products ORDER BY title").fetchall()


def parse(row):
    """Image list for one row; empty list when the column is NULL."""
    return json.loads(row["images"]) if row["images"] else []


def flag_for(images):
    """
    The one thing about this row a human should look at, or "" for nothing.

    Only three conditions qualify. A flag on every row is the same as no flags
    at all, so anything merely unusual stays quiet.
    """
    if not images:
        return "NO IMAGES"
    if all(not i["alt_text"].strip() for i in images):
        return "no alt text at all"
    if len(images) == 1:
        return "single image, thin grounding"
    return ""


# ── DETAIL MODE ───────────────────────────────────────────────────────────────
# One line per image. This is the view for answering "is this the right sock",
# which needs alt text and filename side by side, not a count.
if ARG and ARG != "all":
    hits = [r for r in rows if ARG.lower() in r["title"].lower()]
    if not hits:
        print(f"no product title contains {ARG!r}")
        sys.exit(1)

    for row in hits:
        images = parse(row)
        print(f"\n{row['title']}")
        print(f"{row['gid']}   {len(images)} images")
        print(f"{'POS':>3}  {'ALT TEXT':<44}  FILE")
        print("-" * 104)
        for image in images:
            alt = image["alt_text"].strip()
            if len(alt) > 44:
                alt = alt[:42] + ".."
            elif not alt:
                # A blank alt text is the signal worth seeing, so it gets a word
                # rather than a gap that reads like a formatting fault.
                alt = "-- BLANK --"
            filename = image["url"].split("/")[-1].split("?")[0]
            print(f"{image['position']:>3}  {alt:<44}  {filename[:52]}")
    sys.exit(0)


# ── SUMMARY ───────────────────────────────────────────────────────────────────
image_total = 0
blank_total = 0
histogram = collections.Counter()
flagged = []

for row in rows:
    images = parse(row)
    image_total += len(images)
    blank_total += sum(1 for i in images if not i["alt_text"].strip())
    histogram[len(images)] += 1

    flag = flag_for(images)
    if flag:
        flagged.append((len(images), row["title"], flag))

print(f"products          : {len(rows)}")
print(f"images stored     : {image_total}")
print(f"blank alt text    : {blank_total} of {image_total} "
      f"({100 * blank_total / image_total:.1f}%)  <-- the alt-text backlog")

print("\nIMAGES PER PRODUCT")
for count in sorted(histogram):
    bar = "#" * min(histogram[count], 60)
    print(f"{count:>4} images  {histogram[count]:>4} products  {bar}")

# The cap is the bug that shipped on 2026-08-20 and cost 32 photos. fetch.py now
# warns while fetching; this is the second check, read back off the stored rows
# rather than trusted from the run's own output. Verify against the artifact.
at_cap = histogram.get(fetch._MAX_MEDIA, 0)
print()
if at_cap:
    print(f"!! {at_cap} products sit exactly on _MAX_MEDIA={fetch._MAX_MEDIA} — "
          f"some may be truncated. Re-run fetch.py and read the warnings.")
else:
    print(f"no product reaches _MAX_MEDIA={fetch._MAX_MEDIA} — nothing truncated")

# ── ROWS ──────────────────────────────────────────────────────────────────────
if ARG == "all":
    print(f"\nALL PRODUCTS\n{'IMGS':>4}  {'BLANK':>5}  TITLE")
    print("-" * 96)
    for row in rows:
        images = parse(row)
        blanks = sum(1 for i in images if not i["alt_text"].strip())
        flag = flag_for(images)
        print(f"{len(images):>4}  {blanks:>5}  {row['title'][:62]}"
              + (f"   <-- {flag}" if flag else ""))
else:
    print(f"\nNEEDS A LOOK ({len(flagged)} of {len(rows)})\n{'IMGS':>4}  TITLE")
    print("-" * 96)
    for count, title, flag in sorted(flagged):
        print(f"{count:>4}  {title[:62]:<62}  {flag}")
    print("\nrun with 'all' to list every product")

conn.close()
