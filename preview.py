"""Print what generate.py WOULD write, without writing it.

Not the pipeline. Saves no proposals, touches no store.

This exists because save_proposal is a one-way door in practice: once a product
has a proposal row, get_products_needing_seo's NOT EXISTS clause takes it out
of the queue, so bad copy blocks its own replacement. Reviewing before writing
costs one extra run and avoids that entirely.

Usage:
    python3 preview.py               5 products, prompt v3, images on
    python3 preview.py 10 v2         10 products, text-only
"""
import html
import json
import sys
import textwrap
import time
from pathlib import Path

import db
import generate

REPORT = Path("preview.html")


def first_photo(images_json):
    """URL of the product's first stored photo, or None. Used for the report only."""
    images = json.loads(images_json) if images_json else []
    return images[0]["url"] if images else None

COUNT = int(sys.argv[1]) if len(sys.argv) > 1 else 5
VERSION = sys.argv[2] if len(sys.argv) > 2 else "v3"
MODEL = "gemini:gemini-2.5-flash"

prompt_text, version_tag = generate.load_prompt(VERSION)
use_images = version_tag == "v3"   # same rule generate.py uses; not a second switch

conn = db.connect()
products = generate.get_products_needing_seo(conn, limit=COUNT)

print(f"prompt   : listing-{version_tag}.md")
print(f"model    : {MODEL}")
print(f"images   : {'ON, up to %d per product' % generate._MAX_IMAGES if use_images else 'OFF'}")
print(f"products : {len(products)}")
print("DRY RUN — nothing will be written")
print("=" * 96)

over_60 = 0
missing = 0
refused = 0
failed = 0
no_photo = 0

# Collected as the run goes so the HTML is written even if a later product
# blows up. Each entry is one product's before, after and reasoning.
results = []

for number, product in enumerate(products, 1):
    print(f"\n[{number}/{len(products)}] {product['title'][:74]}")

    # What the store holds right now, printed before anything is generated.
    # Every product in this queue has an empty seo_title by definition -- that
    # is what put it here -- so showing it is not redundant, it is the proof
    # that the field really is blank and the blue link in Google is currently
    # being built from the product title instead.
    print("  CURRENTLY LIVE")
    print(f"    seo_title             {product['seo_title'] or '(empty)'}")
    print(f"    seo_description       {(product['seo_description'] or '(empty)')[:100]}")

    images = generate.fetch_image_parts(product["images"]) if use_images else []
    if use_images and not images:
        no_photo += 1
        print("  (no usable photo — text-only grounding)")

    started = time.time()
    try:
        reply = generate.call_model(
            generate.build_message(product, prompt_text), MODEL, images
        )
    except Exception as error:
        failed += 1
        print(f"  FAILED — {type(error).__name__}: {error}")
        continue
    elapsed = time.time() - started

    fields = generate.parse_response(reply)
    title = fields.get("seo_title")
    description = fields.get("seo_description")

    # Reasoning first, matching the order the model was asked to think in, and
    # labelled as not-published. A title that is right for the wrong reason
    # looks identical to a good one if you only ever read the output.
    if fields.get("grounding"):
        print(f"  REASONING (not published)")
        print(f"    {fields['grounding']}")

    if fields.get("needs_human"):
        # A refusal is compliance, not failure. Counted separately from a
        # missing field so the two never blur in the tally.
        refused += 1
        print(f"  NEEDS HUMAN — {fields['needs_human']}")
        print(f"  ({time.time() - started:.1f}s, {len(images)} image(s))")
        results.append(
            {
                "title": product["title"],
                "handle": product["handle"],
                "photo": first_photo(product["images"]),
                "live_title": product["seo_title"],
                "live_desc": product["seo_description"],
                "new_title": None,
                "new_desc": None,
                "grounding": fields.get("grounding"),
                "needs_human": fields["needs_human"],
            }
        )
        continue

    # What actually gets written to Shopify, shown against what is live now.
    # Side by side because "is this better than what we have" is the only
    # question a reviewer is really answering, and it cannot be answered
    # against the new copy alone.
    print("  GOES LIVE ON THE SITE")

    # Flags the one fault checkable without judgement. Quality is deliberately
    # not scored here -- CLAUDE.md: nothing that generates output may judge it.
    if title:
        flag = "   <-- OVER 60, Google truncates" if len(title) > 60 else ""
        if flag:
            over_60 += 1
        print(f"    seo_title       [{len(title):>2}]  {title}{flag}")
    else:
        missing += 1
        print("    seo_title             -- MISSING --")
        # Raw reply, so a parser failure is visibly different from a model
        # that simply declined to answer.
        print(f"    raw reply             {reply.strip()[:150]}")

    if description:
        print(f"    seo_description       {description}")
    else:
        missing += 1
        print("    seo_description       -- MISSING --")

    print(f"  ({elapsed:.1f}s, {len(images)} image(s))")

    results.append(
        {
            "title": product["title"],
            "handle": product["handle"],
            "photo": first_photo(product["images"]),
            "live_title": product["seo_title"],
            "live_desc": product["seo_description"],
            "new_title": title,
            "new_desc": description,
            "grounding": fields.get("grounding"),
            "needs_human": None,
        }
    )

print("\n" + "=" * 96)
print(f"over 60 chars : {over_60}")
print(f"missing fields: {missing}")
print(f"failed calls  : {failed}")
print(f"needs_human   : {refused}   (a refusal is the prompt working, not a fault)")
if use_images:
    print(f"no photo      : {no_photo}")

# ── TERMINAL TABLE ───────────────────────────────────────────────────────────
# Titles only. Descriptions are 150+ characters and a terminal column that wide
# wraps into porridge -- they go in the HTML instead, where the width is real.
print("\nPROPOSED TITLES")
print(f"{'LEN':>3}  {'PRODUCT':<34}  TITLE")
print("-" * 118)
for row in results:
    if row["needs_human"]:
        print(f"{'--':>3}  {row['title'][:34]:<34}  NEEDS HUMAN — {row['needs_human'][:50]}")
        continue
    new = row["new_title"]
    length = len(new) if new else 0
    mark = " *" if length > 60 else ""
    print(f"{length:>3}{mark:<2} {row['title'][:34]:<34}  {new or '-- MISSING --'}")
print("-" * 118)
print("* over 60 characters — Google truncates")

# ── HTML REPORT ──────────────────────────────────────────────────────────────
# The photo has to be in the review. The whole claim of v3 is that the copy was
# written from the picture, and that claim cannot be checked against a title
# alone -- you have to see the sock the words are describing.
cells = []
for row in results:
    photo = (f'<img src="{html.escape(row["photo"])}" loading="lazy">'
             if row["photo"] else '<div class="nophoto">no photo</div>')

    if row["needs_human"]:
        proposed = f'<p class="flag">NEEDS HUMAN — {html.escape(row["needs_human"])}</p>'
    else:
        title = row["new_title"] or ""
        over = " over" if len(title) > 60 else ""
        proposed = (
            f'<p class="t{over}">{html.escape(title) or "-- MISSING --"}'
            f'<span class="len">{len(title)} chars</span></p>'
            f'<p class="d">{html.escape(row["new_desc"] or "-- MISSING --")}</p>'
        )

    cells.append(f"""
      <tr>
        <td class="pic">{photo}</td>
        <td>
          <p class="name">{html.escape(row["title"])}</p>
          <p class="was">{html.escape(row["live_title"] or "(no seo title)")}</p>
          <p class="was">{html.escape((row["live_desc"] or "(no seo description)")[:180])}</p>
        </td>
        <td>{proposed}</td>
        <td class="why">{html.escape(row["grounding"] or "(no reasoning returned)")}</td>
      </tr>""")

REPORT.write_text(f"""<!doctype html>
<meta charset="utf-8"><title>SEO preview — listing-{version_tag}</title>
<style>
 body{{font:14px/1.5 -apple-system,system-ui,sans-serif;margin:24px;color:#111}}
 h1{{font-size:18px;margin:0 0 4px}} .sub{{color:#666;margin:0 0 18px}}
 table{{border-collapse:collapse;width:100%}}
 th{{text-align:left;font-size:11px;letter-spacing:.08em;text-transform:uppercase;
    color:#666;border-bottom:2px solid #ddd;padding:8px}}
 td{{border-bottom:1px solid #eee;padding:12px 8px;vertical-align:top}}
 .pic{{width:110px}} .pic img{{width:100px;height:100px;object-fit:cover;border-radius:6px}}
 .nophoto{{width:100px;height:100px;background:#f4f4f4;border-radius:6px;color:#999;
   font-size:11px;display:flex;align-items:center;justify-content:center}}
 .name{{font-weight:600;margin:0 0 6px}}
 .was{{color:#888;margin:0 0 4px;font-size:13px}}
 .t{{font-weight:600;margin:0 0 6px}}
 .t.over{{color:#b00}} .t.over .len{{background:#fee;color:#b00}}
 .len{{font-weight:400;font-size:11px;color:#666;background:#f2f2f2;
   padding:1px 6px;border-radius:9px;margin-left:8px}}
 .d{{margin:0;color:#333}}
 .why{{color:#555;font-size:13px;width:26%;background:#fafafa}}
 .flag{{color:#b00;margin:0}}
</style>
<h1>SEO preview — listing-{version_tag}.md</h1>
<p class="sub">{len(results)} products · {MODEL} · images {"on" if use_images else "off"}
 · nothing written to the database or the store</p>
<table>
 <tr><th>photo</th><th>product / currently live</th><th>proposed — goes live</th>
     <th>reasoning — not published</th></tr>
 {"".join(cells)}
</table>
""", encoding="utf-8")

print(f"\nreport written: {REPORT.resolve()}")
print("  open it to review the copy against the actual product photos")

print("\nNothing was written to the database. To generate for real:")
print(f"  python3 -c \"import db, generate; c=db.connect(); "
      f"generate.generate_for_products(c, limit={COUNT}, "
      f"model_ref='{MODEL}', prompt_version='{version_tag}'); c.close()\"")

conn.close()
