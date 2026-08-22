"""
push.py — stage 6 of the pipeline: one approved proposal -> the live store.

Why this file exists:
  This is the only file in the repository allowed to write to the Shopify
  store. Every other module — db.py, fetch.py, prioritise.py, generate.py,
  verify.py, review.py — is read-only by design, not by convention. Keeping
  the single mutation in one file means the blast radius of this project is
  one file to audit, one file to test, and one file to be careful in.

  The store is live: ~370 products, real revenue, and a human who edits
  listings by hand. That is the operating condition this file is shaped
  around, not an edge case.

What it does:
  - Reads the newest un-superseded proposal for one named product and field.
  - Re-reads the LIVE product (not the local mirror) to snapshot the value it
    is about to overwrite, and to run the §6.4 staleness guard against the
    store_updated_at recorded in products.
  - Writes the undo row into pushes BEFORE calling productUpdate, so a crash
    mid-write leaves a recoverable record rather than an unexplained change.
  - Calls productUpdate for exactly one field.
  - Re-reads the product a second time and confirms the value actually landed,
    stamping verified_at only when it did.
  - undo_push() replays the before_value back to the store, which is what
    turns "the undo log is written first" from an assertion into a claim that
    has been executed.

What it is FORBIDDEN from doing:
  - Never generates copy and never calls a model. It pushes text that already
    exists in proposals, written by generate.py.
  - Never touches product.title, price, status, inventory, or any field other
    than seo.title and seo.description. Out of scope permanently, per
    DESIGN-v2.md §2 and §3.
  - Never UPDATEs or DELETEs a row in proposals. That table is append-only.
  - Never writes to the store unless the caller passed live=True. The default
    is a dry run, and a dry run does not touch the undo log either — there is
    no write to undo.
  - Never writes more than the one product named by the caller. The --limit 10
    batch loop is a later session; there is no loop in this file.
"""

import datetime
import json
import os
import sqlite3

import requests
from dotenv import load_dotenv

load_dotenv()  # populate os.environ from .env before the reads below

STORE = os.environ["SHOPIFY_STORE"]  # e.g. mystore.myshopify.com — KeyError if absent
TOKEN = os.environ["SHOPIFY_TOKEN"]  # Admin API access token — KeyError if absent
VERSION = os.environ["SHOPIFY_API_VERSION"]  # e.g. 2025-07 — KeyError if absent

_ENDPOINT = f"https://{STORE}/admin/api/{VERSION}/graphql.json"

_HEADERS = {
    "Content-Type": "application/json",
    "X-Shopify-Access-Token": TOKEN,
}

_TIMEOUT = 30  # seconds; a hung request must not leave the operator guessing

# The two fields this module is permitted to write, mapped to the key they
# occupy inside SEOInput. Anything not in this dict raises before a request is
# built — the scope limit is data, not a comment someone can talk themselves
# out of.
_FIELD_TO_SEO_KEY = {
    "seo_title": "title",
    "seo_description": "description",
}

# Fields that are GENERATED but must never be written to the store, and the
# reason each is refused. DESIGN-v2 §3a says this refusal is hard-coded here;
# until 2026-08-21 it was not, and a product_title proposal would have fallen
# through to the _FIELD_TO_SEO_KEY lookup and raised a bare KeyError — a
# failure that reads like a bug rather than a policy.
#
# The distinction matters. An unknown field is a mistake. These are known,
# deliberate, and refused on purpose, and the error should say so.
_NEVER_PUSH = {
    "product_title":
        "product.title is the store's H1 and the strongest on-page signal. "
        "An autonomous rewrite can torch brand voice across a live revenue "
        "store, so it is generated as a suggestion for the owner to apply by "
        "hand in Shopify admin. There is no promotion path — see §3a.",
    "body_description":
        "editing populated body copy is a different risk class from filling a "
        "null field, and it has no verify.py path yet.",
}


def _refuse_if_never_push(field: str) -> None:
    """
    Raise before a request is built if this field may never reach the store.

    Called first by push_one(), ahead of every other check. A design-doc rule
    is a promise; a raised exception is a guarantee, and this is the one place
    that distinction can be enforced.
    """
    if field in _NEVER_PUSH:
        raise ValueError(
            f"REFUSED: {field} is never pushable. {_NEVER_PUSH[field]}"
        )

_LIVE_FIELD_QUERY = """
query PushReadProduct($id: ID!) {
    product(id: $id) {
        id
        updatedAt
        seo {
            title
            description
        }
    }
}
"""

# productUpdate takes `product: ProductUpdateInput` on 2025-07. The older
# `input: ProductInput` argument no longer exists on this API version — it was
# checked against the live schema, not assumed.
_PRODUCT_UPDATE = """
mutation PushSeoField($product: ProductUpdateInput!) {
    productUpdate(product: $product) {
        product {
            id
            updatedAt
            seo {
                title
                description
            }
        }
        userErrors {
            field
            message
        }
    }
}
"""

_SELECT_PROPOSAL = """
SELECT
    id, gid, field, current_value, proposed_value,
    model, prompt_version, created_at, status
FROM proposals
WHERE gid = :gid
  AND field = :field
  AND superseded_by IS NULL
ORDER BY id DESC
LIMIT 1
"""

_INSERT_PUSH = """
INSERT INTO pushes
(
    proposal_id, batch_id, autonomy_level, pushed_at,
    before_value, after_value, api_response, verified_at, rolled_back_at
)
VALUES
(
    :proposal_id, :batch_id, :autonomy_level, :pushed_at,
    :before_value, :after_value, NULL, NULL, NULL
)
"""

# COALESCE on api_response so a verify that was handed no response body leaves
# the column as it found it rather than blanking a response already recorded.
_MARK_VERIFIED = """
UPDATE pushes
SET
    verified_at  = :verified_at,
    api_response = COALESCE(:api_response, api_response)
WHERE id = :id
"""

_SELECT_PUSH = """
SELECT
    p.id, p.proposal_id, p.before_value, p.after_value,
    p.pushed_at, p.verified_at, p.rolled_back_at,
    r.gid, r.field
FROM pushes p
JOIN proposals r ON r.id = p.proposal_id
WHERE p.id = :id
"""

_MARK_ROLLED_BACK = """
UPDATE pushes
SET rolled_back_at = :rolled_back_at
WHERE id = :id
"""


def push_one(conn: sqlite3.Connection, gid: str, field: str, live: bool = False) -> dict | None:
    """
    WHAT IT DOES:
      Pushes exactly one field of one product, end to end. Reads the newest
      proposal for that gid and field, re-reads the live product to snapshot
      the value about to be overwritten, runs the staleness guard, and — only
      when live=True — writes the undo row, calls productUpdate, and verifies
      the write by reading the product back a third time.

      The staleness guard (DESIGN-v2.md §6.4) is enforced here and nowhere
      else: the live updatedAt is compared against store_updated_at on the
      matching row in the local products table, which fetch.py stamped. If the
      store has moved since that fetch, someone edited the listing by hand and
      the proposal was written against text that no longer exists — so this
      prints why and stops without writing anything. §6.4 also calls for the
      proposal to be marked 'stale'; that UPDATE is deliberately not done here
      because proposals is append-only, and reconciling the two rules is its
      own commit in verify.py, not a quiet exception made by the one module
      allowed to touch the store.

      Called by: the operator, from the REPL — once per product per field.
                 There is no main(), no argparse, and no batch loop in this
                 file; the --limit 10 loop from §7 is a later session.

      In the pipeline: generate.py -> proposals table
                         -> push_one()
                         -> get_proposal() / fetch_live_field()
                         -> write_undo_row()  [BEFORE the store is touched]
                         -> push_field_to_shopify()
                         -> verify_push()
                         -> pushes table, and the live store

    WHY IT IS ITS OWN FUNCTION:
      The rejected alternative was letting the operator call write_undo_row(),
      push_field_to_shopify() and verify_push() by hand from the REPL in the
      right order. That works exactly until the night it does not: the ordering
      of those three calls IS the safety property of this project, and an
      ordering that lives in a human's memory is one distracted evening away
      from a live write with no undo row behind it. Putting the sequence in one
      function makes the order a property of the code, and makes the dry-run
      default the only path a tired operator can take by accident.

      It is also the only place that knows whether this is a dry run, which is
      why the live flag stops here and is never threaded down into the writing
      functions — a function that writes should not also be deciding whether
      writing was allowed.

    WHAT IT RETURNS, AND WHO CONSUMES IT:
      A dict with six keys — gid, field, before, after, verified, push_id —
      consumed by the operator reading it in the REPL, and by the batch loop a
      later session will wrap around this function.

      before is the live value at push time (None when the field was empty,
      which is ~99% of this catalog). after is the proposed text. verified is
      True only when a fresh read of the store came back equal to after; on a
      dry run it is False because nothing was written, not because anything
      failed. push_id is the pushes row id, and None on a dry run — a dry run
      writes no undo row, since there is no write to undo.

      Returns None when there is nothing to push at all: no proposal for this
      gid and field, no local products row to compare against, or the
      staleness guard tripped. None means "no attempt was made"; a dict means
      an attempt was made and its outcome is in the dict.
    """
    # FIRST, before the proposal is even read. A refusal must not depend on
    # whether a row happens to exist — the field is forbidden either way, and
    # checking later would mean the guard is skipped for exactly the products
    # that have no proposal yet.
    _refuse_if_never_push(field)

    proposal = get_proposal(conn, gid, field)
    if proposal is None:
        print(f"no proposal for {gid} / {field} — nothing to push")
        return None

    before_value, live_updated_at = fetch_live_field(gid, field)

    local = conn.execute(
        "SELECT store_updated_at FROM products WHERE gid = ?", (gid,)
    ).fetchone()
    if local is None:
        # No mirror row means there is no baseline to compare the live
        # timestamp against, so the staleness guard cannot run. Refusing is the
        # only honest option: run fetch.py first.
        print(f"{gid} is not in the local products table — run fetch.py, then retry")
        return None

    # Parsed rather than string-compared. Both sides are Shopify's own
    # ISO-8601, so lexical comparison would work today, but a timezone offset
    # instead of a trailing Z would silently compare wrong.
    live_moment = datetime.datetime.fromisoformat(live_updated_at.replace("Z", "+00:00"))
    local_moment = datetime.datetime.fromisoformat(
        local["store_updated_at"].replace("Z", "+00:00")
    )
    if live_moment > local_moment:
        print(
            f"STALE — {gid} was edited in the store after the last fetch\n"
            f"  local  store_updated_at: {local['store_updated_at']}\n"
            f"  live   updatedAt:        {live_updated_at}\n"
            f"  proposal {proposal['id']} was written against the old text — not pushing.\n"
            f"  run fetch.py and regenerate before pushing this product."
        )
        return None

    after_value = proposal["proposed_value"]

    if not live:
        print(
            f"DRY RUN — would push proposal {proposal['id']} to {gid} / {field}\n"
            f"  before: {before_value!r}\n"
            f"  after:  {after_value!r}\n"
            f"  pass live=True to write. No undo row written — nothing was written to undo."
        )
        return {
            "gid": gid,
            "field": field,
            "before": before_value,
            "after": after_value,
            "verified": False,
            "push_id": None,
        }

    # Single-gid pushes still carry a batch_id so that when the batch loop
    # arrives, today's rows and tomorrow's group the same way.
    batch_id = _utc_now()

    # Order is the whole point: the undo row is committed first, so a crash or
    # a timeout inside push_field_to_shopify() leaves a row naming exactly what
    # the value was before the call that may or may not have landed.
    push_id = write_undo_row(
        conn,
        proposal_id=proposal["id"],
        batch_id=batch_id,
        before_value=before_value,
    )
    print(f"undo row {push_id} written — before: {before_value!r}")

    response = push_field_to_shopify(gid, field, after_value)
    verified = verify_push(conn, push_id, gid, field, after_value, api_response=response)

    return {
        "gid": gid,
        "field": field,
        "before": before_value,
        "after": after_value,
        "verified": verified,
        "push_id": push_id,
    }


def get_proposal(conn: sqlite3.Connection, gid: str, field: str) -> dict | None:
    """
    WHAT IT DOES:
      Selects the newest proposal for one product and one field that has not
      been superseded — highest id, superseded_by IS NULL — and returns it as a
      plain dict.

      There is deliberately NO status filter in the WHERE clause. That is not
      an oversight to be fixed by whoever reads this next: verify.py and
      review.py are not built yet, so no row in proposals has ever advanced
      past 'draft', and filtering on status='approved' today would make this
      function return None for every row in the table. At L0 the approval step
      is the operator typing the exact gid on the command line. When verify.py
      and review.py exist, the status gate belongs in this WHERE clause and
      nowhere else — that is the one line to change.

      Called by: push_one(), once per push — the first thing it does, before
                 any network call, so a missing proposal costs nothing.

      In the pipeline: generate.py -> save_proposal() -> proposals table
                         -> get_proposal()
                         -> push_one()   [reads proposed_value and id]

    WHY IT IS ITS OWN FUNCTION:
      The rejected alternative was inlining the SELECT in push_one(). The
      reason not to is the paragraph above: the status gate is the single most
      important change this file will receive, and it must land in exactly one
      place. Inlined, "which proposal gets pushed" would be four lines buried
      in the middle of a function that also does network I/O and timestamp
      arithmetic — and the day the gate is added, the reviewer would have to
      read the whole orchestrator to be sure it was added once and correctly.

      The superseded_by IS NULL clause is the same argument in miniature. The
      append-only rule means a revised proposal is a NEW row, so "the current
      proposal" is a query, not a fact — and that query should have one home.

    WHAT IT RETURNS, AND WHO CONSUMES IT:
      A dict of the proposal row: id, gid, field, current_value,
      proposed_value, model, prompt_version, created_at, status. A dict rather
      than the sqlite3.Row so the caller can be handed a value that outlives
      the cursor and prints readably in the REPL.

      None means no un-superseded proposal exists for this gid and field —
      either generate.py has not run for this product, or the model omitted
      that field and no row was written. push_one() treats None as "print and
      stop", never as "push something else".

      The consumer is push_one(), which reads ["id"] for the undo row's
      proposal_id and ["proposed_value"] as the text to write to the store.
    """
    row = conn.execute(_SELECT_PROPOSAL, {"gid": gid, "field": field}).fetchone()
    return dict(row) if row is not None else None


def fetch_live_field(gid: str, field: str) -> tuple[str | None, str]:
    """
    WHAT IT DOES:
      Queries the Shopify Admin GraphQL API for one product's seo { title,
      description } and updatedAt, and returns the requested field's value
      alongside the timestamp. Reads the LIVE store, never the local mirror —
      the mirror is a snapshot from the last fetch.py run and is exactly the
      thing the staleness guard exists to distrust.

      Checks body["errors"] explicitly. Shopify answers a completely failed
      query with HTTP 200 and an errors array, so raise_for_status() alone
      would hand back a body with no "data" key and the failure would surface
      later as a KeyError somewhere unrelated.

      Called by: push_one(), once before the write to snapshot before_value and
                 the staleness timestamp; verify_push(), once after the write
                 to confirm it landed. Twice per successful push, three times
                 counting the verify inside undo_push().

      In the pipeline: the live Shopify store
                         -> fetch_live_field()
                         -> push_one()      [before_value + staleness guard]
                         -> verify_push()   [the value to compare]

    WHY IT IS ITS OWN FUNCTION:
      The rejected alternative was importing fetch.shopify_graphql() and
      reusing it. It would have worked for this read, but fetch.py's module
      docstring states it never writes to the store — and once push.py routes
      requests through it, the mutation is one careless edit away from going
      out over the same helper, and the audit claim "only push.py writes"
      stops being checkable by reading push.py. The duplicated six lines of
      requests.post buy a file that can be audited on its own.

      Keeping the read separate from the mutation matters for a second reason:
      this function is called both before and after the write, and verify_push
      needs a genuinely independent read. If the read were folded into
      push_field_to_shopify() and verification used the mutation's echoed
      response, a push would be verifying itself — the response says what
      Shopify claims it did, and a fresh query says what is actually there.

    WHAT IT RETURNS, AND WHO CONSUMES IT:
      A tuple (value, store_updated_at).

      value is the live text of the requested field, or None when the field is
      empty — which is the normal case on this catalog, where ~99% of products
      have no seo_title. None here means "empty in the store", not "lookup
      failed": a gid that does not resolve raises instead.

      store_updated_at is Shopify's updatedAt as an ISO-8601 string, in the
      same format fetch.py stored in products.store_updated_at, so the two are
      directly comparable.

      value goes to push_one() as before_value, which passes it to
      write_undo_row() — that string is the entire undo capability of this
      project. store_updated_at goes to the staleness comparison in push_one().
      verify_push() consumes value only, and discards the timestamp.
    """
    seo_key = _seo_key_for(field)

    response = requests.post(
        _ENDPOINT,
        headers=_HEADERS,
        json={"query": _LIVE_FIELD_QUERY, "variables": {"id": gid}},
        timeout=_TIMEOUT,
    )
    response.raise_for_status()  # real HTTP failures: 401 auth, 5xx, rate-limit
    body = response.json()
    if "errors" in body:  # Shopify returns query errors at HTTP 200; must check explicitly
        raise RuntimeError(f"Shopify GraphQL error: {body['errors'][0]['message']}")

    product = body["data"]["product"]
    if product is None:  # a valid query for a gid that does not exist returns null
        raise RuntimeError(f"no such product in the store: {gid}")

    return product["seo"][seo_key], product["updatedAt"]


def _seo_key_for(field: str) -> str:
    """
    WHAT IT DOES:
      Translates a proposals.field value ('seo_title' / 'seo_description') into
      the key it occupies inside Shopify's SEOInput ('title' / 'description'),
      and raises on anything else.

      Called by: fetch_live_field() and push_field_to_shopify(), once each per
                 call — so twice per push, plus once more per verify.

      In the pipeline: it sits between the column name used everywhere in
      seo.db and the field name used in the GraphQL documents in this file.

    WHY IT IS ITS OWN FUNCTION:
      The rejected alternative was field.removeprefix("seo_") at both call
      sites. That is shorter and it is wrong in the way that matters here:
      "seo_" stripped off an unexpected value produces a plausible-looking key
      and sends a mutation for a field nobody authorised. DESIGN-v2.md §2 and
      §3 put product.title, price and status permanently out of scope, and the
      cheapest way to keep that true is a lookup that raises on anything not in
      the dict, in the one place both the read and the write pass through.

    WHAT IT RETURNS, AND WHO CONSUMES IT:
      'title' or 'description', and nothing else — it raises rather than
      returning for any other input. fetch_live_field() uses it to index the
      seo object in the response; push_field_to_shopify() uses it to build the
      SEOInput sent to productUpdate.
    """
    if field not in _FIELD_TO_SEO_KEY:
        raise ValueError(
            f"push.py may only touch {sorted(_FIELD_TO_SEO_KEY)}, got {field!r}"
        )
    return _FIELD_TO_SEO_KEY[field]


def write_undo_row(
    conn: sqlite3.Connection,
    proposal_id: int,
    batch_id: str,
    before_value: str | None,
    autonomy_level: str = "L0",
) -> int:
    """
    WHAT IT DOES:
      Inserts one row into pushes recording what is about to happen: which
      proposal, which batch, at what autonomy level, at what time, the value
      currently live (before_value), and the value about to replace it
      (after_value, read off the proposal). api_response, verified_at and
      rolled_back_at are left NULL — they are stamped later by verify_push()
      and undo_push(), and a NULL in any of them is meaningful: it means that
      stage has not happened.

      Commits immediately, on its own. This is the one place in the codebase
      where a commit is not deferred to the caller, and the reason is the whole
      safety model: the row must be durable on disk BEFORE the network call
      that changes the store, not merely staged in an open transaction that a
      crash would roll back. A staged undo row and no undo row are the same
      thing to a process that dies mid-write.

      Called by: push_one(), once per live push — never on a dry run, because a
                 dry run performs no write and an undo log full of writes that
                 never happened is worse than no log.

      In the pipeline: fetch_live_field()  [the before value]
                         -> write_undo_row()
                         -> pushes table (committed)
                         -> push_field_to_shopify()   [only now is the store touched]

    WHY IT IS ITS OWN FUNCTION:
      The rejected alternative was an INSERT inline in push_one() right above
      the mutation call. Physically adjacent lines are not the same as an
      enforced order: someone refactoring push_one() — adding a batch loop,
      hoisting the mutation, wrapping things in a try — can move the INSERT
      below the write without noticing, and nothing would fail a test. As its
      own function that commits before returning, "the undo row exists" is a
      precondition the caller cannot accidentally reorder away, because the
      push_id it needs for verify_push() only exists after this returns.

      The commit-here decision is the other half. Deferring it to the caller
      would put the single most important durability guarantee in the file
      under the control of whichever function calls next.

    WHAT IT RETURNS, AND WHO CONSUMES IT:
      The new pushes row's id, as an int, taken from cursor.lastrowid.

      push_one() holds it and passes it to verify_push(), which stamps
      verified_at and api_response on that exact row. It is also the handle the
      operator types into undo_push(push_id) to revert the write — so the
      number this returns is what stands between a bad push and a permanent
      one, and it is printed for that reason.
    """
    proposal = conn.execute(
        "SELECT proposed_value FROM proposals WHERE id = ?", (proposal_id,)
    ).fetchone()
    if proposal is None:
        raise ValueError(f"no proposal with id {proposal_id} — refusing to log a push")

    cursor = conn.execute(
        _INSERT_PUSH,
        {
            "proposal_id": proposal_id,
            "batch_id": batch_id,
            "autonomy_level": autonomy_level,
            "pushed_at": _utc_now(),
            "before_value": before_value,  # NULL is a real answer here: the field was empty
            "after_value": proposal["proposed_value"],
        },
    )
    conn.commit()  # durable BEFORE the store is touched — the entire point of this function
    return cursor.lastrowid


def push_field_to_shopify(gid: str, field: str, new_value: str) -> dict:
    """
    WHAT IT DOES:
      Calls productUpdate for exactly one product and exactly one SEO field.
      This is the only function in this entire codebase that calls a Shopify
      mutation. Everything above it in the pipeline is a read.

      Builds a ProductUpdateInput containing only { id, seo: { <one key> } },
      so the fields not named are left untouched by Shopify rather than
      overwritten with nulls — pushing a title must not blank the description.

      Checks failures at both levels Shopify reports them. body["errors"] is a
      malformed or rejected query returned at HTTP 200; userErrors is a
      well-formed mutation that Shopify declined — a length limit, an invalid
      value. Either one raises, because a push that did not happen must never
      reach verify_push() looking like a push that did.

      Called by: push_one(), once per live push; undo_push(), once per revert.
                 Never called on a dry run.

      In the pipeline: write_undo_row()   [must have committed first]
                         -> push_field_to_shopify()
                         -> the live Shopify store
                         -> verify_push()  [which ignores this response and re-reads]

    WHY IT IS ITS OWN FUNCTION:
      The rejected alternative was building the mutation inside push_one().
      The reason it is separate is not tidiness — it is that undo_push() calls
      the identical operation with the before_value, and a revert implemented
      as its own second copy of the mutation is a revert that can drift from
      the push. The undo path has to be exactly as correct as the write path,
      and the cheapest way to guarantee that is for both to be the same code.

      It is also the function an auditor greps for. "Which line of this repo
      changes the store" should have exactly one answer, and it should be
      findable by searching for productUpdate.

    WHAT IT RETURNS, AND WHO CONSUMES IT:
      The full parsed response body as a dict, unmodified — including the
      product node Shopify echoes back and the (empty, by the time it returns)
      userErrors list. Never a body containing errors; those raise.

      push_one() passes it through to verify_push(), which serialises it into
      pushes.api_response for the audit trail. It is deliberately NOT used to
      decide whether the write succeeded: this is Shopify's account of what it
      did, and verify_push() asks the store separately what is actually there.
    """
    seo_key = _seo_key_for(field)
    variables = {"product": {"id": gid, "seo": {seo_key: new_value}}}

    response = requests.post(
        _ENDPOINT,
        headers=_HEADERS,
        json={"query": _PRODUCT_UPDATE, "variables": variables},
        timeout=_TIMEOUT,
    )
    response.raise_for_status()
    body = response.json()
    if "errors" in body:  # same 200-with-errors contract as fetch_live_field
        raise RuntimeError(f"Shopify GraphQL error: {body['errors'][0]['message']}")

    user_errors = body["data"]["productUpdate"]["userErrors"]
    if user_errors:  # a valid mutation Shopify refused — the field is unchanged
        raise RuntimeError(
            f"productUpdate rejected {gid} / {field}: "
            f"{user_errors[0]['field']} — {user_errors[0]['message']}"
        )

    return body


def verify_push(
    conn: sqlite3.Connection,
    push_id: int,
    gid: str,
    field: str,
    expected_value: str | None,
    api_response: dict | None = None,
) -> bool:
    """
    WHAT IT DOES:
      Re-reads the field from the live store — a fresh query, not the response
      productUpdate returned — and compares it to expected_value. On a match it
      stamps verified_at on the pushes row and records the API response
      alongside it. On a mismatch it leaves verified_at NULL and prints a loud
      warning.

      A failed verify does not raise. An unverified push is data: the row
      stays in pushes with verified_at NULL, which is exactly the query that
      answers "which writes did not land" later. Crashing here would abort the
      batch loop a later session wraps around this, and would leave the
      operator with a traceback instead of a record.

      Values are compared with None and empty string treated as the same
      thing, because Shopify returns an unset SEO field as null but accepts ""
      as the way to clear one — undo_push() relies on that equivalence.

      Called by: push_one(), once per live push; undo_push(), once per revert
                 to confirm the before_value actually went back.

      In the pipeline: push_field_to_shopify()
                         -> verify_push()
                         -> fetch_live_field()   [an independent read]
                         -> pushes.verified_at / pushes.api_response

      The api_response parameter is not in the signature named in the build
      notes for this file; it is here because the notes also require this
      function to write api_response, and this is the only function that runs
      after the mutation and knows the push_id. It is keyword-only in practice
      and defaults to None, and a None leaves the column as it found it.

    WHY IT IS ITS OWN FUNCTION:
      The rejected alternative was trusting productUpdate's echoed product
      node — it comes back in the same response and comparing it costs nothing.
      That verifies the wrong thing. It confirms Shopify parsed the request,
      not that the store now serves that text: a theme-level override, a
      concurrent app write, or a partially applied mutation all produce a happy
      response over a store that does not match. The whole reason this project
      re-reads is that the store is not assumed to obey.

      Separating it from push_one() also means the revert path gets verified by
      the same code as the write path, which is what makes undo_push() a proof
      rather than a hope.

    WHAT IT RETURNS, AND WHO CONSUMES IT:
      True when the live value equals expected_value, False when it does not.
      False is a report, not an error — the caller decides what to do with it.

      push_one() puts it in the returned dict under "verified", which is what
      the operator reads to decide whether to call undo_push(). undo_push()
      uses it to decide whether the revert itself landed, and prints a warning
      when it did not, because a failed revert is the one state in this system
      that needs a human immediately.

      The durable output matters more than the bool: pushes.verified_at is NULL
      until this returns True, so "SELECT * FROM pushes WHERE verified_at IS
      NULL" is the standing list of writes nobody has confirmed.
    """
    live_value, _ = fetch_live_field(gid, field)

    # Shopify reports an unset SEO field as null but takes "" to clear one, so
    # the two must compare equal or every revert-to-empty would report failure.
    matched = (live_value or "") == (expected_value or "")

    if not matched:
        print(
            f"!! VERIFY FAILED for push {push_id} — {gid} / {field}\n"
            f"   expected: {expected_value!r}\n"
            f"   live now: {live_value!r}\n"
            f"   pushes.verified_at left NULL. The write may not have landed, or\n"
            f"   something else changed the field between the write and this read."
        )
        return False

    conn.execute(
        _MARK_VERIFIED,
        {
            "verified_at": _utc_now(),
            # Stored as text; the column is a record for later reading, not
            # something any query needs to look inside.
            "api_response": json.dumps(api_response) if api_response is not None else None,
            "id": push_id,
        },
    )
    conn.commit()
    print(f"verified push {push_id} — {gid} / {field} is live")
    return True


def undo_push(conn: sqlite3.Connection, push_id: int) -> bool:
    """
    WHAT IT DOES:
      Reverts one push. Reads before_value off the named row in pushes, joins
      through to proposals for the gid and field the row belongs to, writes
      that old value back to the store with the same mutation the push used,
      verifies the revert with a fresh read, and stamps rolled_back_at.

      rolled_back_at is stamped whether or not the verify passed, because the
      column records that a revert was attempted on this row; verified_at
      records whether the store agreed. Conflating them would lose the
      difference between "reverted" and "tried to revert".

      A before_value of NULL is sent as an empty string. Shopify has no way to
      return an SEO field to genuinely unset via productUpdate, so "" is the
      closest available revert — the field reads as empty everywhere it is
      consumed, and verify_push() treats "" and NULL as equal for exactly this
      reason. Worth knowing rather than discovering: this is the one respect in
      which an undo is not byte-identical to the original state.

      Called by: the operator, from the REPL, with a push_id printed by
                 push_one(). Once per push that needs reverting — and once
                 deliberately, on a push that does not, because a rollback path
                 that has never executed is a rollback path that does not work.

      In the pipeline: pushes table   [before_value, written before the push]
                         -> undo_push()
                         -> push_field_to_shopify()
                         -> verify_push()
                         -> pushes.rolled_back_at

    WHY IT IS ITS OWN FUNCTION:
      The rejected alternative was no function at all — the undo row holds the
      old value, so an operator can read it and re-push by hand. That is the
      version of a backup nobody has ever restored from. "The undo log is
      written before the write" is a claim about recoverability, and the only
      thing that turns it into a fact is code that performs the recovery and
      has been run. Making it a function means the claim is testable in one
      call, on a real product, in the same session as the push.

      It is a separate function from push_one() rather than a flag on it
      because a revert has no proposal to read, no staleness guard to run, and
      writes no new undo row — it is pointed at a row that already exists.

    WHAT IT RETURNS, AND WHO CONSUMES IT:
      True when a fresh read confirms the store is back to before_value, False
      when the revert was attempted but the store does not match.

      The operator consumes it. False after an undo is the loudest signal this
      system can produce — it means the store holds a value nobody chose, the
      push row has both verified_at and rolled_back_at telling different parts
      of the story, and the next step is a person opening the Shopify admin.

      Raises rather than returning if push_id does not exist, because the
      alternative is a False that reads like a failed revert when in fact
      nothing was ever attempted.
    """
    row = conn.execute(_SELECT_PUSH, {"id": push_id}).fetchone()
    if row is None:
        raise ValueError(f"no push with id {push_id} — nothing to undo")

    if row["rolled_back_at"] is not None:
        print(f"push {push_id} was already rolled back at {row['rolled_back_at']}")
        return False

    gid = row["gid"]
    field = row["field"]
    before_value = row["before_value"]

    # No true "unset" exists through productUpdate; "" is the closest revert.
    revert_value = before_value if before_value is not None else ""
    if before_value is None:
        print(f"before_value was NULL — reverting {field} to empty string, not unset")

    print(f"undoing push {push_id} — {gid} / {field} back to {before_value!r}")
    response = push_field_to_shopify(gid, field, revert_value)
    verified = verify_push(conn, push_id, gid, field, before_value, api_response=response)

    conn.execute(_MARK_ROLLED_BACK, {"rolled_back_at": _utc_now(), "id": push_id})
    conn.commit()  # stamped regardless of verify: the attempt is what this column records

    if not verified:
        print(
            f"!! UNDO NOT CONFIRMED for push {push_id} — the store does not match\n"
            f"   before_value. Open the Shopify admin for {gid} and check {field} by hand."
        )

    return verified


def _utc_now() -> str:
    """
    WHAT IT DOES:
      Returns the current UTC time as an ISO-8601 string in the same
      "%Y-%m-%dT%H:%M:%SZ" shape fetch.py and generate.py write.

      Called by: write_undo_row() (pushed_at and batch_id), verify_push()
                 (verified_at) and undo_push() (rolled_back_at) — three to four
                 times per push.

      In the pipeline: it produces the timestamps stored in the pushes table,
      which are compared against products.store_updated_at and read in order by
      anyone reconstructing what happened during a run.

    WHY IT IS ITS OWN FUNCTION:
      The rejected alternative was the datetime one-liner at each of the four
      call sites. Four copies of a format string is four chances for one of
      them to drift — a microsecond-bearing isoformat() in one place and a
      trailing Z in another sort differently as text, and these columns are
      read as text by every query that reconstructs a run's order. One function
      means the timestamps in pushes are comparable to each other and to the
      ones fetch.py wrote, by construction.

    WHAT IT RETURNS, AND WHO CONSUMES IT:
      A string like "2026-08-16T14:03:22Z". It goes straight into pushes rows
      as pushed_at, batch_id, verified_at or rolled_back_at, and is read back
      by the operator and by any later query that orders pushes by time.
    """
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
