# Collection copy — v1

Category pages, not product pages. A different job with different evidence.

## Why this file exists

Search Console, 3 months: **product pages carry 22,973 impressions.
Collections carry 270.**

Every competitor ranking for a head term does it with a collection page —
adidas, Nike, Darn Tough and Sealskinz all rank `/collections/…` for
`ankle length socks`. This store answers with a single product, sits at
position 9, and gets **zero clicks**, because somebody typing a category term
wants to browse and a product page is the wrong answer.

`ankle socks` is 1,503 impressions. `ankle length socks` is 1,173. Those are
collection queries. One good collection page reaches more demand than fifty
good product pages.

---

## The grounding is different — read this first

A collection has **no photograph of itself, no fibre composition and no SKU**.
What it IS is the set of products inside it.

You are given `member products` — up to 30 real product titles from the page —
and `products_count`, the true total.

**Those titles are your only evidence.** Read them and describe what a shopper
would actually find on the page.

- If the members are all polka-dot socks, it is a polka dot collection,
  whatever the page is called.
- If the members have nothing in common, say so plainly in the description
  rather than inventing a theme. "Cotton Excellence" holds 137 products with
  no shared trait; the honest answer is a broad cotton-socks page, not a
  curated story that does not exist.
- **The collection's own name is the weakest signal in the room.** This store
  names collections for merchandising, not for search: *Dotty Delight*,
  *Savvy Stripes*, *Solid Steps*, *Cotton Excellence*, *Monochrome Magic*.
  Those are shelf names. Nobody types them.
- **Check the handle.** It is often more accurate than the title —
  "Dotty Delight" lives at `/collections/polka-dot-parade`, and the handle is
  the better description of the two.

**Never invent a product count.** Use `products_count` as given, or omit it.

---

## What people search for a category page

Measured on this store, 3 months:

| word type | share of impressions |
|---|---|
| **length** — ankle, short, crew | **58%** |
| **colour** — green, purple, mango, white | **51%** |
| material — cotton, bamboo | 8% |
| pattern — striped, polka, geometric | 2.5% |

The real head terms, with their impressions:

```
1,503  ankle socks
1,173  ankle length socks
  770  white ankle socks
  725  green short socks
  648  green ankle socks
  238  neon socks
  160  cotton ankle socks
  153  crew length socks
```

**One correction that matters more than any other on this store.** The
collection titled **"Quarter Length Socks"** holds 102 products and lives at
`/collections/cotton-ankle-socks`. In three months, `quarter` got **zero
impressions** and `ankle` got **6,693**. The page is named for a word nobody
types. Where the members are ankle socks, call them ankle socks.

Other vocabulary the data supports:

- **"short socks"** — 1,471 impressions, and no page on this store uses it.
- **"gray"** as well as **"grey"** — the audience is American.
- **"for men" / "for women"** is the most common modifier in this market.
  Use it where the members support it. "for Men & Women" is true by default
  for this catalog and is stated in its own product copy.
- **`dress socks`** — the store's best-selling line and the segment it is most
  invisible for. Where a collection holds dress socks, say **Dress Socks for
  Men**, not "Crew Socks".

---

## seo.title — the blue link

**Shape:** `<Colour or Motif> <Length> Socks for Men & Women | Dynamocks`

- **Under 60 characters.** Google truncates near there.
- **Lead with what people type**, not with the shelf name.
- Plural and browsable. A category page promises a *range*, not one item.
- Drop `| Dynamocks` before you drop the keyword.

| Current | Better |
|---|---|
| *(none)* — "Dotty Delight" | Polka Dot Socks for Men & Women \| Dynamocks |
| *(none)* — "The Geometrics Collection" | Geometric Print Socks \| Crew & Ankle \| Dynamocks |
| *(none)* — "MONOCHROME MAGIC" | Black & White Socks for Men & Women \| Dynamocks |
| "Colorful Cotton Ankle Socks for Men & Women \| Colorful Unisex Styles" | Cotton Ankle Socks for Men & Women \| Dynamocks |
| "Shop Artistic Cotton Socks: Ankle & Crew \| Unisex Styles" | Artistic Print Cotton Socks \| Crew & Ankle \| Dynamocks |

Note the fourth row: the existing title says "Colorful" twice. Repetition
inside one title wastes the budget it is competing for.

## seo.description — the click

- **Say what is on the page and roughly how much of it.** "Over 100 cotton
  ankle socks in 30+ colors" answers the question a browser is asking.
- **Name two or three concrete things a shopper would recognise** — colours,
  patterns, lengths — taken from the member titles.
- **Give a reason to click over the nine results above it.** Range, price,
  free shipping over $50, gift box — whatever the store data supports.
- Under about 155 characters, or Google cuts it.
- No filler. Not "Experience the ultimate", not "Elevate your".

---

## Do not invent facts

- **Product count:** from `products_count` only.
- **Colours and patterns:** only those you can see in the member titles.
- **Material:** only where the member titles state it — cotton, bamboo.
- **Price and shipping:** never, unless given.
- **Gender:** "for Men & Women" is the catalog default and is safe. Narrow to
  "for Men" only where the members are genuinely a men's line, such as the
  dress and executive ranges.

---

## Output format

Three labelled lines, nothing else — no preamble, no markdown fences:

```
grounding: <what the members actually are, and which search term you aimed at>
seo_title: <the meta title>
seo_description: <the meta description>
```

`grounding` first, so the reasoning shapes the answer rather than excusing it.
Name the member products you read and the phrase you chose.

Good:

> grounding: All 30 sampled members are polka-dot crew and ankle socks —
> Fizzy Aqua, Bubbles, Confetti, Plus. The page is called "Dotty Delight",
> which nobody types; the handle says polka-dot-parade and agrees with the
> members. Aimed at "polka dot socks" rather than the shelf name.

Useless:

> grounding: This is a nice collection of socks.

If the members share nothing describable and the handle gives no clue:

```
needs_human: <one line saying what could not be resolved>
```
