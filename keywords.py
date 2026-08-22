"""Load real search demand into the keywords table. Read-only against the store.

Three feeds, three different jobs:

  gsc      Search Console export folder — Queries.csv and Pages.csv.
           The only source with volume AND position. Blind to anything the
           store does not already rank for.
  onsite   Shopify "Searches by search query" export. Real intent from people
           already on the site, including demand for products not stocked.
  auto     Google autocomplete phrases, one per line in a text file. No volume;
           proves a phrase exists, nothing more.

Usage:
    python3 keywords.py gsc    "/path/to/dynamocks.us-Performance-on-Search-.../"
    python3 keywords.py onsite "/path/to/Searches by search query.csv"
    python3 keywords.py auto   "/path/to/autocomplete.txt"
    python3 keywords.py show                 what is loaded and what it says
"""
import csv
import datetime
import re
import sys
from pathlib import Path

import db

NOW = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

# Word groups used to report what kind of language the demand is in. Kept here
# rather than in the prompt because these are measurements, not instructions.
GROUPS = {
    "colour": r"\b(purple|pink|green|orange|brown|red|blue|yellow|black|white|grey|gray|"
              r"mango|neon|mint|maroon|teal|aqua|navy|turquoise|olive|rust|burgundy|"
              r"lavender|rainbow|multicolor|colou?rful)\b",
    "length": r"\b(ankle|short|crew|no.?show|invisible|quarter|over the calf|otc|"
              r"mid.?calf|knee|low.?cut)\b",
    "dress":  r"\b(dress|formal|executive|office|business|suit|wedding|groomsmen)\b",
    "gift":   r"\b(gift|set|pack|bundle|box|combo)\b",
    "gender": r"\b(men|mens|women|womens|boys|girls|unisex|kids)\b",
    "material": r"\b(cotton|bamboo|wool|merino|combed|lisle|egyptian)\b",
    "pattern": r"\b(stripe|striped|polka|dot|geometric|argyle|houndstooth|check|"
               r"floral|print|solid|novelty|leopard)\b",
}


def insert(conn, rows):
    """Append rows. No de-duplication on purpose — see below."""
    conn.executemany(
        """
        INSERT INTO keywords
        (source, query, landing_page, impressions, clicks, position, captured_at)
        VALUES (:source, :query, :landing_page, :impressions, :clicks, :position, :captured_at)
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def load_gsc(conn, folder):
    """
    Load Queries.csv and Pages.csv from a Search Console export folder.

    Both files, not just queries. Pages.csv is what turns a keyword list into a
    work queue: it says WHICH URL is already ranking and at what position, and
    a page sitting at 14 with impressions and no clicks is a title problem
    someone can act on today.
    """
    folder = Path(folder)
    loaded = 0

    queries = folder / "Queries.csv"
    if queries.exists():
        rows = []
        for r in csv.DictReader(queries.open(encoding="utf-8-sig")):
            rows.append({
                "source": "gsc_query",
                "query": r["Top queries"].strip().lower(),
                "landing_page": None,
                "impressions": int(r["Impressions"]),
                "clicks": int(r["Clicks"]),
                "position": float(r["Position"]),
                "captured_at": NOW,
            })
        loaded += insert(conn, rows)
        print(f"  gsc_query : {len(rows)} queries")

    pages = folder / "Pages.csv"
    if pages.exists():
        rows = []
        for r in csv.DictReader(pages.open(encoding="utf-8-sig")):
            rows.append({
                "source": "gsc_page",
                "query": None,
                "landing_page": r["Top pages"].strip(),
                "impressions": int(r["Impressions"]),
                "clicks": int(r["Clicks"]),
                "position": float(r["Position"]),
                "captured_at": NOW,
            })
        loaded += insert(conn, rows)
        print(f"  gsc_page  : {len(rows)} pages")

    return loaded


def load_onsite(conn, path):
    """Shopify's own search box. Columns: 'Search query', 'Searches'."""
    rows = []
    for r in csv.DictReader(Path(path).open(encoding="utf-8-sig")):
        rows.append({
            "source": "onsite",
            "query": r["Search query"].strip().lower(),
            "landing_page": None,
            # Stored in the impressions column deliberately. A site search IS
            # an impression of intent, and one column means every downstream
            # "order by demand" query works across all three sources without
            # special-casing which one it came from.
            "impressions": int(r["Searches"]),
            "clicks": None,
            "position": None,
            "captured_at": NOW,
        })
    n = insert(conn, rows)
    print(f"  onsite    : {n} queries")
    return n


def load_auto(conn, path):
    """Autocomplete phrases, one per line. Blank lines and #comments ignored."""
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip().lower()
        if not line or line.startswith("#"):
            continue
        rows.append({
            "source": "autocomplete",
            "query": line,
            "landing_page": None,
            # NULL, not 0. Autocomplete carries no volume, and a zero would
            # sort it alongside genuinely dead terms rather than marking it as
            # a different KIND of evidence.
            "impressions": None,
            "clicks": None,
            "position": None,
            "captured_at": NOW,
        })
    n = insert(conn, rows)
    print(f"  autocomplete: {n} phrases")
    return n


def vocabulary(conn, limit=40):
    """
    The catalog-wide phrases worth writing toward, strongest evidence first.

    Ordered by real impressions, so a Google query with 700 impressions
    outranks an autocomplete phrase with none. That ordering IS the trust
    hierarchy — it is not a stylistic choice.

    Called by generate.py, once per run, and rendered into every prompt.
    """
    rows = conn.execute(
        """
        SELECT query, source, MAX(impressions) AS impressions, MIN(position) AS position
        FROM keywords
        WHERE query IS NOT NULL AND query <> ''
        GROUP BY query
        ORDER BY (impressions IS NULL), impressions DESC
        LIMIT :limit
        """,
        {"limit": limit},
    ).fetchall()
    return rows


def page_rank_for(conn, handle):
    """
    What Search Console says about this product's own URL, or None.

    A product already ranking at position 14 with impressions and no clicks is
    not a blank page needing copy — it is a page whose title is failing at the
    last step. Telling the model that changes what it should write.
    """
    row = conn.execute(
        """
        SELECT impressions, clicks, position
        FROM keywords
        WHERE source = 'gsc_page' AND landing_page LIKE :like
        ORDER BY impressions DESC LIMIT 1
        """,
        {"like": f"%/products/{handle}%"},
    ).fetchone()
    return row


def show(conn):
    """What is loaded, and what it says about how people search."""
    print("LOADED")
    for r in conn.execute(
        "SELECT source, COUNT(*) n, SUM(impressions) imp FROM keywords GROUP BY source"
    ):
        imp = f"{r['imp']:,} impressions" if r["imp"] else "no volume data"
        print(f"  {r['source']:<13} {r['n']:>5} rows   {imp}")

    rows = conn.execute(
        "SELECT query, impressions FROM keywords "
        "WHERE query IS NOT NULL AND impressions IS NOT NULL"
    ).fetchall()
    total = sum(r["impressions"] for r in rows) or 1

    print("\nWHAT KIND OF WORDS CARRY THE DEMAND")
    for name, pattern in GROUPS.items():
        rx = re.compile(pattern, re.I)
        share = sum(r["impressions"] for r in rows if rx.search(r["query"]))
        print(f"  {name:<9} {share:>7,} ({100 * share / total:>4.1f}%)")

    print("\nTOP PHRASES (strongest evidence first)")
    for r in vocabulary(conn, 25):
        vol = f"{r['impressions']:>6,}" if r["impressions"] else "     —"
        pos = f"pos {r['position']:>5.1f}" if r["position"] else "         "
        print(f"  {vol}  {pos}  {r['source']:<13} {r['query'][:48]}")

    print("\nPAGES RANKING 8-20 WITH NO CLICKS — the winnable ones")
    for r in conn.execute(
        """
        SELECT landing_page, impressions, position FROM keywords
        WHERE source = 'gsc_page' AND position BETWEEN 8 AND 20 AND clicks = 0
        ORDER BY impressions DESC LIMIT 10
        """
    ):
        page = r["landing_page"].replace("https://www.dynamocks.us", "")
        print(f"  {r['impressions']:>5} imp  pos {r['position']:>4.1f}  {page[:64]}")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "show"
    conn = db.connect()
    db.init_schema(conn)

    if mode == "show":
        show(conn)
    elif mode == "gsc":
        load_gsc(conn, sys.argv[2])
    elif mode == "onsite":
        load_onsite(conn, sys.argv[2])
    elif mode == "auto":
        load_auto(conn, sys.argv[2])
    else:
        print(__doc__)
        sys.exit(1)

    conn.close()
