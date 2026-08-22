"""Re-run the generator over products whose proposals came out wrong.

The queue in generate.py deliberately skips anything that already has a
proposal, which makes a run resumable. That same rule makes a BAD proposal
permanent — the product never comes back. This is the way back in.

Append-only is respected. Nothing is edited and nothing is deleted: the old
rows stay exactly as written, and their superseded_by is set to point at the
row that replaced them. That pointer is the whole audit trail — six months from
now, "why did this title change" is a query rather than a guess.

Modes:
    duplicates   products sharing a proposed seo_title with another product
    needs_human  products the model refused to write copy for
    too_long     proposals whose title is over 60 characters
    prompt:v1    products whose live copy came from an older prompt
    orphans      repair rows left retired with no replacement
    <gid>        one specific product

Usage:
    python3 regenerate.py duplicates            show what would be redone
    python3 regenerate.py duplicates --write    redo it
    python3 regenerate.py needs_human --write
    python3 regenerate.py prompt:v1 --write
"""
import sys

import db
import generate

MODE = sys.argv[1] if len(sys.argv) > 1 else "duplicates"
WRITE = "--write" in sys.argv
MODEL = "gemini:gemini-2.5-flash"
VERSION = "v6"

conn = db.connect()
db.init_schema(conn)


def _link(conn, gid, new_id):
    """Point this product's retired rows at the row that replaced them."""
    conn.execute(
        "UPDATE proposals SET superseded_by = :new "
        "WHERE gid = :gid AND superseded_by = -1",
        {"new": new_id, "gid": gid},
    )


def repair_orphans(conn):
    """
    Re-link rows left at -1 by an earlier interrupted or buggy run.

    -1 means "retired, replacement unknown". Two ways out, and which one is
    correct depends on whether a replacement actually exists:

      - a newer row for that product exists  -> point at it, the retirement was
        real and only the pointer was lost.
      - no newer row exists                  -> reset to NULL. The row was
        retired for a replacement that was never written, so it is still the
        live proposal and pretending otherwise would hide a product from the
        review export entirely.

    Reset-to-NULL is the safer default of the two: a product with a stale
    proposal gets reviewed and rejected, while a product with no proposal at
    all is simply invisible.
    """
    stranded = conn.execute(
        "SELECT id, gid FROM proposals WHERE superseded_by = -1"
    ).fetchall()

    linked = restored = 0
    for row in stranded:
        newer = conn.execute(
            "SELECT id FROM proposals WHERE gid = :gid AND id > :id "
            "AND superseded_by IS NULL ORDER BY id DESC LIMIT 1",
            {"gid": row["gid"], "id": row["id"]},
        ).fetchone()

        if newer:
            conn.execute(
                "UPDATE proposals SET superseded_by = :new WHERE id = :id",
                {"new": newer["id"], "id": row["id"]},
            )
            linked += 1
        else:
            conn.execute(
                "UPDATE proposals SET superseded_by = NULL WHERE id = :id",
                {"id": row["id"]},
            )
            restored += 1

    conn.commit()
    return linked, restored


def targets():
    """The gids this mode is about, newest proposal first."""
    if MODE == "duplicates":
        # A title used by more than one product. Both sides are regenerated,
        # not just one: picking a winner would mean deciding which product
        # deserves the phrase, and that is a judgement this script has no
        # grounds to make.
        return conn.execute(
            """
            SELECT DISTINCT gid FROM proposals
            WHERE field = 'seo_title' AND status = 'draft' AND superseded_by IS NULL
              AND proposed_value IN (
                  SELECT proposed_value FROM proposals
                  WHERE field = 'seo_title' AND status = 'draft'
                    AND superseded_by IS NULL
                  GROUP BY proposed_value HAVING COUNT(*) > 1
              )
            """
        ).fetchall()

    if MODE == "needs_human":
        return conn.execute(
            "SELECT DISTINCT gid FROM proposals "
            "WHERE status = 'needs_human' AND superseded_by IS NULL"
        ).fetchall()

    if MODE == "too_long":
        # A proposal that breaks the rule the prompt itself sets. Regenerating
        # is cheaper than reviewing it, and shipping it would put a truncated
        # blue link on the store — the exact defect this run set out to fix.
        return conn.execute(
            "SELECT DISTINCT gid FROM proposals "
            "WHERE field = 'seo_title' AND status = 'draft' AND superseded_by IS NULL "
            "AND LENGTH(proposed_value) > :max",
            {"max": generate._MAX_TITLE_CHARS},
        ).fetchall()

    if MODE.startswith("prompt:"):
        # Everything still carrying copy from an older prompt. Named by version
        # rather than "anything not current" on purpose: a sweep of everything
        # older would regenerate 260 perfectly good v3 products the first time
        # v5 exists, and re-paying for good copy is not a maintenance task.
        wanted = MODE.split(":", 1)[1]
        return conn.execute(
            "SELECT DISTINCT gid FROM proposals "
            "WHERE prompt_version = :version AND superseded_by IS NULL",
            {"version": wanted},
        ).fetchall()

    return conn.execute(
        "SELECT DISTINCT gid FROM proposals WHERE gid = :gid AND superseded_by IS NULL",
        {"gid": MODE},
    ).fetchall()


# Runs before targets() is consulted: an orphaned row is a broken pointer, not
# a product needing copy, and repairing it costs no API calls.
if MODE == "orphans":
    linked, restored = repair_orphans(conn)
    print(f"re-linked to a replacement                : {linked}")
    print(f"restored to live (no replacement written) : {restored}")
    conn.close()
    sys.exit(0)

rows = targets()
gids = [row["gid"] for row in rows]

if not gids:
    print(f"nothing matches mode {MODE!r}")
    sys.exit(0)

print(f"mode     : {MODE}")
print(f"products : {len(gids)}")
print(f"prompt   : listing-{VERSION}.md")
print("-" * 92)

for gid in gids:
    product = conn.execute(
        "SELECT title FROM products WHERE gid = :gid", {"gid": gid}
    ).fetchone()
    current = conn.execute(
        "SELECT proposed_value FROM proposals WHERE gid = :gid AND field = 'seo_title' "
        "AND superseded_by IS NULL ORDER BY id DESC LIMIT 1",
        {"gid": gid},
    ).fetchone()
    print(f"  {product['title'][:52]:<52}  {(current['proposed_value'] or '')[:36]}")

print("-" * 92)

if not WRITE:
    print(f"DRY RUN — nothing changed. Re-run with --write to regenerate these {len(gids)}.")
    conn.close()
    sys.exit(0)

# Mark the old rows superseded BEFORE generating, not after.
#
# The order matters and it is the same argument push.py makes for writing the
# undo row first: taken_titles() reads draft proposals with superseded_by IS
# NULL, so if the old titles were still live the model would be handed the very
# duplicates it is being asked to replace and told not to reuse them — which
# would forbid the correct answer for whichever product legitimately owns the
# phrase. Retiring them first empties the field.
old_ids = [
    row["id"]
    for row in conn.execute(
        f"""
        SELECT id FROM proposals
        WHERE superseded_by IS NULL
          AND gid IN ({",".join("?" * len(gids))})
        """,
        gids,
    ).fetchall()
]

# -1 is a placeholder: "retired, replacement not yet known". It is overwritten
# below with the real id. If the run dies in between, these rows read as
# retired-with-no-successor, which is visibly broken rather than silently
# looking like live proposals.
conn.executemany(
    "UPDATE proposals SET superseded_by = -1 WHERE id = ?", [(i,) for i in old_ids]
)
conn.commit()
print(f"retired {len(old_ids)} old proposal rows")

prompt_text, version_tag = generate.load_prompt(VERSION)
succeeded = 0
failed = 0
refused = 0

for index, gid in enumerate(gids):
    if index:
        import time
        time.sleep(generate._PRODUCT_PAUSE)

    product = conn.execute(
        """
        SELECT gid, handle, title, product_type, tags,
               seo_description, seo_title, images, body_html, material
        FROM products WHERE gid = :gid
        """,
        {"gid": gid},
    ).fetchone()

    try:
        taken = generate.taken_titles(conn, product["product_type"])
        # A note the owner typed in resolve.py outranks everything and must
        # survive regeneration, or the same question gets asked again.
        note = generate.owner_note_for(conn, gid)
        message = generate.build_message(product, prompt_text, taken, owner_note=note)
        images = generate.fetch_image_parts(product["images"])
        fields = generate.parse_response(
            generate.call_model(message, MODEL, images)
        )

        if fields["needs_human"] and not fields["seo_title"]:
            generate.save_proposal(
                conn, gid=gid, field="seo_title", current_value=product["seo_title"],
                proposed_value=fields["needs_human"], model=MODEL,
                prompt_version=version_tag, grounding=fields.get("grounding"),
                status="needs_human",
            )
            # The refusal row IS the replacement. Missing this left the retired
            # rows stranded at -1 on any product that refused -- reported as
            # "0 failed" with orphans, which is the run telling you two
            # different things at once.
            _link(conn, gid, conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            conn.commit()
            refused += 1
            print(f"  ? {product['handle']} — still NEEDS HUMAN")
            continue

        written = []
        for field in ("seo_title", "seo_description"):
            if not fields[field]:
                print(f"  ! {product['handle']} — {field} MISSING")
                continue
            generate.save_proposal(
                conn, gid=gid, field=field, current_value=product[field],
                proposed_value=fields[field], model=MODEL,
                prompt_version=version_tag, grounding=fields.get("grounding"),
            )
            written.append(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

        # Point the retired rows at their replacement. Done per product so a
        # failure later in the run cannot leave earlier products pointing at -1.
        if written:
            _link(conn, gid, written[0])
        conn.commit()
        succeeded += 1
        print(f"  {product['handle']}: {fields['seo_title']}")

    except Exception as error:
        conn.rollback()
        failed += 1
        print(f"  ! {product['handle']} FAILED — {type(error).__name__}: {error}")

orphans = conn.execute(
    "SELECT COUNT(*) FROM proposals WHERE superseded_by = -1"
).fetchone()[0]

print("-" * 92)
print(f"done — {succeeded} regenerated, {refused} still needs_human, {failed} failed")
if orphans:
    print(f"!! {orphans} rows still retired with no replacement — those products "
          f"failed. Re-run this command; they are the only ones left at -1.")

conn.close()
