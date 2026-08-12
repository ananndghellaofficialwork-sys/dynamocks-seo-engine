from pathlib import Path    # portable path handling; avoids raw string concatenation
import sqlite3              # standard library database adapter — no pip install needed

DB_PATH = Path("data/seo.db")  # canonical path to seo.db; every other module imports this constant

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
    store_updated_at   TEXT NOT NULL,      -- Shopify's updatedAt; drives the staleness guard in push.py
    fetched_at         TEXT NOT NULL       -- ISO-8601 UTC timestamp of the most recent fetch run
);

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
    uniqueness_status  TEXT,                    -- 'pass' | 'fail' | 'not_checked'
    max_similarity     REAL,                    -- cosine similarity vs nearest neighbour in embeddings
    nearest_gid        TEXT,                    -- gid of the product it collided with, if any
    eval_score         REAL,                    -- rubric score from verify.py; range 0.0–1.0
    status             TEXT NOT NULL,           -- state machine from §6.2
    reviewer_note      TEXT,                    -- free-text note from the business owner
    superseded_by      INTEGER                  -- points at the retry proposal id, if any
);

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
"""

# _UPSERT_PRODUCT: parameterised INSERT ... ON CONFLICT used by upsert_product().
# Named placeholders (:key) are bound from the row dict the caller passes.
# Prefixed _ to signal module-private; fetch.py never references this string directly.
_UPSERT_PRODUCT = """
INSERT INTO products
(
    gid, handle, sku, title, product_type, vendor, tags,
    status, total_inventory, seo_title, seo_description,
    store_updated_at, fetched_at
)
VALUES
(
    :gid, :handle, :sku, :title, :product_type, :vendor, :tags,
    :status, :total_inventory, :seo_title, :seo_description,
    :store_updated_at, :fetched_at
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
    DB_PATH.parent.mkdir(exist_ok=True)        # create data/ directory if it does not exist yet
    conn = sqlite3.connect(DB_PATH)            # open seo.db; creates the file on first call
    conn.row_factory = sqlite3.Row             # rows come back as dict-like objects, not plain tuples
    conn.execute("PRAGMA foreign_keys = ON")   # activate FK enforcement; SQLite ignores FKs by default
    return conn                                # caller owns the connection — must close it explicitly


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
    conn.executescript(_DDL)    # run all five CREATE TABLE IF NOT EXISTS statements in one script block


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
    conn.execute(_UPSERT_PRODUCT, row)    # bind :key placeholders from row dict; does not commit


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
def count_products(conn: sqlite3.Connection) -> int:
    result = conn.execute("SELECT COUNT(*) FROM products").fetchone()    # single-row result from the aggregate
    return result[0]                                                      # index 0 is the COUNT(*) integer
