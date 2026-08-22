"""Work through the refusals with the owner, one product at a time.

The model returns needs_human when it cannot settle a conflict between the
photograph and the stored text. It is right to stop — but it will keep stopping
on the same product forever, because nothing it has access to can resolve the
question. Only the person who owns the store can.

So this asks, and writes the answer down. The note is stored on the proposal
row (proposals.reviewer_note) and read back by owner_note_for() on every future
run, which means a fact stated once keeps applying. Answering the same question
twice is the failure this is built to avoid.

Nothing is written to the store. Proposals stay append-only: the refusal row is
kept and superseded, never edited.

Usage:
    python3 resolve.py           work through every refusal
    python3 resolve.py 5         just the first 5
"""
import json
import sys
import time

import db
import generate

LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else None
MODEL = "gemini:gemini-2.5-flash"
VERSION = "v5"

conn = db.connect()
db.init_schema(conn)

rows = conn.execute(
    """
    SELECT pr.id, pr.gid, pr.proposed_value AS refusal, pr.grounding,
           p.title, p.handle, p.tags, p.seo_title, p.seo_description, p.images
    FROM proposals pr
    JOIN products p ON p.gid = pr.gid
    WHERE pr.status = 'needs_human' AND pr.superseded_by IS NULL
    ORDER BY p.title
    """
).fetchall()

if LIMIT:
    rows = rows[:LIMIT]

if not rows:
    print("no refusals to resolve")
    sys.exit(0)

prompt_text, version_tag = generate.load_prompt(VERSION)

print(f"{len(rows)} product(s) the model refused to write copy for.")
print("For each one: say what is actually true, and it will write the copy.")
print()
print("  p  = the PHOTO is right, the text is wrong")
print("  t  = the TEXT is right, ignore what the photo seems to show")
print("  n  = type your own note (a name, a correction, anything)")
print("  s  = skip this one, leave it refused")
print("  q  = stop here, keep everything already answered")
print("=" * 96)

resolved = skipped = failed = 0

for number, row in enumerate(rows, 1):
    images = json.loads(row["images"]) if row["images"] else []

    print(f"\n[{number}/{len(rows)}] {row['title']}")
    print(f"  https://dynamocks.us/products/{row['handle']}")
    if images:
        print(f"  photo    {images[0]['url'].split('?')[0]}")
    print(f"  tags     {row['tags'] or '(none)'}")
    print(f"  on site  {(row['seo_description'] or '(no description)')[:150]}")
    print(f"  REFUSED  {row['refusal']}")
    if row["grounding"]:
        print(f"  the model saw:\n    {row['grounding'][:400]}")

    try:
        answer = input("\n  p / t / n / s / q  > ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\nstopped.")
        break

    if answer == "q":
        print("stopped.")
        break

    if answer == "s" or not answer:
        skipped += 1
        print("  skipped")
        continue

    if answer == "p":
        note = (
            "The photographs are correct and the stored text is not. "
            "Describe what you can see in the images. The product name and "
            "tags are internal grouping labels, not descriptions of the "
            "design — ignore any mismatch between them and the photo."
        )
    elif answer == "t":
        note = (
            "The stored text is correct. Write the copy from the title and "
            "description as given, even if the photographs appear to show "
            "something different."
        )
    elif answer == "n":
        try:
            note = input("  your note > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nstopped.")
            break
        if not note:
            skipped += 1
            print("  empty note — skipped")
            continue
    else:
        skipped += 1
        print("  not an option — skipped")
        continue

    # Retire the refusal BEFORE writing the replacement, same order push.py
    # uses for its undo row: if the run dies mid-product the row reads as
    # retired-with-no-successor, which is visibly broken rather than quietly
    # looking like two live proposals for one product.
    conn.execute(
        "UPDATE proposals SET superseded_by = -1 WHERE gid = :gid AND superseded_by IS NULL",
        {"gid": row["gid"]},
    )
    conn.commit()

    product = conn.execute(
        """
        SELECT gid, handle, title, product_type, tags,
               seo_description, seo_title, images
        FROM products WHERE gid = :gid
        """,
        {"gid": row["gid"]},
    ).fetchone()

    try:
        message = generate.build_message(
            product,
            prompt_text,
            generate.taken_titles(conn, product["product_type"]),
            owner_note=note,
        )
        fields = generate.parse_response(
            generate.call_model(message, MODEL, generate.fetch_image_parts(product["images"]))
        )

        if not fields["seo_title"]:
            # Still refused, or came back malformed. The note is saved anyway —
            # it was correct when typed and should not have to be typed again
            # just because this attempt failed.
            generate.save_proposal(
                conn, gid=row["gid"], field="seo_title",
                current_value=product["seo_title"],
                proposed_value=fields["needs_human"] or "(no title returned)",
                model=MODEL, prompt_version=version_tag,
                grounding=fields.get("grounding"), status="needs_human",
                reviewer_note=note,
            )
            new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "UPDATE proposals SET superseded_by = :new WHERE gid = :gid AND superseded_by = -1",
                {"new": new_id, "gid": row["gid"]},
            )
            conn.commit()
            failed += 1
            print(f"  STILL REFUSED — {fields['needs_human'] or 'no title returned'}")
            continue

        first_id = None
        for field in ("seo_title", "seo_description"):
            if not fields[field]:
                continue
            generate.save_proposal(
                conn, gid=row["gid"], field=field, current_value=product[field],
                proposed_value=fields[field], model=MODEL,
                prompt_version=version_tag, grounding=fields.get("grounding"),
                reviewer_note=note,
            )
            first_id = first_id or conn.execute(
                "SELECT last_insert_rowid()").fetchone()[0]

        conn.execute(
            "UPDATE proposals SET superseded_by = :new WHERE gid = :gid AND superseded_by = -1",
            {"new": first_id, "gid": row["gid"]},
        )
        conn.commit()
        resolved += 1

        print(f"  TITLE    {fields['seo_title']}")
        print(f"  DESC     {fields['seo_description']}")

    except Exception as error:
        conn.rollback()
        failed += 1
        print(f"  FAILED — {type(error).__name__}: {error}")

    time.sleep(generate._PRODUCT_PAUSE)

print("\n" + "=" * 96)
print(f"resolved : {resolved}")
print(f"skipped  : {skipped}")
print(f"still refused / failed : {failed}")

orphans = conn.execute(
    "SELECT COUNT(*) FROM proposals WHERE superseded_by = -1"
).fetchone()[0]
if orphans:
    print(f"!! {orphans} rows retired with no replacement — run: regenerate.py orphans")

print("\nYour notes are saved. Any future regeneration of these products will "
      "use them\nautomatically — you will not be asked again.")

conn.close()
