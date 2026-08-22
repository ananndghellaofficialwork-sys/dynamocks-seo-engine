"""Export draft proposals for a human to review. Read-only.

Not the pipeline. Writes two files and touches neither the database nor the
store.

  review.html  — to READ. Product photo beside the current copy, the proposed
                 copy, and the model's reasoning. Built for someone who does
                 not use a terminal.
  review.csv   — to FILL IN. Opens in Excel or Google Sheets. Two blank
                 columns: decision, and a rewrite if the decision is "edit".

Two files rather than one because reading and editing want opposite things.
A page that shows the sock is the only way to judge whether the copy describes
it; a spreadsheet is the only way to hand back 200 verdicts without retyping
anything. Neither alone does the job.

Nothing here writes a decision back. Importing the filled-in CSV is a separate
module, deliberately: proposals is append-only, and the row that records a
human's verdict has to be written under that rule rather than bolted onto an
export script.

Usage:
    python3 review.py              every draft proposal
    python3 review.py 50           the 50 most recent
"""
import csv
import html
import json
import sys
from pathlib import Path

import db
from verify import PASS_ACCURACY   # one definition of the gate, not two

LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else None

HTML_OUT = Path("review.html")
CSV_OUT = Path("review.csv")

conn = db.connect()

# One row per product, with both fields side by side. The two proposals for a
# product were written by one call and share one grounding line, so splitting
# them across two review rows would ask the reviewer the same question twice
# and show them the photo twice.
rows = conn.execute(
    """
    SELECT
        pr.gid                                                        AS gid,
        p.title                                                       AS product,
        p.handle                                                      AS handle,
        p.images                                                      AS images,
        p.seo_title                                                   AS live_title,
        p.seo_description                                             AS live_desc,
        MAX(CASE WHEN pr.field = 'seo_title'       THEN pr.id END)     AS title_id,
        MAX(CASE WHEN pr.field = 'seo_title'       THEN pr.proposed_value END) AS new_title,
        MAX(CASE WHEN pr.field = 'seo_description' THEN pr.id END)     AS desc_id,
        MAX(CASE WHEN pr.field = 'seo_description' THEN pr.proposed_value END) AS new_desc,
        MAX(pr.grounding)                                             AS grounding,
        MAX(pr.status)                                                AS status,
        MAX(pr.model)                                                 AS model,
        MAX(pr.prompt_version)                                        AS prompt_version,
        MAX(pr.created_at)                                            AS created_at,
        -- Newest score per field. MAX(id) picks the latest judgement without
        -- a correlated subquery per row; scores is append-only so the highest
        -- id for a (gid, arm) pair is always the current one.
        (SELECT s.accuracy FROM scores s WHERE s.gid = pr.gid AND s.arm = 'seo_title'
          ORDER BY s.id DESC LIMIT 1)                                  AS title_accuracy,
        (SELECT s.search FROM scores s WHERE s.gid = pr.gid AND s.arm = 'seo_title'
          ORDER BY s.id DESC LIMIT 1)                                  AS title_search,
        (SELECT s.accuracy FROM scores s WHERE s.gid = pr.gid AND s.arm = 'seo_description'
          ORDER BY s.id DESC LIMIT 1)                                  AS desc_accuracy,
        (SELECT s.search FROM scores s WHERE s.gid = pr.gid AND s.arm = 'seo_description'
          ORDER BY s.id DESC LIMIT 1)                                  AS desc_search,
        (SELECT s.reason FROM scores s WHERE s.gid = pr.gid
          ORDER BY s.id DESC LIMIT 1)                                  AS judge_reason
    FROM proposals pr
    JOIN products p ON p.gid = pr.gid
    WHERE pr.superseded_by IS NULL
    GROUP BY pr.gid
    -- Worst first, unscored at the very top. A reviewer's attention is the
    -- scarcest thing in this process and it should meet the risky copy while
    -- it is still fresh, not after 200 good ones have taught them to skim.
    ORDER BY (title_accuracy IS NULL) DESC, title_accuracy ASC, title_search ASC
    """
).fetchall()

if LIMIT:
    rows = rows[:LIMIT]

if not rows:
    print("no proposals to review — run the generator first")
    sys.exit(1)


def photo_of(images_json):
    """First stored photo URL, asked of the CDN at review size. None if no photo."""
    images = json.loads(images_json) if images_json else []
    if not images:
        return None
    url = images[0]["url"]
    return f"{url}{'&' if '?' in url else '?'}width=300"


# ── CSV: the file she fills in ───────────────────────────────────────────────
# proposal ids are carried through so the importer can attach each verdict to
# the exact row it judged. Without them a reviewer's edit could only be matched
# back by product name, which breaks the moment a product is renamed.
with CSV_OUT.open("w", newline="", encoding="utf-8") as handle:
    writer = csv.writer(handle)
    writer.writerow([
        # Scores come FIRST, before the copy. The column that tells a reviewer
        # where to spend attention is worth nothing in column nine, off the
        # right-hand edge of the screen.
        "CHECK_FIRST",
        "title_accuracy_0_5", "title_search_0_5",
        "desc_accuracy_0_5", "desc_search_0_5",
        "what_the_judge_flagged",
        "product", "link",
        "current_title", "proposed_title",
        "current_description", "proposed_description",
        "why_the_model_chose_this",
        "DECISION (approve / reject / edit)",
        "YOUR_TITLE (only if edit)",
        "YOUR_DESCRIPTION (only if edit)",
        "title_id", "desc_id",
    ])
    for row in rows:
        accuracy = row["title_accuracy"]
        if row["status"] == "needs_human":
            check = "MODEL REFUSED"
        elif accuracy is None:
            check = "NOT SCORED"
        elif accuracy < PASS_ACCURACY:
            check = "LOOK AT THIS"
        else:
            check = ""

        writer.writerow([
            check,
            "" if accuracy is None else accuracy,
            "" if row["title_search"] is None else row["title_search"],
            "" if row["desc_accuracy"] is None else row["desc_accuracy"],
            "" if row["desc_search"] is None else row["desc_search"],
            row["judge_reason"] or "",
            row["product"],
            f"https://dynamocks.us/products/{row['handle']}",
            row["live_title"] or "",
            row["new_title"] or "",
            row["live_desc"] or "",
            row["new_desc"] or "",
            row["grounding"] or "",
            "", "", "",
            row["title_id"] or "", row["desc_id"] or "",
        ])

# ── HTML: the file she reads ─────────────────────────────────────────────────
cards = []
for row in rows:
    photo = photo_of(row["images"])
    picture = (f'<img src="{html.escape(photo)}" loading="lazy">'
               if photo else '<div class="nophoto">no photo</div>')

    # One badge per axis, coloured only when it needs attention. A badge on
    # every card in the same colour is decoration; a red one has to mean
    # something or the reviewer stops seeing it.
    def badge(label, value):
        if value is None:
            return f'<span class="s none">{label} not checked</span>'
        klass = "s bad" if value < PASS_ACCURACY else "s ok"
        return f'<span class="{klass}">{label} {value}/5</span>'

    scores = (
        badge("accuracy", row["title_accuracy"])
        + badge("search", row["title_search"])
    )

    if row["status"] == "needs_human":
        proposed = (f'<p class="flag">The model refused to guess — '
                    f'{html.escape(row["new_title"] or "")}</p>')
    else:
        title = row["new_title"] or ""
        over = " over" if len(title) > 60 else ""
        proposed = (
            f'<p class="new{over}">{html.escape(title) or "(none)"}'
            f'<span class="len">{len(title)}</span></p>'
            f'<p class="newd">{html.escape(row["new_desc"] or "(none)")}</p>'
        )

    reason_block = (
        f'<p class="judgereason">Judge flagged: '
        f'{html.escape(row["judge_reason"])}</p>'
        if row["judge_reason"] and (row["title_accuracy"] is None
                                    or row["title_accuracy"] < PASS_ACCURACY)
        else ""
    )

    cards.append(f"""
  <article>
    <div class="pic">{picture}</div>
    <div class="body">
      <h2><a href="https://dynamocks.us/products/{html.escape(row['handle'])}"
             target="_blank">{html.escape(row["product"])}</a></h2>
      <div class="cols">
        <section>
          <h3>On the site now</h3>
          <p class="old">{html.escape(row["live_title"] or "(no title — Google is guessing)")}</p>
          <p class="old">{html.escape((row["live_desc"] or "(no description)")[:220])}</p>
        </section>
        <section>
          <h3>Proposed</h3>
          {proposed}
          <p class="scores">{scores}</p>
        </section>
      </div>
      {reason_block}
      <details><summary>Why the model chose this</summary>
        <p>{html.escape(row["grounding"] or "(no reasoning recorded)")}</p></details>
    </div>
  </article>""")

HTML_OUT.write_text(f"""<!doctype html>
<meta charset="utf-8"><title>Dynamocks SEO — for review</title>
<style>
 body{{font:15px/1.55 -apple-system,system-ui,sans-serif;margin:0;background:#f6f6f7;color:#111}}
 header{{background:#fff;border-bottom:1px solid #e3e3e6;padding:20px 28px;
   position:sticky;top:0}}
 h1{{margin:0;font-size:19px}} .sub{{margin:4px 0 0;color:#666;font-size:13px}}
 main{{max-width:1080px;margin:0 auto;padding:20px 16px 60px}}
 article{{display:flex;gap:18px;background:#fff;border:1px solid #e3e3e6;
   border-radius:10px;padding:16px;margin-bottom:14px}}
 .pic img{{width:132px;height:132px;object-fit:cover;border-radius:8px}}
 .nophoto{{width:132px;height:132px;background:#f0f0f2;border-radius:8px;color:#999;
   font-size:12px;display:flex;align-items:center;justify-content:center}}
 .body{{flex:1;min-width:0}}
 h2{{font-size:15px;margin:0 0 12px}} h2 a{{color:#111;text-decoration:none}}
 h2 a:hover{{text-decoration:underline}}
 .cols{{display:grid;grid-template-columns:1fr 1fr;gap:20px}}
 h3{{font-size:11px;letter-spacing:.07em;text-transform:uppercase;color:#888;
   margin:0 0 6px;font-weight:600}}
 .old{{color:#8a8a8a;margin:0 0 5px;font-size:13.5px}}
 .new{{font-weight:600;margin:0 0 5px}}
 .new.over{{color:#c00}} .new.over .len{{background:#fdecec;color:#c00}}
 .len{{font-weight:400;font-size:11px;color:#777;background:#f0f0f2;
   padding:1px 7px;border-radius:9px;margin-left:8px}}
 .newd{{margin:0;font-size:13.5px}}
 .flag{{color:#c00;margin:0}}
 .scores{{margin:10px 0 0}}
 .s{{font-size:11px;padding:2px 8px;border-radius:10px;margin-right:6px;
   background:#eef7ee;color:#2a6b2a}}
 .s.bad{{background:#fdecec;color:#c00;font-weight:600}}
 .s.none{{background:#f0f0f2;color:#888}}
 .judgereason{{margin:10px 0 0;padding:8px 10px;background:#fff8e6;
   border-left:3px solid #e0a800;font-size:13px;color:#5a4a1a}}
 details{{margin-top:12px;border-top:1px solid #f0f0f2;padding-top:10px}}
 summary{{cursor:pointer;color:#666;font-size:12.5px}}
 details p{{margin:8px 0 0;color:#555;font-size:13px}}
</style>
<header>
  <h1>Dynamocks — proposed SEO copy</h1>
  <p class="sub">{len(rows)} products · nothing is live yet · mark your decisions
    in <b>review.csv</b>. Sorted worst-first: anything needing a look is at the top.
    Red badge = the judge scored it below {PASS_ACCURACY}/5.</p>
</header>
<main>{"".join(cards)}</main>
""", encoding="utf-8")

over = sum(1 for r in rows if (r["new_title"] or "") and len(r["new_title"]) > 60)
refused = sum(1 for r in rows if r["status"] == "needs_human")

print(f"products      : {len(rows)}")
print(f"over 60 chars : {over}")
print(f"needs_human   : {refused}")
print()
print(f"to read : {HTML_OUT.resolve()}")
print(f"to fill : {CSV_OUT.resolve()}")
print("\nNothing was written to the database or the store.")

conn.close()
