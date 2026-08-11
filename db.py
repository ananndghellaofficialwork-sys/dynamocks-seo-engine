import sqlite3

conn = sqlite3.connect("data/seo.db")  # createes file dba nd opens connection

cur = conn.cursor()

cur.execute(
    """CREATE TABLE IF NOT EXISTS products (
    product_id TEXT PRIMARY KEY,   -- gid://shopify/Product/...
    handle TEXT,
    title TEXT,
    sku TEXT,
    inventory INTEGER,
    status TEXT,
    priority_score REAL
)
"""
)

cur.execute("""
CREATE TABLE IF NOT EXISTS fields (
    handle TEXT,
    entity_type TEXT,
    field_name TEXT,
    live_value TEXT,
    approved_value TEXT,
    approved_at TEXT,
    published_at TEXT,
    PRIMARY KEY (handle, entity_type, field_name)
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS seo_drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    handle TEXT,
    field_name TEXT,
    draft_value TEXT,
    model TEXT,
    created_at TEXT,
    similarity_score REAL,
    accepted INTEGER
)
""")

conn.commit()
conn.close()
print("done")
