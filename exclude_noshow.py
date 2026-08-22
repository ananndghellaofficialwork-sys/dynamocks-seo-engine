"""One-off: mark the discontinued No Show / Invisibles line as out of scope for SEO.

Run once. Safe to re-run — exclude_from_seo upserts, so a second run rewrites
the same rows rather than failing or duplicating them.

Decision, 2026-08-20: the Invisibles line is being discontinued, so generating
SEO copy for it spends paid API calls on listings that will not exist.

Why product_type and not a title match: 'No Show' is exactly 81 products, and
that set is identical to a case-insensitive title/handle search for
"invisible|no-show" — verified on the 455-product catalog, 81 and 81. A
structured column does not break when somebody renames a listing.

Usage:
    python3 exclude_noshow.py          show what would be excluded, change nothing
    python3 exclude_noshow.py --write  write the exclusion rows
"""
import sys

import db

REASON = "line discontinued 2026-08-20 — Invisibles / No Show"
PRODUCT_TYPE = "No Show"

WRITE = "--write" in sys.argv

conn = db.connect()
db.init_schema(conn)   # creates seo_exclusions on an existing database

targets = conn.execute(
    """
    SELECT gid, title, seo_title
    FROM products
    WHERE product_type = :product_type
    ORDER BY title
    """,
    {"product_type": PRODUCT_TYPE},
).fetchall()

already = conn.execute("SELECT COUNT(*) FROM seo_exclusions").fetchone()[0]

print(f"product_type = {PRODUCT_TYPE!r}: {len(targets)} products")
print(f"already excluded        : {already}")
print(f"reason                  : {REASON}")
print("-" * 92)

for row in targets:
    # Flag the ones that already carry an seo_title. Excluding them is still
    # correct — no NEW copy gets written — but it is worth seeing that some
    # already-optimised listings are in the discontinued set, because that is
    # effort already spent on a line being retired.
    mark = "  (has seo_title)" if row["seo_title"] else ""
    print(f"  {row['title'][:72]}{mark}")

print("-" * 92)

if not WRITE:
    print(f"DRY RUN — nothing written. Re-run with --write to exclude these {len(targets)}.")
    conn.close()
    sys.exit(0)

for row in targets:
    db.exclude_from_seo(conn, row["gid"], REASON)
conn.commit()

# Verify against the artifact, not the fact that the loop finished.
total = conn.execute("SELECT COUNT(*) FROM seo_exclusions").fetchone()[0]
queue = conn.execute(
    """
    SELECT COUNT(*)
    FROM products
    WHERE (seo_title IS NULL OR seo_title = '')
      AND NOT EXISTS
      (
          SELECT 1
          FROM seo_exclusions
          WHERE seo_exclusions.gid = products.gid
      )
      AND NOT EXISTS
      (
          SELECT 1
          FROM proposals
          WHERE proposals.gid = products.gid
      )
    """
).fetchone()[0]

leaked = conn.execute(
    """
    SELECT COUNT(*)
    FROM products
    WHERE product_type = :product_type
      AND NOT EXISTS
      (
          SELECT 1
          FROM seo_exclusions
          WHERE seo_exclusions.gid = products.gid
      )
    """,
    {"product_type": PRODUCT_TYPE},
).fetchone()[0]

print(f"seo_exclusions rows now : {total}")
print(f"generator queue now     : {queue} products")
print(f"No Show still reachable : {leaked}" + ("  <-- BUG" if leaked else "  (correct)"))

conn.close()
