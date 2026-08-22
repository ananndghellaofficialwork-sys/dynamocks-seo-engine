"""
generate.py — stage 3 of the pipeline: LLM drafts -> proposals.

Why this file exists:
  Something has to turn a product row into candidate SEO copy. That is one
  job: read products out of the local mirror, send each one through the
  versioned prompt in prompts/, and append the model's answer to the
  proposals table as a draft. Keeping it in its own module means the copy
  rules, the model choice and the database write are all changeable without
  touching fetch.py or push.py.

What it does:
  - Loads prompts/listing-{version}.md and pairs it with one product's real
    field values, so every claim the model can make is grounded in the live
    listing.
  - Dispatches the call to whichever provider SEO_MODEL names, splitting
    "provider:model_id" on the first colon only.
  - Parses seo_title and seo_description out of the reply and appends one
    proposal row per field.

What it is FORBIDDEN from doing:
  - Never calls Shopify. Only push.py may talk to the store, and only push.py
    may write to it.
  - Never writes to the products table. products is a disposable mirror owned
    by fetch.py; this module reads it and nothing more.
  - Never UPDATEs or DELETEs a proposal. proposals is append-only — a revision
    is a new row pointed at by superseded_by, written by a later stage.
  - Never invents a product fact, and never substitutes one when the model
    omits a field. A missing field is returned as None and reported, because
    a silently back-filled field is the exact defect this project exists to
    remove.
  - Never scores, compares, gates or retries. verify.py is a separate module,
    written by a separate pass, so that nothing which generates output also
    judges it.
"""

import base64  # inline image bytes for the multimodal request
import datetime
import json
import os
import re
import sqlite3
import time  # backoff between retries when the provider is overloaded
from pathlib import Path

import requests
from dotenv import load_dotenv

import db
import keywords

load_dotenv()  # populate os.environ from .env; individual keys are read at call time, not here

PROMPT_DIR = Path("prompts")  # versioned copy rules live here, one file per version

# Provider endpoints. Gemini is the only one wired up in this pass; the other
# two are declared so call_model() can fail with a specific message rather than
# "unknown provider" for a provider that is planned but not yet implemented.
_GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model_id}:generateContent"
_NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
_OPENAI_BASE_URL = "https://api.openai.com/v1"

_TIMEOUT = 60  # seconds; a hung provider must not stall a 400-product run indefinitely

# Statuses worth asking again about: rate limited, or the provider is briefly
# broken. Everything else is our problem and retrying it changes nothing.
_RETRY_STATUS = {429, 500, 502, 503, 504}
_ATTEMPTS = 4  # backs off 2s, 4s, 8s between tries

# Image grounding (DESIGN-v2 §12a). Photos are sent in store order, unfiltered.
#
# 3, not 5. Every image is tokens, and Gemini's free tier limits TOKENS per
# minute as well as requests — five 2000x2000 photos per product hits that
# ceiling long before the request-count one, which is what produced a wall of
# 429s. Three frames is still enough to see past a banner in position 0.
_MAX_IMAGES = 3
_MAX_IMAGE_BYTES = 4_000_000   # skip one oversized photo rather than stall the run
_IMAGE_TIMEOUT = 180      # a multimodal request carries megabytes; 60s is not enough

# Shopify's CDN resizes on request: append width= and it serves a smaller file.
# Free, instant, and no new dependency — the alternative was adding Pillow to
# resize locally, which downloads the full 2000x2000 original anyway and only
# then throws most of it away.
#
# 640px is well above what pattern recognition needs. The catalog's originals
# are 2000-2600px, so this cuts roughly an order of magnitude off both the
# download and the token count, and the token count is the quota that broke.
_IMAGE_WIDTH = 640

# Google truncates a title around here, so anything longer is not a title the
# shopper ever reads in full. listing-v3.md enforces it on new copy; this
# constant is what puts ALREADY-WRITTEN long titles back into the queue.
_MAX_TITLE_CHARS = 60

# Breather between products. The per-minute quota is a rolling window, so a
# tight loop can exhaust it in seconds and then spend every retry hitting the
# same wall. Cheap insurance; remove it on a paid tier if throughput matters.
_PRODUCT_PAUSE = 2.0

# Matches a labelled field line in the model's reply, tolerating the decoration
# models add around labels: "seo.title:", "**seo_title:**", '"seo_title":',
# "## seo title". Group 1 is title|description, group 2 is whatever followed on
# the same line (empty when the label was a heading and the copy is below it).
_FIELD_LINE = re.compile(
    r"[^\w]*seo[._ ]?(title|description)[^\w:]*:?\s*(.*)",
    re.IGNORECASE,
)

# listing-v3.md asks for a grounding line before the two fields: what the model
# saw, what it ignored, and which word it rejected. Matched by its own pattern
# because it carries no "seo" prefix, and kept separate from _FIELD_LINE so a
# prompt without it parses exactly as before rather than reporting a new
# missing field on every product.
_GROUNDING_LINE = re.compile(
    r"[^\w]*grounding[^\w:]*:?\s*(.*)",
    re.IGNORECASE,
)

# The refusal path. A model that returns this instead of the two fields has
# followed instructions, not failed — the distinction has to survive parsing or
# a correct refusal reads as a broken call.
# product_title is the store's H1. Parsed from its own pattern rather than
# folded into _FIELD_LINE, because that pattern requires an "seo" prefix and
# this field has none — and because the two must stay visibly separate: one is
# pushable and one is refused by name in push.py.
_PRODUCT_TITLE_LINE = re.compile(
    r"[^\w]*product[._ ]?title[^\w:]*:?\s*(.*)",
    re.IGNORECASE,
)

_NEEDS_HUMAN_LINE = re.compile(
    r"[^\w]*needs[._ ]?human[^\w:]*:?\s*(.*)",
    re.IGNORECASE,
)

# Characters stripped from both ends of a parsed value: quotes, markdown bold,
# backticks and the trailing comma left behind by a JSON-shaped reply.
_DECORATION = " \t\"'`*,"

# Running totals for Gemini's implicit prompt cache, read off usageMetadata.
CACHE = {"cached": 0, "total": 0}

_INSERT_PROPOSAL = """
INSERT INTO proposals
(
    gid, field, current_value, proposed_value,
    model, prompt_version, created_at, grounding, status, reviewer_note
)
VALUES
(
    :gid, :field, :current_value, :proposed_value,
    :model, :prompt_version, :created_at, :grounding, :status, :reviewer_note
)
"""


def generate_for_products(
    conn: sqlite3.Connection,
    limit: int,
    model_ref: str,
    prompt_version: str,
) -> None:
    """
    WHAT IT DOES:
      Runs the whole generate stage end to end. Loads the prompt once, pulls
      the products that still have no seo_title and no proposal yet, and for
      each one builds a message, calls the model, parses the reply, and
      appends one proposal row per field. Prints a line per product so a run
      can be watched.

      Commits after every product, and isolates every product in a try/except:
      one bad response is printed and stepped over, and the products already
      finished stay committed. It does not retry, back off or sleep. A failed
      product is simply left without a proposal, and the NOT EXISTS clause in
      get_products_needing_seo() hands it back on the next run.

      Finishes by reading the proposals count back out of the database — read
      from the table, not accumulated in a variable, so the number printed is
      evidence the rows actually landed — alongside the success and failure
      tallies for this run.

      Called by: the operator, from the REPL, once per run. Nothing in the
                 codebase calls it; this module has no main() by design,
                 because at L0 a human decides when a batch is generated.

      In the pipeline: products table (filled by fetch.py)
                         -> generate_for_products()
                         -> proposals table, status 'draft'
                         -> verify.py   [uniqueness gate and rubric score]

    WHY IT IS ITS OWN FUNCTION:
      The rejected alternative was leaving the loop in the REPL — the operator
      calling get_products_needing_seo(), then call_model(), then
      save_proposal() by hand. That would work for three products and fall
      apart at four hundred, and worse, the commit boundary and the
      two-rows-per-product invariant would live in whatever was typed that
      day rather than in the file. Putting the loop here means the commit
      boundary is one product wide and means both fields are always written
      together.

      The rejected alternative on the commit was a single commit at the end of
      the batch, which is what fetch.py's per-page commit already argues
      against: four hundred products is a run measured in hours, and one
      malformed response an hour in would roll back every product that had
      already succeeded. Per-product commits make the unit of loss one
      product. That only works because the query skips products that already
      have proposals — the two changes are one change.

    WHAT IT RETURNS, AND WHO CONSUMES IT:
      None. The return value is not the point — the proposal rows are.

      The side effect is N committed rows in proposals, at most two per
      product, each with status 'draft'. Fewer than two are written when the
      model omitted a field; that case is printed as a MISSING line and left
      as a hole in the table on purpose, so it shows up in a count rather
      than being papered over.

      The printed failure tally is the other half of that: a run that reports
      failures has left those products untouched for the next run, and a
      failure count that does not fall between runs is a prompt or provider
      problem rather than a flaky one.

      verify.py reads those draft rows next, sets uniqueness_status and
      eval_score on them, and moves them along the §6.2 state machine.
    """
    prompt_text, version_tag = load_prompt(prompt_version)
    products = get_products_needing_seo(conn, limit)

    # Image grounding is driven by the PROMPT VERSION, not by a separate flag.
    # The rejected alternative was a use_images argument the caller sets. That
    # allows the one combination that is silently wrong -- v2's text-is-source
    # rules with photos attached, or v3's look-at-the-photo rules with none --
    # and neither would raise. Tying them together makes the pair impossible to
    # get wrong: asking for v3 IS asking for images.
    use_images = version_tag in ("v3", "v4")

    # Sibling awareness arrived with v4. Sending the taken-titles block to v3
    # would pay for tokens the prompt has no instruction to use.
    use_taken = version_tag == "v4"

    # Keyword grounding arrived with v6. vocab is loaded ONCE here, not inside
    # the loop -- it is the same 60 rows for every product in the run, and
    # keyword_block() renders it as the cached prefix of every message, so
    # re-querying it per product would both waste a query and risk two
    # products in the same run seeing a slightly different list if the table
    # changed mid-run.
    use_keywords = version_tag == "v6"
    vocab = keywords.vocabulary(conn, limit=60) if use_keywords else None

    print(f"{len(products)} products need seo_title — generating with {model_ref}")
    print(f"prompt listing-{version_tag}.md — "
          + (f"image grounding ON, up to {_MAX_IMAGES} photos per product"
             if use_images else "text-only grounding"))

    succeeded = 0
    failed = 0
    refused = 0

    for index, product in enumerate(products):
        # Between products, not before the first one.
        if index and _PRODUCT_PAUSE:
            time.sleep(_PRODUCT_PAUSE)

        try:
            # Re-queried per product, not hoisted out of the loop, so a title
            # written three products ago is already forbidden for this one.
            taken = taken_titles(conn, product["product_type"]) if use_taken else None
            note = owner_note_for(conn, product["gid"]) if use_taken else None
            rank = keywords.page_rank_for(conn, product["handle"]) if use_keywords else None
            message = build_message(product, prompt_text, taken, owner_note=note,
                                    vocab=vocab, page_rank=rank)

            # Only fetch photos when the prompt actually asks the model to look
            # at them. Running v1 or v2 with images attached would pay for the
            # download and the tokens while the prompt still tells the model
            # that the body copy is the source of truth.
            images = fetch_image_parts(product["images"]) if use_images else []
            if use_images and not images:
                # Not an error -- listing-v3.md has a text-only fallback path.
                # Printed because a run where most products land here is not
                # the experiment anyone thinks they are running.
                print(f"  . {product['handle']} — no usable photo, text-only grounding")

            reply = call_model(message, model_ref, images)
            fields = parse_response(reply)

            # A refusal is the prompt working. listing-v3.md tells the model to
            # stop rather than guess when no frame shows the product or the
            # image contradicts the text, and that decision has to be RECORDED.
            #
            # Without a row the product keeps no proposal, so the NOT EXISTS
            # clause in get_products_needing_seo hands it back on the next run,
            # and the same refusal is paid for again on every run forever. The
            # row is what takes it out of the queue and puts it in front of a
            # human, which is where a refusal was always meant to go.
            if fields["needs_human"] and not fields["seo_title"]:
                save_proposal(
                    conn,
                    gid=product["gid"],
                    field="seo_title",
                    current_value=None,
                    # The refusal reason, not copy. status is what stops this
                    # ever being read as a candidate: push.py touches passed
                    # rows only, and this one never passes a gate.
                    proposed_value=fields["needs_human"],
                    model=model_ref,
                    prompt_version=version_tag,
                    grounding=fields.get("grounding"),
                    status="needs_human",
                )
                conn.commit()
                refused += 1
                print(f"  ? {product['handle']} — NEEDS HUMAN: {fields['needs_human'][:70]}")
                continue

            # product_title joins the loop but can never reach the store:
            # push.py refuses it by name. Generated in the SAME call as the
            # other two so all three share one reading of the photograph —
            # generating it separately is how a page ends up with an H1 saying
            # hexagon and a meta title saying geometric.
            for field in ("seo_title", "seo_description", "product_title"):
                proposed = fields[field]
                if not proposed:
                    # Left deliberately unwritten. proposed_value is NOT NULL, and
                    # inserting a placeholder would hide the omission from the
                    # count that verify.py and the reviewer work from.
                    print(f"  ! {product['handle']} — {field} MISSING, no row written")
                    continue

                save_proposal(
                    conn,
                    gid=product["gid"],
                    field=field,
                    # The live value at generation time, so a later diff shows
                    # what actually changed rather than what the store says
                    # today. Both fields now, not description only: since the
                    # queue includes over-long titles, seo_title can be
                    # REPLACING something rather than filling a blank, and a
                    # reviewer cannot judge a replacement without seeing what
                    # it replaces.
                    # product_title's "current" is the live product title, not
                    # a seo_ column — the reviewer is comparing the suggestion
                    # against the heading that is on the page today.
                    current_value=(product["title"] if field == "product_title"
                                   else product[field]),
                    proposed_value=proposed,
                    model=model_ref,
                    prompt_version=version_tag,
                    # Same text on both rows of the pair. One call produced
                    # them, and a reviewer reading one row in a CSV cannot go
                    # and find the other.
                    grounding=fields.get("grounding"),
                )

            conn.commit()  # per product: a later failure cannot cost this one
            succeeded += 1
            print(f"  {product['handle']}: {fields['seo_title']}")

        except Exception as error:
            # Fault isolation, not error handling. Nothing is retried and
            # nothing is slept on — the product keeps no proposal row, so the
            # next run's NOT EXISTS clause returns it and tries it again.
            conn.rollback()  # drop the half-written pair; never leave one field of two
            failed += 1
            print(f"  ! {product['handle']} FAILED — {type(error).__name__}: {error}")

    total = conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0]
    print(f"done — {succeeded} succeeded, {refused} needs_human, {failed} failed, "
          f"{total} rows in proposals")

    if CACHE["total"]:
        share = 100 * CACHE["cached"] / CACHE["total"]
        print(f"prompt cache — {CACHE['cached']:,} of {CACHE['total']:,} "
              f"prompt tokens served from cache ({share:.0f}%)")
        if CACHE["cached"] == 0 and succeeded > 2:
            print("  !! no implicit caching. The prompt prefix is not identical "
                  "across calls;\n     check nothing per-product was moved above "
                  "the prompt text in build_message.")


def load_prompt(version: str) -> tuple[str, str]:
    """
    WHAT IT DOES:
      Reads prompts/listing-{version}.md off disk and hands back its text
      along with the version tag it was loaded from.

      Called by: generate_for_products(), once at the start of a run — not
                 once per product, because the prompt does not change
                 mid-run.

      In the pipeline: prompts/listing-v1.md (file on disk)
                         -> load_prompt()
                         -> build_message()   [the text]
                         -> save_proposal()   [the version tag]

    WHY IT IS ITS OWN FUNCTION:
      The rejected alternative was pasting the prompt directly into
      build_message() as a string literal. That would mean editing Python
      every time the copy rules change, and it would make the prompt
      invisible to git diff as prose. Keeping it as a file loaded by one
      function means the copy rules are versioned as their own artifact,
      and listing-v2.md can be added without touching any code.

    WHAT IT RETURNS, AND WHO CONSUMES IT:
      A tuple (prompt_text, version_tag).

      prompt_text goes to build_message(), which glues it to one product's
      real field values to form the message sent to the model.

      version_tag goes through to save_proposal(), which stores it on every
      proposal row — so when a batch of output turns out to be bad, the
      prompt that produced it is a query, not a guess.
    """
    # A version like "v6" names listing-v6.md. A version that already carries
    # its own family — "collection-v1" — names collection-v1.md directly.
    # Collections are not listings and forcing them under one prefix would
    # make the filename lie about what the file is.
    stem = version if "-" in version else f"listing-{version}"
    path = PROMPT_DIR / f"{stem}.md"
    return path.read_text(encoding="utf-8"), version


def get_products_needing_seo(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    """
    WHAT IT DOES:
      Selects products whose seo_title needs work and which have no row in
      proposals yet. "Needs work" is two conditions, not one:

        - EMPTY: NULL on a product that never had a title, or empty string
          where the field exists and holds nothing.
        - TOO LONG: over _MAX_TITLE_CHARS. Google truncates near 60, so a
          90-character title is not a title anyone reads in full.

      The second condition was added 2026-08-20 after a count made the gap
      visible: 76 in-scope products carried a title over 60 characters and
      were in NO queue, because "not empty" had been standing in for "done".
      A filled field and a working field are different things, and the query
      is the only place that distinction can be enforced.

      The column list is the grounding contract with one deliberate exception.
      seo_title is selected but is NOT a grounding input: it is empty on every
      row here by definition of the WHERE clause, and it is returned so a
      reviewer can be SHOWN that rather than told it. build_message() decides
      what actually reaches the model, and it does not pass this column — the
      two lists are allowed to differ, and this is the one place they do.

      The NOT EXISTS clause is what makes a run resumable. Without it every
      run starts at the top of the catalog and regenerates the same first few
      products, so a limit of 10 run forty times produces forty proposals for
      ten products rather than proposals for four hundred. With it, each run
      walks forward from wherever the last one stopped, and a product that
      failed mid-run comes back because failure left it with no rows.

      Orders by gid so that walk is deterministic: the same limit returns the
      same next slice on a repeat run, which is what makes a three-product
      test worth anything before a four-hundred-product one.

      Skips anything listed in seo_exclusions. Today that is the 81-product
      Invisibles / No Show line, which the owner decided on 2026-08-20 to
      discontinue — 51 of them were sitting in this queue, and copy generated
      for a listing about to be deleted is a paid call spent on nothing.

      The exclusion lives in a table rather than in this WHERE clause on
      purpose. A condition here would work until the second reason to exclude
      something arrives, after which the queue definition slowly becomes a list
      of special cases and "why has this product never been proposed" stops
      being answerable without reading SQL. A row carries its own reason.

      Called by: generate_for_products(), once per run.

      In the pipeline: products table (written by fetch.py)
                         -> get_products_needing_seo()
                         -> build_message()   [one row at a time]

    WHY IT IS ITS OWN FUNCTION:
      The rejected alternative was an inline SELECT inside the orchestrator's
      loop header. Three things go wrong there. First, the column list is the
      grounding contract — it is the complete set of facts the model may use,
      and that decision deserves to be visible in one place rather than buried
      in a loop. Second, the work-queue rule is going to change: §6.3 defines
      a priority_score that prioritise.py will compute, and when the ordering
      moves from "by gid" to "by priority_score" it is this function that
      changes and nothing else. Third, "already done" is now part of the
      definition of the queue rather than something the caller remembers, so
      the orchestrator has no offset to track and no state to carry between
      runs — the table is the state.

    WHAT IT RETURNS, AND WHO CONSUMES IT:
      A list of sqlite3.Row, at most `limit` long, each supporting
      row["title"] style access. An empty list means every product already
      has an seo_title — a legitimate result, not an error, and the caller
      simply generates nothing.

      Each row goes to build_message(), which formats its values into the
      message body, and its gid goes on to save_proposal() as the foreign key
      linking the proposal back to the product it describes.

      images rides along as the raw JSON string written by fetch.py, not a
      parsed list. Parsing belongs to whoever chooses which photos to send,
      and that choice is not made here — this function decides WHICH PRODUCTS
      are in the queue, never which facts about one of them are worth using.
      8 rows in today's queue carry NULL here: real products with no photo on
      the store at all. They are returned anyway rather than filtered out,
      because "cannot be grounded on an image" is a decision for the step that
      needs the image, not a reason to pretend the product does not exist.
    """
    rows = conn.execute(
        """
        SELECT
            gid,
            handle,
            title,
            product_type,
            tags,
            seo_description,
            seo_title,
            images,
            material,
            body_html
        FROM products
        WHERE (
                  seo_title IS NULL
               OR seo_title = ''
               OR LENGTH(seo_title) > :max_title
              )
          AND delisted_at IS NULL
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
        ORDER BY gid
        LIMIT :limit
        """,
        {"limit": limit, "max_title": _MAX_TITLE_CHARS},
    ).fetchall()
    return rows


# An explicit count, which is the only reliable signal. "Pack of 1" and
# "1 Pair" are SINGLES and appear all over this catalog's tags — a keyword
# match on "pack of" alone classified 352 of 367 products as multipacks.
_PACK_COUNT = re.compile(
    r"(?:pack of|set of)\s*(\d+)"        # "Pack of 4", "Set of 3"
    r"|(\d+)\s*[-–]?\s*pairs?\b"         # "6 Pair", "3-Pair"
    r"|(\d+)\s*[-–]\s*pack\b",           # "6-Pack" — hyphen required, so that
    re.IGNORECASE,                       # "Pack of 1" cannot match here too
)

# Phrases that mean several pairs without stating how many. Deliberately short.
# "Collection", "series", "edit" and "mix" were tried and dropped: this store
# uses them for single products too ("The Geometrics Collection"), and a
# classifier that is wrong in the safe direction still writes a pack count onto
# a product that has none, which is an invented fact on a live listing.
_MULTIPACK_WORDS = re.compile(
    r"\bcombo\b|gift box|gift set|mystery box|\bbundle\b|multipack|multi-pack",
    re.IGNORECASE,
)


_DRESS = re.compile(
    r"\bdress\b|\bexecutive\b|\bformal\b|\bboardroom\b|\boffice\b",
    re.IGNORECASE,
)


def is_dress(product: sqlite3.Row) -> bool:
    """
    WHAT IT DOES:
      Decides whether a listing belongs to the dress/formal segment, from the
      product title and tags.

      Called by: build_message(), once per product, before is_multipack().

    WHY IT IS ITS OWN FUNCTION:
      The dress segment sells to a different search than the rest of the
      catalog: material is a headline feature, gender is stated, and the word
      "dress" itself is the term. One prompt path cannot serve both — a casual
      sock led by material reads like a spec sheet, and a dress sock led by
      colour misses the query entirely.

      Kept out of the model's hands for the same reason is_multipack() is: a
      photograph cannot tell a dress sock from a crew sock, so the model would
      be guessing at the fact that decides which rules apply.

    WHAT IT RETURNS, AND WHO CONSUMES IT:
      True or False. False is the safe default — casual copy on a dress sock
      understates it, while dress copy on a novelty sock states an occasion the
      product does not serve.

      build_message() uses it to choose PRODUCT KIND: dress outranks multipack,
      because a six-pair dress gift box is still sold to the dress search.
    """
    haystack = f"{product['title']} {product['tags'] or ''}"
    return bool(_DRESS.search(haystack))


def is_multipack(product: sqlite3.Row) -> bool:
    """
    WHAT IT DOES:
      Decides whether a listing sells one pair or several, by matching the
      product title and tags against the phrases this catalog actually uses —
      "Pack of 4", "Combo", "Gift Box", "6 Pair", "Mystery Box".

      Called by: build_message(), once per product.

      In the pipeline: one products row
                         -> is_multipack()
                         -> build_message()   [as the PRODUCT KIND line]
                         -> the model, which follows path A or path B

    WHY IT IS ITS OWN FUNCTION:
      The rejected alternative was letting the model classify the product from
      the photographs and pick its own path. Two things go wrong there. A pair
      photographed from six angles looks exactly like a six-pack, so the model
      would be guessing at the one fact that decides which set of rules apply.
      And a model asked to both classify and write has an incentive to pick
      whichever path makes the writing easier, with no record that it chose.

      Keeping it in code means the decision is inspectable, testable without an
      API key, and correctable by editing one regular expression rather than
      re-running 85 products to see whether a prompt tweak took.

    WHAT IT RETURNS, AND WHO CONSUMES IT:
      True or False. Never None — an unmatched product is a single pair, which
      is the safe default: single-pair copy on a multipack understates the
      product, while multipack copy on a single pair states a pack count that
      does not exist, and that is an invented fact on a live store.

      build_message() turns it into the PRODUCT KIND line, and listing-v4.md
      branches on that line. Nothing else reads it.
    """
    haystack = f"{product['title']} {product['tags'] or ''}"

    # An explicit count decides it outright, in both directions. "Pack of 1"
    # is a single and must be read as one, not merely fail to match.
    counts = [
        int(found)
        for groups in _PACK_COUNT.findall(haystack)
        if (found := next((g for g in groups if g), "")).isdigit()
    ]
    if counts:
        return max(counts) > 1

    return bool(_MULTIPACK_WORDS.search(haystack))


def taken_titles(conn: sqlite3.Connection, product_type: str, limit: int = 40) -> list[str]:
    """
    WHAT IT DOES:
      Returns titles already proposed for other products of the same type, so
      the prompt can forbid reusing them.

      Called by: generate_for_products(), once per product — deliberately not
                 once per run, because each product must see the titles written
                 for the products generated before it in the same run.

      In the pipeline: proposals table
                         -> taken_titles()
                         -> build_message()   [the TITLES ALREADY TAKEN block]

    WHY IT IS ITS OWN FUNCTION:
      This is the fix for the fault that a 268-product run exposed: every
      product was generated in isolation, so five different geometric socks
      each independently chose "Geometric Print Crew Socks" and ended up
      competing with each other for one phrase.

      The rejected alternative was catching duplicates afterwards in verify.py
      and regenerating the losers. That works, but it pays for every duplicate
      twice and it arrives after the reviewer has already read the batch. A
      title the model can see is taken is a title it will not write.

      Scoped to product_type rather than the whole table because that is where
      collisions actually happen — an ankle sock and a crew sock may share a
      motif without competing, since the searches differ. It also keeps the
      block small enough not to dominate the prompt.

    WHAT IT RETURNS, AND WHO CONSUMES IT:
      A list of title strings, newest first, at most `limit` long. Empty on the
      first product of a fresh run, which is correct — nothing is taken yet.

      The cap exists because this text is paid for on every call. Forty titles
      is enough to cover the near neighbours; the whole catalog would cost more
      in tokens than the duplicates cost in rank.
    """
    rows = conn.execute(
        """
        SELECT DISTINCT pr.proposed_value AS title
        FROM proposals pr
        JOIN products p ON p.gid = pr.gid
        WHERE pr.field = 'seo_title'
          AND pr.status = 'draft'
          AND pr.superseded_by IS NULL
          AND p.product_type IS :product_type
        ORDER BY pr.id DESC
        LIMIT :limit
        """,
        {"product_type": product_type, "limit": limit},
    ).fetchall()
    return [row["title"] for row in rows if row["title"]]


def owner_note_for(conn: sqlite3.Connection, gid: str) -> str | None:
    """
    WHAT IT DOES:
      Returns the most recent note the owner wrote about this product, or None.

      Called by: resolve.py and regenerate.py, once per product, before the
                 message is built.

      In the pipeline: proposals.reviewer_note  [written by resolve.py]
                         -> owner_note_for()
                         -> build_message()   [as the OWNER NOTE block]

    WHY IT IS ITS OWN FUNCTION:
      The rejected alternative was passing the note down from whichever script
      collected it. That works once and loses it forever after: the note was
      typed to settle a conflict, and the conflict is still there next time the
      product is regenerated. Reading it back from the table means a fact the
      owner stated once keeps applying, which is the entire point of writing it
      down rather than answering a prompt.

      It reads from proposals rather than a table of its own because the note
      IS part of the proposal record — DESIGN-v2 §6.1 already gives proposals a
      reviewer_note column for exactly this, and an eighth table for one string
      per product would be schema for its own sake.

    WHAT IT RETURNS, AND WHO CONSUMES IT:
      The note text, or None when the owner has never commented on this
      product. None is the normal case and means "no override" — the model
      falls back to the photograph and the stored text as usual.

      build_message() renders it as the highest-authority block in the message,
      above the photographs and above the stored fields.
    """
    row = conn.execute(
        """
        SELECT reviewer_note FROM proposals
        WHERE gid = :gid AND reviewer_note IS NOT NULL AND reviewer_note <> ''
        ORDER BY id DESC LIMIT 1
        """,
        {"gid": gid},
    ).fetchone()
    return row["reviewer_note"] if row else None


def keyword_block(vocab) -> str:
    """
    WHAT IT DOES:
      Renders the real search demand into the message: the phrases people
      actually type, strongest evidence first, and what Search Console says
      about THIS product's own URL.

      Called by: build_message(), once per product.

    WHY IT IS ITS OWN FUNCTION:
      The trust hierarchy has to be visible in the text, not just in the sort
      order. A Google query with 739 impressions and an autocomplete phrase
      with none are different KINDS of evidence, and a flat list would let the
      model treat them alike. Rendering is where that distinction is either
      preserved or lost, so it gets one function.

      The page-rank line is the other half. A product already sitting at
      position 14 with impressions and no clicks is not a blank page needing
      copy — it is a title failing at the last step, and saying so changes what
      the model should write.

    WHAT IT RETURNS, AND WHO CONSUMES IT:
      A markdown block, or "" when no keyword data is loaded. Empty is correct
      and safe: the prompt still works, it just writes from the product alone,
      which is exactly what v5 did.
    """
    if not vocab:
        return ""

    with_volume = [r for r in vocab if r["impressions"]]
    no_volume = [r for r in vocab if not r["impressions"]]

    lines = [
        "\n## WHAT PEOPLE ACTUALLY SEARCH",
        "Real demand for this store. Use these words where they are TRUE for",
        "this product. Grounding still outranks volume — never claim something",
        "the product is not, however attractive the number.",
        "",
    ]

    if with_volume:
        lines.append("Measured demand (impressions in the last 3 months):")
        for r in with_volume:
            lines.append(f"  {r['impressions']:>6}  {r['query']}")

    if no_volume:
        lines += [
            "",
            "Phrases people type, volume unknown — weaker evidence, use only",
            "when the measured list has nothing that fits:",
        ]
        for r in no_volume:
            lines.append(f"          {r['query']}")

    return "\n".join(lines) + "\n"


def page_rank_block(page_rank) -> str:
    """
    What Search Console says about THIS product's URL. Per-product, so it is
    deliberately NOT part of the cached prefix.
    """
    if not page_rank or not page_rank["impressions"]:
        return ""
    return (
        "\n## THIS PRODUCT'S PAGE IS ALREADY RANKING\n"
        f"  {page_rank['impressions']} impressions, {page_rank['clicks']} clicks, "
        f"average position {page_rank['position']:.1f}\n"
        "\n"
        "It appears in results and is not being clicked. The page is not\n"
        "invisible — the title is failing to earn the click. Write a title a\n"
        "shopper would choose over the nine results above it.\n"
    )


def build_message(product: sqlite3.Row, prompt_text: str,
                  taken: list[str] | None = None,
                  owner_note: str | None = None,
                  vocab=None, page_rank=None) -> str:
    """
    WHAT IT DOES:
      Joins the copy rules to one product's real field values under a clear
      label, producing the single string sent to the model. Tags are stored as
      a JSON array string and are rendered here as a plain comma list, because
      that is what the rest of the message looks like; if the column does not
      parse as JSON the raw text is passed through rather than dropped.
      Nothing is added that is not in the row — no category assumptions, no
      product knowledge, no examples beyond the ones already in the prompt
      file.

      Called by: generate_for_products(), once per product.

      In the pipeline: load_prompt() + one products row
                         -> build_message()
                         -> call_model()   [the message string]

    WHY IT IS ITS OWN FUNCTION:
      The rejected alternative was f-stringing the product fields together at
      the call site inside the loop. The grounding rule — the model sees these
      facts and no others — is the single most important property of this
      stage, and inlining it would spread that rule across the orchestrator
      where an extra field could be added to the string without anyone
      noticing it happened. One function means the model's entire view of the
      world is one function body long and can be read in ten seconds.

    WHAT IT RETURNS, AND WHO CONSUMES IT:
      One string: the full prompt file text, then a PRODUCT block of labelled
      values. Never empty — a product with every optional column NULL still
      produces the labels, with "(none)" as the value, so the model is told a
      field is absent rather than being left to infer it.

      It goes to call_model(), which is the last function that sees it before
      it leaves the machine.
    """
    try:
        tags = ", ".join(json.loads(product["tags"] or "[]"))
    except (json.JSONDecodeError, TypeError):
        tags = product["tags"]  # not valid JSON — pass the stored text through untouched

    # Decided in code and stated as a fact, not asked as a question. See
    # is_multipack() and is_dress() for why the model is not allowed to
    # classify this itself.
    #
    # dress is checked FIRST. A 6-pair dress gift box is sold to the dress
    # search, not the gift search — the buyer wants dress socks and the pack
    # count is a detail, where on a novelty combo it is the headline.
    if is_dress(product):
        kind = "dress"
    elif is_multipack(product):
        kind = "multipack"
    else:
        kind = "single"

    taken_block = ""
    if taken:
        listed = "\n".join(f"- {title}" for title in taken)
        taken_block = (
            "\n## TITLES ALREADY TAKEN\n"
            "Other products of this type already use these. Yours must differ,\n"
            "and a near-match is not a difference.\n"
            "\n"
            f"{listed}\n"
        )

    note_block = ""
    if owner_note:
        note_block = (
            "\n## OWNER NOTE — TREAT AS FACT\n"
            "The store owner has looked at this product and stated the\n"
            "following. It outranks the photographs and every stored field.\n"
            "Do not question it, and do not return needs_human on a point it\n"
            "has already settled.\n"
            "\n"
            f"{owner_note}\n"
        )

    # The on-page copy, tags stripped. Passed IN FULL and never truncated:
    # this is where sizing, fit and care live, and "stay-put fit" — the phrase
    # that answers the searched pain point "socks that stay up" — appears
    # nowhere else. A cut-off body is how a true claim goes missing.
    body = ""
    if product["body_html"]:
        text = re.sub(r"<[^>]+>", " ", product["body_html"])
        text = re.sub(r"\s+", " ", text).strip()
        body = f"body description: {text}\n"

    return (
        # ── CACHED PREFIX ────────────────────────────────────────────────
        # Byte-identical on every call in a run, so Gemini's implicit cache
        # covers it. The keyword vocabulary belongs here, not after the
        # product: it is the same for all 325 products, and appending it
        # later would both break the cache and bury it below the facts.
        f"{prompt_text}\n"
        f"{keyword_block(vocab)}"
        # ── PER-PRODUCT, nothing above this line may vary ────────────────
        f"{note_block}"
        "\n"
        f"## PRODUCT KIND: {kind}\n"
        "\n"
        "## PRODUCT\n"
        "These are the only facts about this product. Use nothing else.\n"
        "\n"
        f"title: {product['title']}\n"
        f"handle: {product['handle']}\n"
        f"product_type: {product['product_type'] or '(none)'}\n"
        f"tags: {tags or '(none)'}\n"
        f"material composition: {product['material'] or '(NOT STATED - do not mention any material)'}\n"
        f"current seo.description: {product['seo_description'] or '(none)'}\n"
        f"{body}"
        f"{taken_block}"
        f"{page_rank_block(page_rank)}"
    )


def _resized(url: str) -> str:
    """
    Ask Shopify's CDN for a _IMAGE_WIDTH-wide copy instead of the original.

    Appends width= as an extra query parameter, keeping the ?v= cache-busting
    value already on the URL — dropping that would serve a stale image after
    the owner replaces a photo.

    A URL that is not a Shopify CDN link gets the parameter too and simply
    ignores it. That is deliberate: silently branching on the hostname would
    mean an image from somewhere else is quietly sent at full size, and the
    token cost of that is exactly what this function exists to control.
    """
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}width={_IMAGE_WIDTH}"


def _quota_reason(response) -> str:
    """
    Pull the human-readable reason out of a 429 or 5xx body.

    Google names the exact quota that tripped — requests per minute, tokens per
    minute, requests per day — and which one it is decides what to do about it.
    Fewer images fixes a token limit; only waiting fixes a daily limit. Without
    this the operator sees "429" and cannot tell those apart.

    Falls back to a truncated raw body, then to the status code, because a
    parser failure here must not mask the error it is trying to explain.
    """
    try:
        error = response.json().get("error", {})
        message = error.get("message") or ""
        details = error.get("details") or []
        quotas = [
            d.get("violations", [{}])[0].get("quotaId", "")
            for d in details
            if d.get("@type", "").endswith("QuotaFailure")
        ]
        quota = next((q for q in quotas if q), "")
        return f"{quota or 'quota'}: {message[:160]}" if message else quota or "no detail"
    except Exception:
        return (response.text or "")[:160] or f"HTTP {response.status_code}"


def _retry_after(response, attempt: int) -> int:
    """
    Seconds to wait: the provider's own number when it gives one, else backoff.

    Guessing shorter than Google asked for is how a retry storm turns one 429
    into four. Capped at 60 so a bad header cannot stall a run indefinitely.
    """
    for source in (response.headers.get("Retry-After"),
                   _quota_delay(response)):
        try:
            if source:
                return min(int(float(source)), 60)
        except (TypeError, ValueError):
            pass
    return 2 ** attempt


def _quota_delay(response):
    """RetryInfo.retryDelay from the error body, e.g. '38s'. None when absent."""
    try:
        for detail in response.json().get("error", {}).get("details", []):
            if detail.get("@type", "").endswith("RetryInfo"):
                return detail.get("retryDelay", "").rstrip("s")
    except Exception:
        return None
    return None


def fetch_image_parts(images_json: str) -> list[dict]:
    """
    WHAT IT DOES:
      Reads the products.images JSON written by fetch.py, downloads the first
      _MAX_IMAGES photos in store order, and returns them as Gemini
      inline_data parts — base64 bytes plus a mime type.

      No filtering. An earlier version preferred images carrying alt text, on
      the theory that a blank alt marks a promo graphic. Withdrawn: alt text
      presence measures whether somebody bothered to write it, not whether the
      frame shows the product, and roughly 90 products have none at all — so
      the rule silently skipped real photography. Junk frames are handled in
      listing-v3.md, which tells the model to ignore banners and size charts.

      A download that fails is skipped, not raised. A dead CDN link costs one
      photo; raising would cost the whole product for a reason that has nothing
      to do with the model.

      Called by: generate_for_products(), once per product.

      In the pipeline: products.images  [JSON written by fetch.py]
                         -> fetch_image_parts()
                         -> call_model()      [as the images argument]
                         -> _call_gemini()    [appended to the request parts]

    WHY IT IS ITS OWN FUNCTION:
      The rejected alternative was doing the download inside _call_gemini,
      next to the request it feeds. That would put a Shopify CDN fetch inside
      the function whose entire job is talking to Google, and the two fail for
      unrelated reasons — a 404 on an image is a catalog problem, a 503 on
      generateContent is a capacity problem, and they need different handling.
      Keeping them apart also means the retry logic in _call_gemini never
      re-downloads megabytes of images it already has.

    WHAT IT RETURNS, AND WHO CONSUMES IT:
      A list of inline_data dicts, possibly empty — empty when the product has
      no photographs, or when every download failed. An empty list is not an
      error: call_model passes it through and the model falls back to
      text-only grounding, which is exactly what listing-v3.md instructs when
      no usable photograph exists.

      It goes to call_model(), which hands it to _call_gemini() to be appended
      after the text part in the request body.
    """
    images = json.loads(images_json) if images_json else []
    parts = []

    for image in images[:_MAX_IMAGES]:
        url = _resized(image["url"])
        try:
            response = requests.get(url, timeout=_IMAGE_TIMEOUT)
            response.raise_for_status()
        except requests.RequestException as error:
            print(f"    .. image skipped ({type(error).__name__}): {url[:60]}")
            continue

        if len(response.content) > _MAX_IMAGE_BYTES:
            print(f"    .. image skipped ({len(response.content) // 1000}KB, over cap)")
            continue

        parts.append(
            {
                "inline_data": {
                    "mime_type": ("image/png"
                                  if url.split("?")[0].lower().endswith(".png")
                                  else "image/jpeg"),
                    "data": base64.b64encode(response.content).decode("ascii"),
                }
            }
        )

    return parts


def call_model(message: str, model_ref: str, images: list[dict] | None = None) -> str:
    """
    WHAT IT DOES:
      Reads "provider:model_id" and routes the call to the matching provider
      function. Splits on the FIRST colon only, because model ids themselves
      contain slashes and sometimes colons — "nvidia:meta/llama-3.3-70b-instruct"
      must arrive at the provider as "meta/llama-3.3-70b-instruct", intact.
      Checks the provider's key is present before dispatching, and raises on
      an unknown provider or a missing key. It never substitutes a different
      provider, and it never retries.

      Called by: generate_for_products(), once per product.

      In the pipeline: build_message()
                         -> call_model()
                         -> _call_gemini() / _call_openai_compatible()
                         -> parse_response()   [the raw reply text]

    WHY IT IS ITS OWN FUNCTION:
      The rejected alternative was calling _call_gemini() directly from the
      orchestrator and adding an if-statement there later when a second
      provider arrived. That fails on the thing this project cares about:
      comparing providers on the same catalog. With the choice living in one
      function driven by one string in .env, switching from Gemini to NVIDIA
      is an .env edit and the proposal rows record which model wrote which
      copy. With the choice inlined it is a code edit, and code edits made to
      run an experiment are how a fallback gets quietly added.

      The silent-fallback ban is enforced here rather than in the provider
      functions on purpose: this is the only place that knows a provider was
      requested but is unavailable, so it is the only place that could be
      tempted to pick another one.

    WHAT IT RETURNS, AND WHO CONSUMES IT:
      The raw text the model produced, exactly as it came back — unparsed and
      untrimmed. Never None: a provider that returns nothing usable raises
      instead, because an empty string here would travel downstream as two
      missing fields and read as a model that declined rather than a call that
      broke.

      It goes to parse_response(), which pulls seo_title and seo_description
      out of it.
    """
    provider, separator, model_id = model_ref.partition(":")  # partition splits once, on the first colon
    if not separator or not model_id:
        raise ValueError(
            f"SEO_MODEL must look like 'provider:model_id', got {model_ref!r}"
        )

    if provider == "gemini":
        return _call_gemini(message, model_id, images)

    # Images are dropped rather than silently reformatted for providers that
    # have not had their multimodal request shape written and tested. Saying so
    # is the point: a run that quietly fell back to text-only would produce
    # proposals indistinguishable from image-grounded ones in the database.
    if images:
        print(f"    !! {provider} is text-only here — {len(images)} image(s) NOT sent")

    if provider == "nvidia":
        return _call_openai_compatible(
            message, model_id, _NVIDIA_BASE_URL, _require_key("NVIDIA_API_KEY", provider)
        )

    if provider == "openai":
        return _call_openai_compatible(
            message, model_id, _OPENAI_BASE_URL, _require_key("OPENAI_API_KEY", provider)
        )

    raise ValueError(
        f"unknown provider {provider!r} in SEO_MODEL={model_ref!r} — "
        "known providers are: gemini, nvidia, openai"
    )


def _require_key(env_var: str, provider: str) -> str:
    """
    WHAT IT DOES:
      Reads one API key out of the environment and raises a message naming
      both the missing variable and the provider that wanted it, instead of
      the KeyError or the provider's own 401 that would otherwise surface.

      Called by: call_model(), once per call, for whichever provider was
                 selected — never for the others, so a Gemini run does not
                 require an NVIDIA key to exist.

      In the pipeline: .env -> _require_key() -> the provider function's
                       api_key argument.

    WHY IT IS ITS OWN FUNCTION:
      It is three lines and it is called from three branches of call_model().
      The rejected alternative was os.environ["GEMINI_API_KEY"] at each
      branch: that raises KeyError('GEMINI_API_KEY'), which does not say what
      was being attempted, and it invites the .get() form whose None sails
      into the request and comes back as an opaque 401 from the provider.

      Reading keys here rather than at module import — the style fetch.py uses
      — is deliberate: importing this module must not require every provider's
      key to be set, only the one actually being used.

    WHAT IT RETURNS, AND WHO CONSUMES IT:
      The key string. Never returns None or "" — both raise, because an empty
      key produces a 401 that looks like a revoked credential rather than an
      unset one.

      It goes straight into the provider function's Authorization header.
    """
    key = os.environ.get(env_var, "").strip()
    if not key:
        raise RuntimeError(
            f"{env_var} is not set in .env — required for provider {provider!r}. "
            "Refusing to fall back to another provider."
        )
    return key


def _call_gemini(message: str, model_id: str, images: list[dict] | None = None) -> str:
    """
    WHAT IT DOES:
      POSTs the message to Google's generateContent endpoint for the given
      model id, with GEMINI_API_KEY in the x-goog-api-key header, and digs the
      generated text out of the response. Raises on a non-2xx status, and
      raises separately when the response is well formed but carries no text —
      a stop for safety filters or a truncation both produce a 200 with no
      usable part, and the finishReason is named in the error so the two can
      be told apart.

      Called by: call_model(), once per product, when SEO_MODEL names gemini.

      In the pipeline: call_model()
                         -> _call_gemini()  [the only network call in this file]
                         -> parse_response()

    WHY IT IS ITS OWN FUNCTION:
      Private and separate from call_model() because it is the half that
      changes for reasons that have nothing to do with the other half. The
      rejected alternative was one function holding both the routing and the
      HTTP: Google's request shape, its header name and its response nesting
      are all specific to Google and none of them are stable, while the
      routing rule — split once on the colon, never fall back — is a project
      rule that should not be edited when a vendor moves a JSON key.

    WHAT IT RETURNS, AND WHO CONSUMES IT:
      The text of the first candidate, as a string, with no cleanup applied —
      markdown fences, labels and preamble all still attached. Never None and
      never "": both raise, so that "the call failed" and "the model omitted a
      field" stay separable further down.

      It goes to parse_response(), which is the function that knows how to
      find seo_title and seo_description inside it.
    """
    # Retry loop. Gemini returns 429 and 503 under load, and on a 244-product
    # run those are common enough that without this a batch loses products to
    # capacity rather than to anything wrong with the request. 4xx other than
    # 429 is NOT retried: a malformed or unauthorised request fails identically
    # every time, and repeating it only spends money making the same mistake.
    for attempt in range(1, _ATTEMPTS + 1):
        try:
            response = requests.post(
                _GEMINI_ENDPOINT.format(model_id=model_id),
                headers={
                    "Content-Type": "application/json",
                    "x-goog-api-key": _require_key("GEMINI_API_KEY", "gemini"),
                },
                # Text part first, images after. listing-v3.md tells the model
                # to look at the photographs before the text, but the ORDER OF
                # THE PARTS is not what carries that instruction -- the prompt
                # is. Text first keeps the request readable when logged.
                json={"contents": [{"parts": [{"text": message}] + (images or [])}]},
                timeout=_IMAGE_TIMEOUT if images else _TIMEOUT,
            )
        except requests.RequestException:
            if attempt == _ATTEMPTS:
                raise
            time.sleep(2 ** attempt)
            continue

        if response.status_code in _RETRY_STATUS and attempt < _ATTEMPTS:
            # Google states WHICH quota was exceeded in the response body and
            # often how long to wait. Swallowing that turns a solvable "you are
            # over tokens-per-minute" into an opaque 429, so it is printed and
            # obeyed rather than backed off blindly.
            wait = _retry_after(response, attempt)
            print(f"    .. HTTP {response.status_code} — {_quota_reason(response)}")
            print(f"       waiting {wait}s, retry {attempt}/{_ATTEMPTS - 1}")
            time.sleep(wait)
            continue

        break

    response.raise_for_status()  # Gemini signals failure with the HTTP status, unlike Shopify GraphQL
    body = response.json()

    # Gemini 2.5 caches a repeated prompt PREFIX automatically — no cache_control
    # to set, but also no guarantee. It only fires when the leading tokens are
    # byte-identical across calls, which is why build_message puts the prompt
    # file first and every per-product fact after it. Read the counter back
    # rather than assuming: a prefix broken by a stray timestamp would cost full
    # price on all 325 products and look exactly the same from here.
    usage = body.get("usageMetadata", {})
    CACHE["cached"] += usage.get("cachedContentTokenCount", 0)
    CACHE["total"] += usage.get("promptTokenCount", 0)

    candidates = body.get("candidates") or []
    if not candidates:
        # Blocked before generation started; promptFeedback carries the reason.
        raise RuntimeError(f"gemini returned no candidates: {body.get('promptFeedback')}")

    parts = candidates[0].get("content", {}).get("parts") or []
    text = "".join(part.get("text", "") for part in parts)
    if not text.strip():
        raise RuntimeError(
            f"gemini returned an empty candidate, finishReason="
            f"{candidates[0].get('finishReason')!r}"
        )
    return text


def _call_openai_compatible(
    message: str,
    model_id: str,
    base_url: str,
    api_key: str,
) -> str:
    """
    WHAT IT DOES:
      Not implemented in this pass — raises NotImplementedError.

      When filled in it will POST to {base_url}/chat/completions with a Bearer
      token, a messages list holding one user message, and read the reply from
      choices[0].message.content — the request and response shape both NVIDIA
      NIM and OpenAI accept unchanged.

      Called by: call_model(), once per product, for provider 'nvidia' or
                 'openai'. Both routes land here; they differ only in the
                 base_url and api_key passed in.

      In the pipeline: call_model()
                         -> _call_openai_compatible()
                         -> parse_response()

    WHY IT IS ITS OWN FUNCTION:
      It is one function rather than two — _call_nvidia() and _call_openai()
      was the rejected alternative — because the difference between those two
      providers is entirely a hostname and a key, and two functions would mean
      two copies of the same parsing that then drift when one gets a fix. Both
      are parameters, so both are arguments.

      It is separate from _call_gemini() for the opposite reason: Gemini's
      shape genuinely differs (x-goog-api-key rather than Bearer, contents
      rather than messages, candidates rather than choices), so folding them
      together would produce a function that is mostly branching on which
      vendor it is talking to.

      It exists now, empty, so that call_model()'s routing table is complete
      and testable today: asking for an unimplemented provider raises
      NotImplementedError, which is a different and more honest failure than
      "unknown provider" for something that is merely unfinished.

    WHAT IT RETURNS, AND WHO CONSUMES IT:
      Nothing yet. Once implemented: the assistant message content as a
      string, matching _call_gemini()'s contract exactly — raw text, never
      None, never empty, raising rather than returning "" — because
      parse_response() consumes both without knowing which provider it came
      from, and that only holds if the two return the same thing.
    """
    raise NotImplementedError(
        "_call_openai_compatible is not implemented yet — "
        "only provider 'gemini' works in this pass"
    )


def parse_response(text: str) -> dict:
    """
    WHAT IT DOES:
      Scans the reply for lines labelled seo_title and seo_description and
      pulls the value off each, tolerating the decoration models put around
      labels — markdown bold, headings, list bullets, JSON quoting. When the
      label was a heading with nothing after it, the following non-empty line
      is taken as the value. Surrounding quotes, asterisks, backticks and a
      trailing comma are stripped from what it finds. First match wins.

      A field that is absent, or present but empty, comes back as None. It is
      never guessed at, never defaulted, and never filled from the product
      title — that substitution is the defect this whole project exists to
      remove, and it would be invisible in the output if it happened here.

      Called by: generate_for_products(), once per product.

      In the pipeline: call_model()  [raw model text]
                         -> parse_response()
                         -> save_proposal()   [one call per non-None field]

    WHY IT IS ITS OWN FUNCTION:
      The rejected alternative was parsing inline in the orchestrator. Model
      output shape is the part of this pipeline most likely to break — a new
      model version starts wrapping its answer in a code fence, or in JSON,
      and everything downstream sees two missing fields. Isolated here, that
      is one function to fix and one function to test against a saved reply,
      rather than surgery on a loop that also owns the database transaction.

      Known limitation, left in on purpose: if a model echoes the prompt's own
      "## seo.title" headings back before answering, the rule text underneath
      is what gets captured. That produces visibly wrong copy that verify.py
      and the reviewer will catch, which is preferable to adding heuristics
      here that guess which occurrence was meant.

    WHAT IT RETURNS, AND WHO CONSUMES IT:
      A dict with exactly two keys, always present: {"seo_title": str|None,
      "seo_description": str|None}. Both keys exist even when both values are
      None, so the caller can loop over the pair without checking membership.
      None means "the model did not give us this" — a fact, and one the caller
      prints rather than repairs.

      generate_for_products() reads both keys; each non-None value becomes one
      proposals row via save_proposal(), and each None becomes a printed
      MISSING line and no row at all.
    """
    fields = {"seo_title": None, "seo_description": None,
              "product_title": None, "grounding": None, "needs_human": None}
    if not text:
        return fields

    lines = text.splitlines()

    # Both are single-line and neither has a heading-then-value form to handle,
    # so they are read in one pass before the field loop rather than threaded
    # through its continue statements.
    for line in lines:
        if fields["grounding"] is None:
            match = _GROUNDING_LINE.match(line)
            if match:
                fields["grounding"] = match.group(1).strip(_DECORATION) or None
                continue
        if fields["needs_human"] is None:
            match = _NEEDS_HUMAN_LINE.match(line)
            if match:
                fields["needs_human"] = match.group(1).strip(_DECORATION) or None
                continue
        if fields["product_title"] is None:
            match = _PRODUCT_TITLE_LINE.match(line)
            if match:
                fields["product_title"] = match.group(1).strip(_DECORATION) or None

    for index, line in enumerate(lines):
        match = _FIELD_LINE.match(line)
        if not match:
            continue

        key = "seo_" + match.group(1).lower()
        if key not in fields:
            continue
        if fields[key] is not None:
            continue  # first match wins; a later repeat is an echo, not a correction

        value = match.group(2).strip(_DECORATION)
        if not value:
            # Heading form: the label was on its own line, the copy is below it.
            for following in lines[index + 1:]:
                if not following.strip():
                    continue
                if _FIELD_LINE.match(following):
                    break  # ran straight into the next label — this field is empty
                value = following.strip(_DECORATION)
                break

        fields[key] = value or None  # empty after stripping is an omission, not a value

    return fields


def save_proposal(
    conn: sqlite3.Connection,
    gid: str,
    field: str,
    current_value: str | None,
    proposed_value: str,
    model: str,
    prompt_version: str,
    grounding: str | None = None,
    status: str = "draft",
    reviewer_note: str | None = None,
) -> None:
    """
    WHAT IT DOES:
      Appends one row to proposals: the product it belongs to, which field it
      is for, the live value snapshotted at generation time, the candidate
      text, and the model and prompt version that produced it. Stamps
      created_at as ISO-8601 UTC and status as 'draft'. Does not commit — the
      caller commits once after the whole batch.

      Called by: generate_for_products(), once per field per product, so twice
                 per product when the model returns both.

      In the pipeline: parse_response()  [one field's text]
                         -> save_proposal()
                         -> proposals table
                         -> verify.py     [reads the draft rows next]

    WHY IT IS ITS OWN FUNCTION:
      The rejected alternative was conn.execute() with the INSERT inline in
      the loop. The append-only rule is the thing being protected: proposals
      is never UPDATEd and never DELETEd, because the table is the audit trail
      that makes a bad batch traceable to the prompt and model that caused it.
      One function holding the only write to that table means the rule is
      enforceable by reading one function, and a stray UPDATE anywhere else in
      the module is obviously out of place.

      It also fixes the two values that must not vary per call site —
      created_at's format and status's initial state — so a row generated in a
      later session sorts and filters alongside the ones generated today.

    WHAT IT RETURNS, AND WHO CONSUMES IT:
      None. The side effect is one row staged in the caller's open
      transaction, not yet durable — nothing is readable by another connection
      until generate_for_products() commits.

      The row's consumer is verify.py, which selects the draft rows, sets
      uniqueness_status, max_similarity and eval_score on them, and advances
      status along the §6.2 state machine. review.py exports them to CSV after
      that, and push.py only ever touches rows that have passed both gates.
    """
    conn.execute(
        _INSERT_PROPOSAL,
        {
            "gid": gid,
            "field": field,
            "current_value": current_value,
            "proposed_value": proposed_value,
            "model": model,  # full provider:model_id — two providers can serve the same id
            "prompt_version": prompt_version,
            "created_at": datetime.datetime.now(datetime.timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            # Defaults to None so v1 and v2 callers are unchanged. A NULL here
            # is a fact worth keeping: nobody knows why that copy was written.
            "grounding": grounding,
            # Defaults to "draft" so every normal call is unchanged. The only
            # other value written here is "needs_human", which the §6.2 state
            # machine treats as terminal until a person intervenes.
            "status": status,
            # The owner's own words, carried onto the row they produced. Kept
            # on every row so owner_note_for() can find it again on the next
            # regeneration without knowing which run wrote it.
            "reviewer_note": reviewer_note,
        },
    )


def generate_for_collections(
    conn: sqlite3.Connection,
    limit: int,
    model_ref: str,
    prompt_version: str = "collection-v1",
) -> None:
    """
    WHAT IT DOES:
      The collections equivalent of generate_for_products. Reads collections
      with no SEO title and no proposal, builds a message from their MEMBER
      PRODUCT TITLES rather than from a photograph, and appends proposals.

      Called by: the operator, from the REPL, once per run.

      In the pipeline: collections table (filled by fetch.fetch_collections)
                         -> generate_for_collections()
                         -> proposals, status 'draft'
                         -> review.py

    WHY IT IS ITS OWN FUNCTION:
      The rejected alternative was a `kind="collection"` flag threaded through
      generate_for_products. Almost nothing is shared: no photographs, no fibre
      composition, no multipack/dress routing, and the grounding is a list of
      member titles instead of an image. The two loops would have been one
      function with two disjoint halves and an if-statement deciding which half
      ran — which is two functions wearing one name.

      What IS shared is reused directly: call_model, parse_response,
      save_proposal and the keyword vocabulary. The duplication is in the loop,
      not in the logic that matters.

    WHAT IT RETURNS, AND WHO CONSUMES IT:
      None. Side effect: proposal rows whose gid is a COLLECTION gid, not a
      product gid.

      That distinction is deliberate and load-bearing: proposals.gid has a
      foreign key to products(gid), so a collection proposal will fail that
      constraint unless the collection row exists in products — it does not.
      The rows are therefore written with the FK relaxed for this table's
      lifetime, and push.py must learn collectionUpdate before any of them can
      ship. Until then these are review-only, which is the correct state for
      the highest-reach pages on the store.
    """
    prompt_text, version_tag = load_prompt(prompt_version)
    vocab = keywords.vocabulary(conn, limit=40)

    rows = conn.execute(
        """
        SELECT gid, handle, title, body_html, seo_title, seo_description,
               products_count, member_titles
        FROM collections
        WHERE (seo_title IS NULL OR seo_title = '' OR LENGTH(seo_title) > :max)
          AND delisted_at IS NULL
          AND NOT EXISTS (SELECT 1 FROM collection_proposals p
                          WHERE p.gid = collections.gid AND p.superseded_by IS NULL)
        ORDER BY products_count DESC
        LIMIT :limit
        """,
        {"limit": limit, "max": _MAX_TITLE_CHARS},
    ).fetchall()

    print(f"{len(rows)} collections need SEO — ordered by reach, biggest first")

    for index, row in enumerate(rows):
        if index:
            time.sleep(_PRODUCT_PAUSE)

        members = json.loads(row["member_titles"] or "[]")
        message = (
            f"{prompt_text}\n"
            f"{keyword_block(vocab)}"
            "\n## COLLECTION\n"
            "These are the only facts about this page. Use nothing else.\n"
            "\n"
            f"collection name: {row['title']}\n"
            f"url handle: {row['handle']}\n"
            f"products on this page: {row['products_count']}\n"
            f"current seo.title: {row['seo_title'] or '(empty)'}\n"
            f"current seo.description: {row['seo_description'] or '(empty)'}\n"
            "\n"
            f"member products ({len(members)} of {row['products_count']} sampled) —\n"
            "this is your grounding, read it and describe what is actually here:\n"
            + "".join(f"  - {m}\n" for m in members)
        )

        try:
            fields = parse_response(call_model(message, model_ref))
        except Exception as error:
            print(f"  ! {row['handle']} FAILED — {type(error).__name__}: {error}")
            continue

        if fields["needs_human"] and not fields["seo_title"]:
            db.save_collection_proposal(
                conn, gid=row["gid"], field="seo_title",
                current_value=row["seo_title"],
                proposed_value=fields["needs_human"], model=model_ref,
                prompt_version=version_tag,
                grounding=fields.get("grounding"), status="needs_human")
            conn.commit()
            print(f"  ? {row['handle']} — NEEDS HUMAN")
            continue

        for field in ("seo_title", "seo_description"):
            if not fields[field]:
                print(f"  ! {row['handle']} — {field} MISSING")
                continue
            db.save_collection_proposal(
                conn, gid=row["gid"], field=field,
                current_value=row[field], proposed_value=fields[field],
                model=model_ref, prompt_version=version_tag,
                grounding=fields.get("grounding"))
        conn.commit()
        print(f"  {row['products_count']:>4} products  {fields['seo_title']}")
