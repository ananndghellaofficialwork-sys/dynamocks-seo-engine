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

# _MAX_MEDIA: how many media nodes to request per product.
#
# This started at 10, based on a sample of 8 products where 10 was the largest.
# The sample lied. Verified against all 455 products on 2026-08-20: 21 of them
# carry MORE than 10 media, the largest is 14, and 32 photos were silently
# dropped on the first full run. Worse, truncation takes the LAST images, and
# position 0 is frequently the promo graphic -- so the cap was discarding real
# product photos while keeping the banner.
#
# 25 gives headroom well past the current maximum. Raise this number rather than
# adding a second page: a nested pagination loop for images is not worth the
# complexity at this catalog size. _images_json warns loudly if it is ever hit.
_MAX_MEDIA = 25

# The `... on MediaImage` fragment is required, not stylistic: media can also hold
# videos and 3D models, which have no `image` field. Without the fragment the query
# does not compile.
#
# mediaCount is fetched purely so truncation can be DETECTED. Without it, hitting
# the cap looks identical to a product that simply has fewer photos -- which is
# exactly how the first version lost 32 images without complaining.
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
                mediaCount {
                    count
                }
                media(first: __MAX_MEDIA__) {
                    edges {
                        node {
                            ... on MediaImage {
                                image {
                                    url
                                    altText
                                }
                            }
                        }
                    }
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
""".replace("__MAX_MEDIA__", str(_MAX_MEDIA))
# Substituted rather than f-stringed: the query is full of GraphQL braces, and an
# f-string would require doubling every one of them. One token, one replace.


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
    - Delegates the media edges to _images_json for the images column.
    - Stamps fetched_at as current UTC time in ISO-8601 format.

    Returns:
    - dict with all 14 keys required by db.upsert_product.
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
        "images":           _images_json(node),              # JSON array of every photo; None if the product has none
        "store_updated_at": node["updatedAt"],               # Shopify's timestamp; push.py reads this for the staleness guard
        "fetched_at":       datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _images_json(node):
    """
    WHAT IT DOES:
      Walks the media edges on one product node, keeps only the entries that are
      actually photographs, and returns them as a JSON string ready to drop
      straight into the products.images column.

      Each kept image becomes {"url", "alt_text", "position"}. position is the
      index Shopify returned it at, which is the order the images appear on the
      product page -- that ordering is information, not decoration, so it is
      preserved rather than inferred later from list order alone.

      Called by: _to_row(), once per product -- so once per row written, roughly
                 455 times per full fetch run.

      In the pipeline: Shopify GraphQL media edges
                         -> _images_json()
                         -> _to_row()            [as the "images" key]
                         -> db.upsert_product()  [written to products.images]
                         -> generate.py          [read back as grounding input]

    WHY IT IS ITS OWN FUNCTION:
      The rejected alternative was a list comprehension inline in _to_row's
      return dict, matching how tags is handled one line above. That works only
      because tags is a flat list of strings. Media is not: it is a mixed
      collection that can hold videos and 3D models alongside photos, so it
      needs a skip condition, and the position index has to be assigned while
      walking. Inlining that means a conditional comprehension with an enumerate
      inside a dict literal -- the kind of line that is written once and never
      safely edited again.

      It is also the part most likely to change. Which images are worth keeping
      is an open question (see the alt-text junk-image hypothesis in
      DESIGN-v2 §12a); when that rule arrives it lands here, in one function,
      rather than as surgery on the row builder every other column depends on.

    WHAT IT RETURNS, AND WHO CONSUMES IT:
      A JSON string, e.g.
        '[{"url": "https://cdn.shopify.com/...jpg", "alt_text": "Dynamocks
           Bubbles polka dot cotton crew socks", "position": 0}, ...]'

      None when the product has no photographs at all. That None becomes SQL
      NULL in products.images, which is the honest record: a product with no
      picture is data, not an error, and the same rule CLAUDE.md already sets
      for a failed verify applies here.

      _to_row() puts the string in the "images" key; db.upsert_product() writes
      it verbatim. Downstream, generate.py parses it back with json.loads and
      picks which photos to send to the model -- that selection is deliberately
      NOT made here, because fetch.py mirrors the store and does not decide what
      the store is worth.
    """
    edges = node["media"]["edges"]

    # Truncation check. A short page and a genuinely small product look identical
    # in the response, so the only honest test is: did we come back holding
    # exactly the cap, while the store says there is more? Print rather than
    # raise -- a lost photo is a degraded input, not a corrupt one, and aborting
    # a 455-product fetch over it would be the wrong trade.
    if len(edges) == _MAX_MEDIA and node["mediaCount"]["count"] > _MAX_MEDIA:
        print(
            f"  WARNING truncated: {node['title'][:50]} has "
            f"{node['mediaCount']['count']} media, kept {_MAX_MEDIA} -- raise _MAX_MEDIA"
        )

    images = []

    for edge in edges:
        image = edge["node"].get("image")
        if not image:
            continue          # video or 3D model: the inline fragment returned an empty node

        # position counts photos only, so it stays contiguous when a video sits
        # between two images. The question downstream is "which photo comes
        # first", and a video is not an answer to that question. The cost is
        # that true media position is not recoverable from this column -- if
        # that ever matters, it is a new field, not a redefinition of this one.
        images.append(
            {
                "url":      image["url"],
                "alt_text": image["altText"],   # often "" on promo graphics; kept as-is, never invented
                "position": len(images),
            }
        )

    return json.dumps(images) if images else None


def _selftest_images_json():
    """
    WHAT IT DOES:
      Runs _images_json against four hand-built media payloads and asserts the
      output, so the parsing can be checked without a Shopify token, a network
      call, or a database. Run it with: python -c "import fetch; fetch._selftest_images_json()"

      The four cases are the ones that actually bite: a normal multi-image
      product, a product with no media at all, a video sitting between two
      photos, and an image whose altText is empty.

      Called by: nobody in the pipeline -- invoked by hand from the REPL after
                 editing _images_json.

      In the pipeline: not in it. This is a guard around _images_json, which is
                       the one part of fetch.py whose input shape is decided by
                       Shopify and can change without warning.

    WHY IT IS ITS OWN FUNCTION:
      The rejected alternative was verifying by running the real fetch and
      eyeballing the database. That needs a token, burns API calls, and cannot
      exercise the empty-media or video-in-the-middle cases at all, because it
      only sees whatever the store happens to contain today. A pure function
      with fabricated input can test the cases that are rare in production and
      therefore most likely to be wrong.

    WHAT IT RETURNS, AND WHO CONSUMES IT:
      Nothing. It raises AssertionError on the first failure and prints one
      confirmation line if every case passes. The consumer is a human deciding
      whether it is safe to run the real fetch.
    """
    def media(*nodes, total=None):
        return {
            "title": "test product",
            "mediaCount": {"count": total if total is not None else len(nodes)},
            "media": {"edges": [{"node": n} for n in nodes]},
        }

    photo_a = {"image": {"url": "https://cdn/a.jpg", "altText": "polka dot socks"}}
    photo_b = {"image": {"url": "https://cdn/b.jpg", "altText": ""}}
    video   = {}   # a Video node: the MediaImage fragment contributes no fields

    # 1. two photos -> both kept, positions 0 and 1
    result = json.loads(_images_json(media(photo_a, photo_b)))
    assert len(result) == 2, result
    assert result[0] == {"url": "https://cdn/a.jpg", "alt_text": "polka dot socks", "position": 0}, result[0]

    # 2. empty alt text survives as "" -- it is never guessed at or filled in
    assert result[1]["alt_text"] == "", result[1]

    # 3. no media at all -> None, which becomes SQL NULL, not an empty array
    assert _images_json(media()) is None

    # 4. a video between two photos -> dropped, and positions stay 0,1 with no gap
    result = json.loads(_images_json(media(photo_a, video, photo_b)))
    assert [i["position"] for i in result] == [0, 1], result

    # 5. hitting the cap while the store holds more must WARN, not pass silently.
    #    This is the case that shipped broken on 2026-08-20 and cost 32 photos:
    #    the old code had no way to tell a full page from a small product.
    capped = media(*([photo_a] * _MAX_MEDIA), total=_MAX_MEDIA + 4)
    import io, contextlib
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        _images_json(capped)
    assert "truncated" in buffer.getvalue(), "cap was hit silently -- the 8/20 bug is back"

    # 6. and the mirror image: exactly at the cap with nothing left behind is
    #    NOT truncation and must stay quiet, or the warning becomes noise.
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        _images_json(media(*([photo_a] * _MAX_MEDIA)))
    assert buffer.getvalue() == "", buffer.getvalue()

    print("_images_json: 6/6 cases pass")


if __name__ == "__main__":
    main()
