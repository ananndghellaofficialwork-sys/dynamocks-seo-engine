"""What is done, what is pending, across every SEO field. Read-only.

Not the pipeline. Runs no model, spends nothing, writes nothing.

Answers one question — "where does the store actually stand" — from the
database rather than from memory of which scripts were run. Every number here
is read back off the artifact, which is the only kind of number worth acting
on.

Where a field is NOT mirrored locally, this says so instead of leaving it out.
A silent omission reads as zero work remaining, which is the opposite of true.

Usage:
    python3 status.py
"""
import json

import db

conn = db.connect()


def count(sql, **params):
    return conn.execute(sql, params).fetchone()[0]


def rule(title):
    print(f"\n{title}")
    print("-" * 74)


LIVE = "delisted_at IS NULL"
EXCLUDED = "EXISTS (SELECT 1 FROM seo_exclusions e WHERE e.gid = products.gid)"
IN_SCOPE = f"{LIVE} AND NOT {EXCLUDED}"

print("=" * 74)
print("DYNAMOCKS SEO — WHERE THE STORE STANDS")
print("=" * 74)

# ── CATALOG ──────────────────────────────────────────────────────────────────
rule("CATALOG")
total = count("SELECT COUNT(*) FROM products")
live = count(f"SELECT COUNT(*) FROM products WHERE {LIVE}")
delisted = total - live
excluded = count(f"SELECT COUNT(*) FROM products WHERE {LIVE} AND {EXCLUDED}")
scope = count(f"SELECT COUNT(*) FROM products WHERE {IN_SCOPE}")

print(f"  {total:>4}  rows in the local mirror")
print(f"  {live:>4}  live in the store")
print(f"  {delisted:>4}  delisted — kept for their history, out of every queue")
print(f"  {excluded:>4}  excluded — discontinued lines")
print(f"  {scope:>4}  IN SCOPE for SEO work")

# ── FIELD BY FIELD ───────────────────────────────────────────────────────────
# The distinction that matters everywhere below: EMPTY is not the same as BAD.
# A field can be filled and still be wrong, and only the empty ones are in any
# queue today.
rule("PRODUCT FIELDS — in-scope products only")

empty_title = count(
    f"SELECT COUNT(*) FROM products WHERE {IN_SCOPE} AND (seo_title IS NULL OR seo_title = '')")
has_title = scope - empty_title
long_title = count(
    f"SELECT COUNT(*) FROM products WHERE {IN_SCOPE} AND LENGTH(seo_title) > 60")
empty_desc = count(
    f"SELECT COUNT(*) FROM products WHERE {IN_SCOPE} AND (seo_description IS NULL OR seo_description = '')")

print(f"  seo_title")
print(f"    {empty_title:>4}  empty        <- what the generator targets")
print(f"    {has_title:>4}  filled")
print(f"    {long_title:>4}  OVER 60 CHARS — Google truncates these, and they are")
print(f"          in NO queue because the field is not empty")
print(f"  seo_description")
print(f"    {empty_desc:>4}  empty")
print(f"    {scope - empty_desc:>4}  filled")

# Duplicate descriptions: siblings sharing copy compete with each other.
dupes = conn.execute(
    f"""
    SELECT COUNT(*) FROM (
        SELECT seo_description
        FROM products
        WHERE {IN_SCOPE} AND seo_description IS NOT NULL AND seo_description <> ''
        GROUP BY seo_description HAVING COUNT(*) > 1
    )
    """
).fetchone()[0]
print(f"    {dupes:>4}  duplicate description texts shared by 2+ products")

# ── IMAGES ───────────────────────────────────────────────────────────────────
rule("IMAGES — the grounding input, and alt text as its own SEO field")
rows = conn.execute(
    f"SELECT images FROM products WHERE {IN_SCOPE}").fetchall()
no_photo = sum(1 for r in rows if not r["images"])
total_images = blank_alt = 0
for row in rows:
    for image in (json.loads(row["images"]) if row["images"] else []):
        total_images += 1
        blank_alt += not image["alt_text"].strip()

print(f"  {total_images:>4}  photos stored")
print(f"  {no_photo:>4}  products with NO photo — can never be image-grounded")
print(f"  {blank_alt:>4}  photos with blank alt text"
      + (f"  ({100 * blank_alt / total_images:.0f}%)" if total_images else ""))
print("        alt text is what Google reads for image search — not yet generated")

# ── PROPOSALS ────────────────────────────────────────────────────────────────
rule("PROPOSALS — generated, not yet live")

# superseded_by IS NULL everywhere below. A superseded row is history: it was
# replaced by a regeneration and counting it inflates coverage with copy that
# will never be pushed. The first version of this report did count them and
# claimed 328 products covered when the real figure was 328 including 82 dead
# rows -- a number that looks like progress and is not.
LIVE_PROPOSAL = "superseded_by IS NULL"

print(f"  {count(f'SELECT COUNT(*) FROM proposals WHERE {LIVE_PROPOSAL}'):>4}  live rows")
print(f"  {count(f'SELECT COUNT(DISTINCT gid) FROM proposals WHERE {LIVE_PROPOSAL}'):>4}  products covered")
print(f"  {count('SELECT COUNT(*) FROM proposals WHERE superseded_by IS NOT NULL'):>4}  superseded — kept as history, never pushed")

orphaned = count("SELECT COUNT(*) FROM proposals WHERE superseded_by = -1")
if orphaned:
    print(f"  {orphaned:>4}  RETIRED WITH NO REPLACEMENT — run: regenerate.py orphans")

for row in conn.execute(
    f"""
    SELECT status, prompt_version, COUNT(*) AS n,
           SUM(grounding IS NOT NULL) AS with_why
    FROM proposals WHERE {LIVE_PROPOSAL}
    GROUP BY status, prompt_version ORDER BY n DESC
    """
):
    print(f"    {row['n']:>4}  {row['status']:<12} {row['prompt_version']:<5}"
          f" {row['with_why']} with reasoning")

dup_titles = count(
    f"""
    SELECT COUNT(*) FROM (
        SELECT proposed_value FROM proposals
        WHERE field = 'seo_title' AND status = 'draft' AND {LIVE_PROPOSAL}
        GROUP BY proposed_value HAVING COUNT(*) > 1
    )
    """
)
long_props = count(
    f"SELECT COUNT(*) FROM proposals WHERE field = 'seo_title' AND status = 'draft' "
    f"AND {LIVE_PROPOSAL} AND LENGTH(proposed_value) > 60"
)
print(f"\n  {dup_titles:>4}  DUPLICATE proposed titles — two products competing for one phrase")
print(f"  {long_props:>4}  proposed titles over 60 chars")

pending = count(
    f"""
    SELECT COUNT(*) FROM products
    WHERE {IN_SCOPE}
      AND (seo_title IS NULL OR seo_title = '')
      AND NOT EXISTS (SELECT 1 FROM proposals p WHERE p.gid = products.gid)
    """
)
print(f"\n  {pending:>4}  STILL TO GENERATE")

# ── PUSHES ───────────────────────────────────────────────────────────────────
rule("PUSHED TO THE LIVE STORE")
pushed = count("SELECT COUNT(*) FROM pushes")
verified = count("SELECT COUNT(*) FROM pushes WHERE verified_at IS NOT NULL")
rolled = count("SELECT COUNT(*) FROM pushes WHERE rolled_back_at IS NOT NULL")
print(f"  {pushed:>4}  writes attempted")
print(f"  {verified:>4}  verified by re-reading the store")
print(f"  {rolled:>4}  rolled back")
if pushed and pushed != verified + rolled:
    print(f"  {pushed - verified - rolled:>4}  NEITHER verified nor rolled back — orphaned rows")

# ── SCORES ───────────────────────────────────────────────────────────────────
rule("JUDGE SCORES")
scored = count("SELECT COUNT(*) FROM scores")
if scored:
    for row in conn.execute(
        """
        SELECT run_label, arm, COUNT(*) AS n,
               ROUND(AVG(accuracy), 1) AS acc, ROUND(AVG(search), 1) AS srch
        FROM scores WHERE accuracy IS NOT NULL
        GROUP BY run_label, arm
        """
    ):
        print(f"  {row['n']:>4}  {row['run_label']} / {row['arm'] or '-'}"
              f"   accuracy {row['acc']}  search {row['srch']}")
else:
    print("     0  nothing scored yet — verify.py is not built")

# ── NOT MIRRORED ─────────────────────────────────────────────────────────────
# Stated rather than omitted. These have real work outstanding and no row in
# this database, so every count above is silent about them.
rule("NOT IN THIS DATABASE — real work, invisible to every number above")
print("  collections     fetch.py mirrors products only. Per SEO-Field-Inventory")
print("                  §I this is the HIGHEST revenue-per-hour target left:")
print("                  one collection write reaches hundreds of products.")
print("  product.title   the H1 — strongest on-page signal. Design says generate,")
print("                  never push (§3a). Not generated by listing-v3.")
print("  body_description   near-term ask is stripping the keyword footer,")
print("                  not a rewrite. Not generated by listing-v3.")
print(f"  image alt text  {blank_alt} photos blank. Read by Google for image search.")
print("  metafields, structured data, technical — §D and §E, untouched.")

print("\n" + "=" * 74)
conn.close()
