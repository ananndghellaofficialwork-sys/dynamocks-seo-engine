# Dynamocks SEO Engine

An SEO pipeline for a live Shopify store (~470 products, 36 collections).
It finds the weakest listings, drafts new meta titles and descriptions,
and only writes to the store after a human approves them.

Built to fix a real problem: ~99% of products have no meta title, and
26 of 36 collections have none either.

## How it works

1. `fetch_catalog.py` — pulls the catalog from the Shopify Admin API
2. `db.py` — SQLite schema: products, fields, drafts
3. Priority score picks the work — no hand-typed product lists
4. LLM drafts one field at a time
5. Human approves. Nothing reaches the store unapproved.

## Design

See `docs/DESIGN-v2.md` for the autonomy ladder and safety rules.

## Status

Schema built. Catalog load next.

## Run it

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 db.py