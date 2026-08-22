import datetime  # UTC timestamps for exclusion rows
import sqlite3  # standard library database adapter — no pip install needed
from pathlib import Path  # portable path handling; avoids raw string concatenation

DB_PATH = Path(
    "data/seo.db"
)  # canonical path to seo.db; every other module imports this constant

# _DDL holds the full schema for all five tables.
# Prefixed _ to signal module-private; other modules never reference it directly.
# CREATE TABLE IF NOT EXISTS makes every statement idempotent.
_DDL = """
-- products: disposable mirror of the Shopify store. Overwritten on every fetch.
CREATE TABLE IF NOT EXISTS products
(
    gid                TEXT PRIMARY KEY,   -- Shopify global ID, e.g. gid://shopify/Product/12345
    handle             TEXT NOT NULL,      -- URL-safe slug used as a human-readable identifier
    sku                TEXT,               -- stock-keeping unit from the first variant; nullable
    title              TEXT NOT NULL,      -- product display name
    product_type       TEXT,               -- merchant-defined category; nullable
    vendor             TEXT,               -- brand or supplier name; nullable
    tags               TEXT,               -- JSON array stored as text, e.g. '["socks","blue"]'
    status             TEXT,               -- ACTIVE | DRAFT | ARCHIVED
    total_inventory    INTEGER,            -- sum of stock across all variants and locations
    seo_title          TEXT,               -- NULL on ~99% of the catalog today — primary target
    seo_description    TEXT,               -- may contain known duplicate clusters — secondary target
    images             TEXT,               -- JSON array of {url, alt_text, position}; NULL if no media
    body_html          TEXT,               -- descriptionHtml, the on-page copy
    material           TEXT,               -- fibre composition extracted from body_html; NULL if absent
    store_updated_at   TEXT NOT NULL,      -- Shopify's updatedAt; drives the staleness guard in push.py
    fetched_at         TEXT NOT NULL,      -- ISO-8601 UTC timestamp of the most recent fetch run
    delisted_at        TEXT                -- set when a fetch no longer finds this product; NULL = live
);

-- delisted_at exists because the row cannot simply be deleted. proposals,
-- pushes, scores and seo_exclusions all reference products(gid), and pushes is
-- the undo log for real writes to a live store -- deleting the product would
-- either fail the foreign key or destroy the record of what was written and
-- how to reverse it. Marking keeps the audit trail intact and takes the
-- product out of every work queue, which is the actual requirement.
--
-- It is NOT written by upsert_product. A fetch that finds a product refreshes
-- every mirrored column; whether a product is GONE is decided by absence from
-- the whole run, which no per-row upsert can see.

-- images holds EVERY product photo, not the featured one. Verified live 2026-08-20:
-- products carry 6-10 images and image #1 is frequently a promo graphic rather than
-- the sock (Allure #1 is a "Buy One Get One Free" Instagram post). Storing only the
-- first would ground the generator on a sale banner. Which images actually reach the
-- model is a cost decision that belongs in generate.py, not here -- fetch mirrors the
-- store, it does not choose. Same layering rule the unreachable-catalog bug broke.

-- metrics: prioritisation inputs sourced from the Shopify sales_by_product export.
CREATE TABLE IF NOT EXISTS metrics
(
    gid                TEXT PRIMARY KEY REFERENCES products(gid),  -- FK; one metrics row per product
    revenue_12mo       REAL DEFAULT 0,     -- rolling 12-month revenue in store currency
    units_12mo         INTEGER DEFAULT 0,  -- rolling 12-month unit sales count
    priority_score     REAL,               -- computed by prioritise.py using the §6.3 formula
    computed_at        TEXT                -- ISO-8601 UTC timestamp of last priority calculation
);

-- proposals: append-only audit trail. Never updated in place, never deleted.
CREATE TABLE IF NOT EXISTS proposals
(
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    gid                TEXT NOT NULL REFERENCES products(gid),  -- which product this draft is for
    field              TEXT NOT NULL,           -- 'seo_title' or 'seo_description'
    current_value      TEXT,                    -- snapshot of the live value at generation time
    proposed_value     TEXT NOT NULL,           -- the LLM-generated candidate text
    model              TEXT NOT NULL,           -- model ID used, e.g. 'claude-opus-4-8'
    prompt_version     TEXT NOT NULL,           -- version tag; ties a regression to a specific prompt
    created_at         TEXT NOT NULL,           -- ISO-8601 UTC timestamp of generation
    grounding          TEXT,                    -- the model's stated reasoning; see note below
    uniqueness_status  TEXT,                    -- 'pass' | 'fail' | 'not_checked'
    max_similarity     REAL,                    -- cosine similarity vs nearest neighbour in embeddings
    nearest_gid        TEXT,                    -- gid of the product it collided with, if any
    eval_score         REAL,                    -- rubric score from verify.py; range 0.0–1.0
    status             TEXT NOT NULL,           -- state machine from §6.2
    reviewer_note      TEXT,                    -- free-text note from the business owner
    superseded_by      INTEGER                  -- points at the retry proposal id, if any
);

-- grounding holds what the model said it saw and what it rejected, captured
-- from listing-v3.md's first output line. Stored per proposal, and duplicated
-- across the seo_title and seo_description rows of the same product, because
-- one call produced both and a row that cannot explain itself alone is no use
-- to a reviewer reading a CSV.
--
-- The reason it is worth a column: a wrong title and a right title look
-- identical in review. "Geometric Print Crew Socks" is correct copy if the
-- sock has triangles on it and an invention if the model read a sale banner.
-- Only the reasoning separates the two, and asking the model again later gets
-- a fresh rationalisation rather than the one that actually produced this row.
--
-- NULL on anything generated with v1 or v2 — those prompts never asked for it.
-- That NULL is honest: it means nobody knows why that copy was written.

-- pushes: the undo log. One row per live write. Written BEFORE the write happens.
CREATE TABLE IF NOT EXISTS pushes
(
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id        INTEGER NOT NULL REFERENCES proposals(id),  -- which proposal was pushed
    batch_id           TEXT NOT NULL,           -- groups all pushes from a single push.py run
    autonomy_level     TEXT NOT NULL,           -- 'L0' | 'L1' | 'L2' | 'L3' at time of write
    pushed_at          TEXT NOT NULL,           -- ISO-8601 UTC timestamp of the write
    before_value       TEXT,                    -- the value that was live before the write — the undo
    after_value        TEXT NOT NULL,           -- the value written to the store
    api_response       TEXT,                    -- raw JSON from Shopify's productUpdate response
    verified_at        TEXT,                    -- set by push.py after re-read confirms the write
    rolled_back_at     TEXT                     -- set if this write was subsequently undone
);

-- embeddings: cosine similarity vectors stored as binary BLOBs.
CREATE TABLE IF NOT EXISTS embeddings
(
    gid                TEXT NOT NULL,           -- references products(gid); no ON DELETE cascade needed
    field              TEXT NOT NULL,           -- 'seo_title' or 'seo_description'
    vector             BLOB NOT NULL,           -- float32 array serialised with ndarray.tobytes()
    model              TEXT NOT NULL,           -- embedding model id, e.g. 'all-MiniLM-L6-v2'
    computed_at        TEXT NOT NULL,           -- ISO-8601 UTC timestamp
    PRIMARY KEY (gid, field)                    -- one vector per (product, field) pair
);

-- seo_exclusions: products the generator must never spend a paid call on.
--
-- A TABLE and not a column on products, because products is a disposable
-- mirror -- upsert_product overwrites every one of its columns from live store
-- data on each fetch, so a flag written there would survive exactly until the
-- next run. This sits outside the mirror, like proposals and pushes do.
--
-- A ROW PER PRODUCT and not a rule in generate.py's WHERE clause, because the
-- reason is worth keeping: six months from now "why does this product have no
-- SEO proposal" should be answerable by a query, not by reading a commit
-- history. It also allows excluding a one-off product without inventing a
-- category rule to justify it.
CREATE TABLE IF NOT EXISTS seo_exclusions
(
    gid                TEXT PRIMARY KEY REFERENCES products(gid),
    reason             TEXT NOT NULL,           -- free text; why this product is out
    excluded_at        TEXT NOT NULL            -- ISO-8601 UTC timestamp
);

-- collections: the category pages. Mirrored the same way products are.
--
-- Added 2026-08-21, after the search data made the case impossible to ignore:
-- product pages carry 22,973 of 26,470 impressions and collections carry 270.
-- Every competitor ranking for a head term like "ankle length socks" does it
-- with a COLLECTION page — adidas, Nike, Darn Tough, Sealskinz — while this
-- store answers with a single product and sits at position 9 with no clicks.
--
-- The grounding is different from a product's. A collection has no photograph
-- of itself and no fibre composition; what it IS is the set of products in it.
-- member_titles holds that set, and it is the only honest source for what the
-- page should claim to be.
CREATE TABLE IF NOT EXISTS collections
(
    gid                TEXT PRIMARY KEY,   -- gid://shopify/Collection/...
    handle             TEXT NOT NULL,      -- URL slug; often more accurate than the title
    title              TEXT NOT NULL,      -- the merchandising name, e.g. "Dotty Delight"
    body_html          TEXT,               -- on-page description, usually empty
    seo_title          TEXT,               -- NULL on 12 of 19 today
    seo_description    TEXT,
    products_count     INTEGER,            -- how many products the page lists
    member_titles      TEXT,               -- JSON array of product titles — the grounding
    image_url          TEXT,
    store_updated_at   TEXT,
    fetched_at         TEXT NOT NULL,
    delisted_at        TEXT
);

-- collection_proposals: the same job as `proposals`, for category pages.
--
-- A SEPARATE TABLE, and the reason is a foreign key. proposals.gid REFERENCES
-- products(gid) — that constraint is real and it is doing useful work, so a
-- collection gid cannot go in that table without either violating it or
-- removing it. Removing it would mean rebuilding a table that already holds
-- 700+ rows AND is pointed at by pushes.proposal_id, which is the undo log for
-- real writes to a live store. That is not a rebuild worth risking to save one
-- table.
--
-- The rejected alternative was inserting collections into `products` so the FK
-- would pass. It would work and it would be a lie: every count, every queue and
-- every "how many products do we have" answer would silently include category
-- pages.
--
-- Append-only, same as proposals. Same columns, minus the ones that only make
-- sense for a product.
CREATE TABLE IF NOT EXISTS collection_proposals
(
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    gid                TEXT NOT NULL REFERENCES collections(gid),
    field              TEXT NOT NULL,           -- 'seo_title' | 'seo_description'
    current_value      TEXT,
    proposed_value     TEXT NOT NULL,
    model              TEXT NOT NULL,
    prompt_version     TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    grounding          TEXT,
    eval_score         REAL,
    status             TEXT NOT NULL,
    reviewer_note      TEXT,
    superseded_by      INTEGER
);

-- keywords: what people actually type. The demand side of the pipeline.
--
-- Everything before this table describes what a product IS — its photo, its
-- fibre, its pattern. None of it says what a shopper TYPES, and those are
-- different things: "Geometric Block Pattern Crew Socks" was accurate and
-- unsearchable. This table is the other half.
--
-- source is not decoration. The three feeds answer different questions and are
-- trusted differently:
--   gsc_query  real Google queries WITH impressions and position. The only
--              source with volume attached. Blind to anything not already
--              ranking — which is why it showed 58 impressions for "dress"
--              while dress socks are the top-selling line.
--   gsc_page   impressions and position PER URL. Not a query; a priority
--              signal saying which pages already rank and are losing the click.
--   onsite     Shopify's own search box. Real intent from people already on
--              the store, including demand for things not stocked.
--   autocomplete  Google's suggestions. NO VOLUME — proves a phrase exists,
--              not that it is worth a title. Ranked last, used to find the
--              phrasings the other two cannot see.
CREATE TABLE IF NOT EXISTS keywords
(
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    source             TEXT NOT NULL,           -- gsc_query | gsc_page | onsite | autocomplete
    query              TEXT,                    -- the search phrase; NULL for gsc_page rows
    landing_page       TEXT,                    -- the URL; NULL for query-only rows
    impressions        INTEGER,
    clicks             INTEGER,
    position           REAL,
    captured_at        TEXT NOT NULL
);

-- scores: what an independent judge model thought of a piece of generated copy.
--
-- APPEND-ONLY, like proposals. A re-score is a new row, never an edit. The
-- point of keeping the old number is that when a prompt change moves a product
-- from 2 to 5, both numbers have to still exist for that to be visible.
--
-- proposal_id is NULLABLE on purpose. A NULL means the copy was never saved as
-- a proposal -- an experiment, like the image-vs-text spike -- and a real id
-- means this scored something that is a genuine push candidate. Both belong in
-- one table because the question "show me the worst copy we have produced" is
-- the same question either way.
--
-- accuracy and search are stored SEPARATELY and never averaged here. A title
-- can be perfectly true and still be a phrase nobody searches for; collapsing
-- the two into one number is exactly how that failure hides.
--
-- judge_model is recorded because the score is only meaningful relative to who
-- gave it. CLAUDE.md: scores come from a model outside the generating set --
-- this column is what makes that rule checkable after the fact rather than
-- merely asserted.
CREATE TABLE IF NOT EXISTS scores
(
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id        INTEGER REFERENCES proposals(id),  -- NULL for experiments
    gid                TEXT NOT NULL REFERENCES products(gid),
    run_label          TEXT NOT NULL,           -- which experiment or eval run
    arm                TEXT,                    -- e.g. 'text-only' | 'image+text'
    seo_title          TEXT,                    -- the copy as scored, so the row
    seo_description    TEXT,                    --   stands alone without a join
    accuracy           INTEGER,                 -- 0-5, grounded in the real product
    search             INTEGER,                 -- 0-5, would a shopper type this
    won                INTEGER,                 -- 1 if the judge preferred this arm
    reason             TEXT,                    -- the judge's one-line why
    judge_model        TEXT NOT NULL,
    scored_at          TEXT NOT NULL            -- ISO-8601 UTC timestamp
);
"""

# _UPSERT_PRODUCT: parameterised INSERT ... ON CONFLICT used by upsert_product().
# Named placeholders (:key) are bound from the row dict the caller passes.
# Prefixed _ to signal module-private; fetch.py never references this string directly.
_UPSERT_PRODUCT = """
INSERT INTO products
(
    gid, handle, sku, title, product_type, vendor, tags,
    status, total_inventory, seo_title, seo_description, images,
    body_html, material, store_updated_at, fetched_at
)
VALUES
(
    :gid, :handle, :sku, :title, :product_type, :vendor, :tags,
    :status, :total_inventory, :seo_title, :seo_description, :images,
    :body_html, :material, :store_updated_at, :fetched_at
)
ON CONFLICT(gid) DO UPDATE SET    -- gid already exists: overwrite every column with fresh store data
    handle           = excluded.handle,
    sku              = excluded.sku,
    title            = excluded.title,
    product_type     = excluded.product_type,
    vendor           = excluded.vendor,
    tags             = excluded.tags,
    status           = excluded.status,
    total_inventory  = excluded.total_inventory,
    seo_title        = excluded.seo_title,
    seo_description  = excluded.seo_description,
    images           = excluded.images,
    body_html        = excluded.body_html,
    material         = excluded.material,
    store_updated_at = excluded.store_updated_at,
    fetched_at       = excluded.fetched_at    -- excluded.* refers to the values that lost the conflict
"""


# ─────────────────────────────────────────────────────────────────────────────
# connect()
#
# WHY:
#   Every module in the pipeline needs a database connection. Without this
#   function each module would repeat the same three-line setup — connect,
#   set row_factory, enable foreign keys — and any one of them could omit a
#   step and produce subtly broken behaviour. Centralising the setup here means
#   there is exactly one place to change if the configuration ever needs to
#   change.
#
# WHAT IT DOES:
#   Opens seo.db at DB_PATH (creating the file and the data/ directory if
#   neither exists yet). Configures two settings that every caller depends on:
#     - row_factory = sqlite3.Row  so query results support column-name access
#       instead of positional indexing (row["gid"] rather than row[0]).
#     - PRAGMA foreign_keys = ON  so the FK constraints declared in _DDL are
#       actually enforced; SQLite silently ignores them by default.
#
# RETURNS:
#   A configured sqlite3.Connection. The caller owns the connection and is
#   responsible for calling conn.close() when done, or using it as a context
#   manager (with connect() as conn:).
#
# Called by: fetch.py, prioritise.py, generate.py, verify.py, review.py, push.py
#            — every module that reads from or writes to seo.db.
# ─────────────────────────────────────────────────────────────────────────────
def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(
        exist_ok=True
    )  # create data/ directory if it does not exist yet
    conn = sqlite3.connect(DB_PATH)  # open seo.db; creates the file on first call
    conn.row_factory = (
        sqlite3.Row
    )  # rows come back as dict-like objects, not plain tuples
    conn.execute(
        "PRAGMA foreign_keys = ON"
    )  # activate FK enforcement; SQLite ignores FKs by default
    return conn  # caller owns the connection — must close it explicitly


# ─────────────────────────────────────────────────────────────────────────────
# init_schema(conn)
#
# WHY:
#   A freshly created seo.db is empty. Before any module can read or write,
#   the five tables must exist with the correct columns and constraints. We
#   need one authoritative place to define and apply the schema so that the
#   database is always in a known state before the pipeline starts.
#
# WHAT IT DOES:
#   Passes _DDL to executescript(), which runs all five CREATE TABLE IF NOT
#   EXISTS statements in a single call. IF NOT EXISTS makes every statement a
#   no-op when the table is already present, so this function is safe to call
#   on every startup without checking whether the schema exists first.
#   executescript() issues an implicit COMMIT before it runs, so no explicit
#   commit is needed after this call.
#
# RETURNS:
#   Nothing (None). The side effect is that all five tables exist in the
#   database when the function returns.
#
# Called by: fetch.py — once at startup, before the first upsert_product call.
# ─────────────────────────────────────────────────────────────────────────────
def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        _DDL
    )  # run all five CREATE TABLE IF NOT EXISTS statements in one script block


# ─────────────────────────────────────────────────────────────────────────────
# upsert_product(conn, row)
#
# WHY:
#   fetch.py receives the full product catalog from Shopify on every run.
#   products is a disposable mirror — it must reflect the store exactly after
#   each fetch. We need a write operation that handles both new products
#   (INSERT) and products that already exist in the local mirror (UPDATE)
#   without requiring fetch.py to check first, and without ever leaving a
#   stale row behind.
#
# WHAT IT DOES:
#   Executes _UPSERT_PRODUCT against the open connection, binding the :key
#   named placeholders from the row dict. If the gid does not yet exist in
#   products, a new row is inserted. If it does exist, every column is
#   overwritten with the value from row — including store_updated_at, which
#   the staleness guard in push.py reads later.
#   Does NOT commit. fetch.py issues a single conn.commit() after all
#   upserts complete, so a network failure mid-fetch leaves the database
#   unchanged rather than half-updated.
#
# RETURNS:
#   Nothing (None). The side effect is that one row is staged in the open
#   transaction, to be committed or rolled back by the caller.
#
# Called by: fetch.py — once per product in the Shopify GraphQL response.
# ─────────────────────────────────────────────────────────────────────────────
def upsert_product(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        _UPSERT_PRODUCT, row
    )  # bind :key placeholders from row dict; does not commit


# ─────────────────────────────────────────────────────────────────────────────
# count_products(conn)
#
# WHY:
#   After fetch.py commits the upserts, we need a quick sanity check that the
#   expected number of products actually landed. Without a dedicated function,
#   every caller would write its own COUNT(*) query inline — a one-liner that
#   is easy to get wrong (wrong table name, forgetting fetchone, off-by-one on
#   index). Isolating it here gives the check a single tested location.
#
# WHAT IT DOES:
#   Issues a SELECT COUNT(*) FROM products query against the open connection
#   and extracts the integer from the single-row result. Read-only — does not
#   modify any data.
#
# RETURNS:
#   An int — the total number of rows currently in the products table.
#   Returns 0 on an empty or freshly initialised database.
#
# Called by: fetch.py — after conn.commit(), to print the final loaded row count.
# ─────────────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
# reconcile_delisted(conn, run_started)
#
# WHY:
#   fetch.py upserts. An upsert can add and it can update, but it can never
#   notice an absence — so when the owner deletes 80 products from the store,
#   the local mirror keeps all 80 forever and the generator happily writes SEO
#   copy for listings that no longer exist. The bug is silent, which is the
#   worst kind: every count still looks right.
#
#   The rejected alternative was deleting seo.db and re-fetching. That works on
#   products and destroys everything else — proposals, scores, seo_exclusions,
#   and pushes, which is the undo log for real writes already made to a live
#   store. Rebuilding the mirror must not cost the audit trail.
#
#   Detection is by timestamp rather than by comparing gid lists. Every upsert
#   stamps fetched_at with the current time, so any row still carrying a
#   timestamp older than the run start was not returned by the store this time.
#   That needs no in-memory set of 455 ids and no SQL parameter limit to worry
#   about as the catalog grows.
#
# WHAT IT DOES:
#   Two updates. Marks rows the run did not touch as delisted. Clears the mark
#   on any row the run DID touch — a product restored in Shopify comes back to
#   life here rather than staying dead because it was gone once.
#   Does NOT commit; the caller owns the transaction.
#
# RETURNS:
#   A tuple (newly_delisted, restored) of ints, for the operator to read. Both
#   zero on a normal run where nothing changed in the store.
#
# Called by: fetch.py — once per run, after the final page commits.
# ─────────────────────────────────────────────────────────────────────────────
def reconcile_delisted(conn: sqlite3.Connection, run_started: str) -> tuple[int, int]:
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    gone = conn.execute(
        """
        UPDATE products
        SET delisted_at = :now
        WHERE fetched_at < :run_started
          AND delisted_at IS NULL
        """,
        {"now": now, "run_started": run_started},
    ).rowcount

    back = conn.execute(
        """
        UPDATE products
        SET delisted_at = NULL
        WHERE fetched_at >= :run_started
          AND delisted_at IS NOT NULL
        """,
        {"run_started": run_started},
    ).rowcount

    return gone, back


def count_products(conn: sqlite3.Connection, include_delisted: bool = True) -> int:
    # Defaults to counting everything so existing callers keep their old
    # meaning. fetch.py passes False, because after a reconcile the number an
    # operator actually wants is how many products still exist in the store.
    where = "" if include_delisted else " WHERE delisted_at IS NULL"
    result = conn.execute(
        f"SELECT COUNT(*) FROM products{where}"
    ).fetchone()  # single-row result from the aggregate
    return result[0]  # index 0 is the COUNT(*) integer


# ─────────────────────────────────────────────────────────────────────────────
# exclude_from_seo(conn, gid, reason)
#
# WHY:
#   Some products must never reach the generator — today the Invisibles / No
#   Show line, which is being discontinued, so any copy written for it is a
#   paid API call spent on a listing that will not exist. The alternative was
#   a condition inside get_products_needing_seo's WHERE clause. That works
#   until the second reason arrives, at which point the queue definition
#   becomes a growing list of special cases and nobody can answer "why is this
#   product excluded" without reading SQL. A row with a reason answers it.
#
# WHAT IT DOES:
#   Inserts one exclusion row, or overwrites the reason and timestamp if the
#   product is already excluded — re-running the seeding script is therefore
#   safe and idempotent. Does NOT commit; the caller owns the transaction, the
#   same contract upsert_product follows.
#
#   Note this is NOT append-only. proposals is append-only because it is an
#   audit trail of what the AI proposed; this is a live policy list, and
#   "which products are excluded right now" is the question it has to answer.
#
# RETURNS:
#   Nothing (None). Side effect: one row staged in the open transaction.
#
# Called by: exclude_noshow.py, once per excluded product.
# ─────────────────────────────────────────────────────────────────────────────
def exclude_from_seo(conn: sqlite3.Connection, gid: str, reason: str) -> None:
    conn.execute(
        """
        INSERT INTO seo_exclusions
        (
            gid, reason, excluded_at
        )
        VALUES
        (
            :gid, :reason, :excluded_at
        )
        ON CONFLICT(gid) DO UPDATE SET
            reason      = excluded.reason,
            excluded_at = excluded.excluded_at
        """,
        {
            "gid": gid,
            "reason": reason,
            "excluded_at": datetime.datetime.now(datetime.timezone.utc)
                                   .strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# save_score(conn, row)
#
# WHY:
#   A judge's verdict that is only printed to a terminal is gone the moment the
#   window closes, which makes "come back later for the products that scored
#   badly" impossible to answer without re-running and re-paying for every
#   score. Writing it down turns a one-off opinion into a queryable backlog.
#
#   The rejected alternative was storing the judge's raw reply text in one
#   column. That reads fine and cannot be sorted — and sorting is the entire
#   point, because the only useful question is "which are the worst". Parsing
#   at write time, once, beats parsing at read time, every time.
#
# WHAT IT DOES:
#   Inserts one row per scored candidate. Append-only: there is no ON CONFLICT
#   clause, no UPDATE path, and re-scoring the same copy deliberately produces
#   a second row so the change is visible. Does NOT commit; the caller owns the
#   transaction.
#
#   accuracy, search and won may all be None. A judge that returned an
#   unparseable reply still gets a row, holding the raw reason — because "the
#   judge failed on this product" is itself a finding, and a silently missing
#   row would look identical to a product that was never scored.
#
# RETURNS:
#   The new row's id (int), so a caller scoring two arms can relate them if it
#   ever needs to.
#
# Called by: spike_images.py today, once per arm per product. verify.py later,
#            once per proposal — the eval gate in DESIGN-v2 Component 6.
# ─────────────────────────────────────────────────────────────────────────────
def save_score(conn: sqlite3.Connection, row: dict) -> int:
    cursor = conn.execute(
        """
        INSERT INTO scores
        (
            proposal_id, gid, run_label, arm,
            seo_title, seo_description,
            accuracy, search, won, reason,
            judge_model, scored_at
        )
        VALUES
        (
            :proposal_id, :gid, :run_label, :arm,
            :seo_title, :seo_description,
            :accuracy, :search, :won, :reason,
            :judge_model, :scored_at
        )
        """,
        {
            "proposal_id": row.get("proposal_id"),
            "gid": row["gid"],
            "run_label": row["run_label"],
            "arm": row.get("arm"),
            "seo_title": row.get("seo_title"),
            "seo_description": row.get("seo_description"),
            "accuracy": row.get("accuracy"),
            "search": row.get("search"),
            "won": row.get("won"),
            "reason": row.get("reason"),
            "judge_model": row["judge_model"],
            "scored_at": datetime.datetime.now(datetime.timezone.utc)
                                 .strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    )
    return cursor.lastrowid


# ─────────────────────────────────────────────────────────────────────────────
# upsert_collection(conn, row)
#
# WHY:
#   Same contract as upsert_product, for the collections mirror. Kept as its
#   own function rather than a generic upsert(table, row) because the two
#   tables have different columns and a generic version would build SQL from a
#   dict's keys — which is how a typo in a caller silently writes to the wrong
#   column instead of failing.
#
# WHAT IT DOES:
#   Inserts, or overwrites every mirrored column when the gid already exists.
#   Does NOT commit; the caller owns the transaction.
#
# RETURNS:
#   Nothing. One row staged in the open transaction.
#
# Called by: fetch.py — once per collection.
# ─────────────────────────────────────────────────────────────────────────────
def upsert_collection(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        """
        INSERT INTO collections
        (
            gid, handle, title, body_html, seo_title, seo_description,
            products_count, member_titles, image_url, store_updated_at, fetched_at
        )
        VALUES
        (
            :gid, :handle, :title, :body_html, :seo_title, :seo_description,
            :products_count, :member_titles, :image_url, :store_updated_at, :fetched_at
        )
        ON CONFLICT(gid) DO UPDATE SET
            handle           = excluded.handle,
            title            = excluded.title,
            body_html        = excluded.body_html,
            seo_title        = excluded.seo_title,
            seo_description  = excluded.seo_description,
            products_count   = excluded.products_count,
            member_titles    = excluded.member_titles,
            image_url        = excluded.image_url,
            store_updated_at = excluded.store_updated_at,
            fetched_at       = excluded.fetched_at
        """,
        row,
    )


# ─────────────────────────────────────────────────────────────────────────────
# save_collection_proposal(conn, ...)
#
# WHY:
#   generate.save_proposal writes to `proposals`, whose gid is foreign-keyed to
#   products. A collection gid fails that constraint. This is the same append
#   with the same discipline, aimed at the table that can hold it.
#
# WHAT IT DOES:
#   Appends one row. No UPDATE path and no ON CONFLICT: a revision is a new row
#   pointed at by superseded_by, exactly as with product proposals.
#   Does NOT commit.
#
# RETURNS:
#   The new row id, so a caller writing two fields can link them.
#
# Called by: generate.generate_for_collections — once per field per collection.
# ─────────────────────────────────────────────────────────────────────────────
def save_collection_proposal(conn: sqlite3.Connection, gid: str, field: str,
                             current_value, proposed_value: str, model: str,
                             prompt_version: str, grounding=None,
                             status: str = "draft") -> int:
    cursor = conn.execute(
        """
        INSERT INTO collection_proposals
        (
            gid, field, current_value, proposed_value,
            model, prompt_version, created_at, grounding, status
        )
        VALUES
        (
            :gid, :field, :current_value, :proposed_value,
            :model, :prompt_version, :created_at, :grounding, :status
        )
        """,
        {
            "gid": gid, "field": field, "current_value": current_value,
            "proposed_value": proposed_value, "model": model,
            "prompt_version": prompt_version, "grounding": grounding,
            "status": status,
            "created_at": datetime.datetime.now(datetime.timezone.utc)
                                   .strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    )
    return cursor.lastrowid
