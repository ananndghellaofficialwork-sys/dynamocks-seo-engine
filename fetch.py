# -----------------------------------------------------------------------------
# fetch.py
#
# Why this file exists:
# - Every downstream module (prioritise, generate, verify, push) reads from
#   the local SQLite mirror in seo.db, not from the live Shopify store.
# - This file is the only place in the pipeline that talks to Shopify to
#   pull the product catalog down into that mirror.
# - The design mandates a re-fetch before every generate run so downstream
#   work is always grounded in current store data, never a stale snapshot.
#
# What it does:
# - Authenticates to the Shopify Admin GraphQL API using credentials from .env.
# - Fetches the full product catalog in pages of 50.
# - Translates each GraphQL node into a flat dict matching the products schema.
# - Writes every product to seo.db via db.upsert_product() — no SQL here.
#
# What it does NOT do:
# - Never writes to the Shopify store. Read-only by design.
# - Never writes SQL directly. All database access goes through db.py.
# -----------------------------------------------------------------------------

import datetime
import json
import os

import requests
from dotenv import load_dotenv

import db

load_dotenv()   # populate os.environ from .env before the reads below

STORE   = os.environ["SHOPIFY_STORE"]          # e.g. mystore.myshopify.com — KeyError if absent
TOKEN   = os.environ["SHOPIFY_TOKEN"]          # Admin API access token — KeyError if absent
VERSION = os.environ["SHOPIFY_API_VERSION"]    # e.g. 2025-01 — KeyError if absent

_ENDPOINT = f"https://{STORE}/admin/api/{VERSION}/graphql.json"

_HEADERS = {
    "Content-Type": "application/json",
    "X-Shopify-Access-Token": TOKEN,
}

_PAGE_SIZE = 50   # products per request; well under Shopify's 250-node limit

_PRODUCTS_QUERY = """
query FetchProducts($first: Int!, $after: String) {
    products(first: $first, after: $after) {
        pageInfo {
            hasNextPage
            endCursor
        }
        edges {
            node {
                id
                handle
                title
                productType
                vendor
                tags
                status
                totalInventory
                updatedAt
                seo {
                    title
                    description
                }
                variants(first: 1) {
                    edges {
                        node {
                            sku
                        }
                    }
                }
            }
        }
    }
}
"""


def main():
    """
    Why:
    - Every downstream module reads from the local SQLite mirror, not the live store.
    - Design mandates a re-fetch before every generate run.
    - This function is the single place that satisfies that requirement.

    What it does:
    - Opens seo.db and ensures schema exists (safe on first run and every run after).
    - Fetches the full product catalog from Shopify one page at a time.
    - After each page: upserts all rows, then commits immediately.
    - Commits per page so a mid-run failure leaves completed pages safely written.
    - Prints a progress line per page and a final row count for operator confirmation.

    Returns:
    - None. Side effect: products table fully populated and committed in seo.db.
    """
    conn = db.connect()          # opens seo.db with row_factory and FK enforcement on
    db.init_schema(conn)         # creates tables if this is a first run; no-op otherwise

    cursor = None
    page = 0

    while True:
        rows, cursor, has_next = fetch_products_page(cursor)
        for row in rows:
            db.upsert_product(conn, row)
        conn.commit()            # commit after each full page so partial runs are recoverable
        page += 1
        print(f"page {page}: {len(rows)} products fetched")
        if not has_next:
            break

    print(f"done — {db.count_products(conn)} products in db")
    conn.close()


def fetch_products_page(cursor=None):
    """
    Why:
    - Shopify paginates using opaque cursors; one request cannot return all 472 products.
    - Isolating one page's call here means main() only manages the loop,
      never the GraphQL response shape or pageInfo fields.

    What it does:
    - Calls shopify_graphql with _PRODUCTS_QUERY and the given cursor as `after`.
    - Passing cursor=None fetches from the beginning of the catalog.
    - Maps each returned product node through _to_row.
    - Reads hasNextPage and endCursor from pageInfo for the caller.

    Returns:
    - 3-tuple (rows, end_cursor, has_next):
      - rows       : list of dicts, each ready for db.upsert_product.
      - end_cursor : opaque string to pass as cursor on the next call; None on last page.
      - has_next   : False means this was the final page.
    """
    variables = {"first": _PAGE_SIZE, "after": cursor}
    body = shopify_graphql(_PRODUCTS_QUERY, variables)
    products = body["data"]["products"]
    rows = [_to_row(edge["node"]) for edge in products["edges"]]
    page_info = products["pageInfo"]
    return rows, page_info["endCursor"], page_info["hasNextPage"]


def shopify_graphql(query, variables):
    """
    Why:
    - Shopify always returns HTTP 200, even on a completely failed query.
    - The real error signal is body["errors"], not the HTTP status code.
    - raise_for_status() alone silently passes broken responses.
    - This is the single place that enforces the correct failure contract.

    What it does:
    - POSTs query and variables to the Shopify GraphQL endpoint with the Admin token.
    - Raises requests.HTTPError on any non-2xx HTTP status.
    - Raises RuntimeError if body["errors"] is present, naming the first error message.
    - Returns the full parsed response body unchanged if no errors exist.

    Returns:
    - Parsed JSON response body as a dict; navigate body["data"] for the payload.
    - Never returns a body that contains errors.
    """
    response = requests.post(
        _ENDPOINT,
        headers=_HEADERS,
        json={"query": query, "variables": variables},
    )
    response.raise_for_status()                        # catches real HTTP failures (5xx, auth 401, etc.)
    body = response.json()
    if "errors" in body:                               # Shopify returns errors at HTTP 200; must check explicitly
        raise RuntimeError(f"Shopify GraphQL error: {body['errors'][0]['message']}")
    return body


def _to_row(node):
    """
    Why:
    - GraphQL returns camelCase keys, nested seo/variants objects, and tags as a plain list.
    - The products table expects snake_case columns, tags as a JSON string, and sku flat.
    - One translation point here means neither fetch_products_page nor db.upsert_product
      needs to know the GraphQL shape; query changes only touch this function.

    What it does:
    - Maps one GraphQL product node to a flat dict matching products column names exactly.
    - Pulls sku from the first variant edge; None if the product has no variants.
    - Serialises tags list to a JSON string.
    - Stamps fetched_at as current UTC time in ISO-8601 format.

    Returns:
    - dict with all 13 keys required by db.upsert_product.
    - Every key is present; nullable columns may carry None.
    """
    variants = node["variants"]["edges"]
    sku = variants[0]["node"]["sku"] if variants else None   # first variant only; None if product has no variants

    return {
        "gid":              node["id"],
        "handle":           node["handle"],
        "sku":              sku,
        "title":            node["title"],
        "product_type":     node["productType"],
        "vendor":           node["vendor"],
        "tags":             json.dumps(node["tags"]),        # stored as JSON text, e.g. '["blue","socks"]'
        "status":           node["status"],
        "total_inventory":  node["totalInventory"],
        "seo_title":        node["seo"]["title"],
        "seo_description":  node["seo"]["description"],
        "store_updated_at": node["updatedAt"],               # Shopify's timestamp; push.py reads this for the staleness guard
        "fetched_at":       datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


if __name__ == "__main__":
    main()
