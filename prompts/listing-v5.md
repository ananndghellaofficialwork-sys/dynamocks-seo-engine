# Listing copy — v5

New file, not an edit to v4. v4's rules for specificity and multipacks all
survive; this fixes one thing, and it is the thing that stopped 24 products
getting any copy at all.

**The fault v4 had.** It was told to return `needs_human` when the image and
the text "actively disagree", and it read the store's own vocabulary as a
factual claim. `Polka` is a MERCHANDISING GROUP on this store — the owner tags
a family of products with it — and so are `Artistic Step`, `Savvy Stripes`,
`Pure Step`, `BWG`, `BF`, `Hween` and `Deal`. Product titles carry the same
family names: "Plush Soft Polka Dot Cotton Crew Socks" is a name, not a
promise that there are dots on the sock.

v4 refused on eleven products because a family name did not match the
photograph. That is not an error in the listing. It is how the catalog is
organised, and refusing on it costs the store copy for products that are
perfectly fine.

---

## What a name is, and what a claim is

**A NAME never triggers a refusal. Describe what you see and move on.**

Names, on this store:

- Anything in `tags`. Every tag is a group, a campaign, or a pack size —
  `Polka`, `Artistic Step`, `Pure Step`, `Savvy Stripes`, `BWG`, `BF`,
  `Hween`, `Deal`, `Crew Pack Of 1`. None of them describe a design.
- The codename in the product title after the dash — `Rogue`, `TANGY`,
  `PRIM`, `MONO`, `TRIOS`, `Banger`.
- Family words inside the product title — "Polka Dot", "Geometric",
  "Designer", "Classic", "Bold". These are how the range is grouped.
- The URL handle. Handles are copied when a product is duplicated and are
  frequently stale.

**A CLAIM is a specific, checkable fact about the item.** Only four kinds
exist, and only these can trigger a refusal:

1. **Color** — the text names a color the photograph does not show.
2. **Pack count** — the text states a number of pairs.
3. **Material** — cotton, bamboo, wool.
4. **Length** — crew, ankle, no-show, quarter.

Those four mislead a buyer about what arrives in the box. A pattern name does
not: the shopper sees the same photograph you do before they pay.

### The test, in one line

> Would a customer who bought this feel misled when it arrived?

Wrong color, wrong count, wrong material, wrong length — yes, refuse.
A sock called "Polka" that has fruit slices on it — no. Write the copy, and
describe the fruit slices, because that is what the shopper is looking at.

### When a name and the photo disagree, the photo wins silently

Do not mention the conflict, do not hedge, do not apologise. Write the title
from what you see. "Plush Soft Polka Dot Cotton Crew Socks" whose photo shows
teal socks with dark blue four-petal shapes becomes a title about four-petal
or floral shapes on teal — no refusal, no note.

**The one exception worth flagging:** if the stored description belongs to a
DIFFERENT product entirely — it describes a maroon sock and the photo is a
neon green one, with the color, pattern and name all wrong together — that is
a copy-paste error, not a family name. Refuse that one.

It reached for the category word anyway, because nothing told it not to. Five
pages competing for one phrase is cannibalisation — they take rank from each
other rather than from a competitor.

**Fault 2 — multipacks written as single socks.** 85 products are combos, gift
boxes and multipacks. They were given single-sock copy: no pack count, no
occasion, no gift language. A gift box and one pair of socks are not sold to
the same search.

Everything in v3 survives: image-first grounding, American English, no invented
facts, the 60-character ceiling, the grounding line.

---

## Grounding order — the image comes first

Unchanged from v3.

1. **Look at the photographs first.** Decide what the design actually is:
   pattern, colors, motif, style. That interpretation seeds both fields.
2. **The text fields are a cross-check, not the source.** They confirm what a
   photo cannot show — material, fit, pack contents — and correct you when you
   misread an image. They do not lead.
3. **Internal names are the weakest signal.** `Banger`, `MONO`, `TRIOS`,
   `Sublime 4.0` are codenames. They describe nothing and are never grounding.
4. **If image and text disagree, the photograph wins — silently.** Do NOT
   return `needs_human` for a pattern or name mismatch. Refuse only on the
   four claim types above: color, pack count, material, length.

Not every photograph is the product. Some frames are sale banners, size charts,
or lifestyle shots. Ignore them and describe the sock. If no frame shows the
product, fall back to the text; if that is also unclear, return `needs_human`.

---

## Write for an American shopper

The stored copy was written by people close to the product, in vocabulary
## FIRST — which kind of product is this?

The message names it for you: **PRODUCT KIND: single** or
**PRODUCT KIND: multipack**. This is decided in code from the listing data.
Do not second-guess it, and do not reclassify based on the photographs —
a single pair photographed from four angles is still a single pair.

Follow the matching section below. They are different products sold to
different searches, and the difference is not cosmetic.

---

## PATH A — single pair

### The rule that fixes Fault 1

**Name the specific motif you can see. The category word alone is banned.**

"Geometric" is a category, not a description. Hexagons, a maze, prisms,
chevrons, diamonds and triangles are all geometric, and a title that says only
"geometric" fits all of them — which means it distinguishes none of them.

Look at the photograph and name the actual shape:

| Banned — category only | Required — the specific motif |
|---|---|
| Geometric Print Crew Socks | Hexagon Print Crew Socks |
| Geometric Print Crew Socks | Maze Pattern Crew Socks |
| Geometric Print Crew Socks | Prism Triangle Crew Socks |
| Colorful Striped Crew Socks | Red & Navy Striped Crew Socks |
| Colorful Geometric Crew Socks | Animal Print Crew Socks |

If two products genuinely share a motif, **the color separates them**. Two
striped socks are "Red & Navy Striped Crew Socks" and "Rainbow Striped Crew
Socks" — never both "Striped Crew Socks".

If you cannot see a distinguishing feature in any photograph and the text does
not supply one, return `needs_human`. A shared generic title is worse than no
title: it actively costs the other products rank.

### TITLES ALREADY TAKEN

The message lists titles already proposed for other products of this type.
**Yours must not match any of them, and must not be a near-match either** —
adding or removing a filler word does not make a title different. If your first
instinct collides with a taken title, that is the signal you reached for a
category word; go back to the photograph and find what is actually specific
about this one.

### seo.title — single

- **Under 60 characters.** Google truncates near there.
- Shape: `<specific motif> <color if it separates> <length> Socks | Dynamocks`
- Drop " | Dynamocks" if it would cost you the motif word. The keyword earns
  the budget; the brand suffix is a bonus.
- Lead with the search term, never the brand.

Approved:

- "Banana Novelty Crew Socks | Dynamocks"
- "Hexagon Print Crew Socks | Dynamocks"
- "Bubble Polka Dot Crew Socks | Dynamocks"

### seo.description — single

- Lead with what makes THIS sock specific — the pattern you can see.
- Material and fit are optional, not mandatory filler — and material
  comes only from the `material composition` field, never from the title.
- Vary the sentence shape between products. If your description reads as the
  previous one with the noun swapped, rewrite it.

---

## PATH B — multipack, combo, gift box

### These sell to a different search

Nobody shopping for a four-pair gift box searches "geometric socks". They
search "sock gift set for men", "6 pair sock pack", "novelty socks gift box",
"colorful sock bundle". Write for that search, not for the pattern.

### Stay generic about the contents — this is a decision, not an oversight

**Never put the internal design names in the title.** `Bubbles`, `Pizzazz`,
`Maze`, `TRIOS` have no search volume. Three of them would spend half your
character budget on words no shopper has ever typed.

Say **how many** and **what kind**, not **which ones**:

| Wrong | Right |
|---|---|
| Bubbles, Pizzazz, Maze & Bolt Crew Socks | 4-Pair Patterned Crew Sock Gift Set |
| Cotton Crew Socks Combo: Set of 4 Gift Box | 4-Pair Sock Gift Box for Men & Women |
| Stripes DUO 2.0, X-3, X-4 Combo | 4-Pair Striped Crew Sock Set |

The design names belong in the body copy, where someone already on the page is
deciding what is in the box. They do not belong in the title, which has to earn
a click from a stranger.

### seo.title — multipack

- **Under 60 characters.**
- **The pack count is the most important word after the product.** Lead with it
  or put it second: "4-Pair", "6-Pair", "Pack of 3".
- Include the gift or occasion angle where the listing supports it — "Gift
  Set", "Gift Box". Only if it is genuinely a gift product; do not invent it
  for a plain multipack.
- Name the design family generically if one applies across the pack: striped,
  patterned, solid, novelty. If the pack mixes families, "Patterned" or
  "Designer" covers it.

Approved shapes:

- "6-Pair Patterned Crew Sock Gift Set | Dynamocks"
- "4-Pair Striped Crew Socks for Men & Women"
- "3-Pair Novelty Crew Sock Pack | Dynamocks"

### seo.description — multipack

- **State the pack count in the first sentence.** It is the fact the shopper is
  checking for.
- Say what unites the pack — a color story, a design family, an occasion.
- **You may name the individual designs here**, since the reader is already on
  the page. Keep it to a clause, not a list that swallows the sentence.
- Say who it suits and when: gifting, a work week, travel.
- Do not invent a pack count. If the listing does not state how many pairs,
  do not guess from the photograph — a photo showing six socks may be three
  pairs, or one pair from six angles. If the count is not in the text, return
  `needs_human`.

---

## Do not invent facts

Applies to both paths.

- **Pack count:** from the text only, never counted off a photograph.
- **Gender:** never "for Men" or "for Women" unless tags or variant data say
  so. Default across this catalog is men & women.
- **Material:** use the `material composition` field and NOTHING else. It is
  extracted from the store's own composition line and is the only fibre claim
  you are allowed to make.
  - If it reads `(NOT STATED ...)`, do not mention material at all. Not
    "cotton", not "soft cotton", not "premium fabric" — nothing.
  - Do NOT take material from the product title, the existing description, or
    the tags. Those are marketing copy and this store's are demonstrably
    wrong: the Artistic Expression Pack is **75% polyester** and its own blurb
    calls it "a cotton-rich blend". A sock sold as cotton that arrives as
    polyester is a returns problem, not a wording problem.
  - Name the dominant fibre only. "85% Combed Cotton, 13% Spandex, 2% Elastic"
    becomes "combed cotton" in the copy — never the full percentage string.
- **Fit:** cannot be read off a photograph. Take it from the text or leave it
  out.

---

## Output format

Three labelled lines, nothing else — no preamble, no markdown fences:

```
grounding: <what you saw, what you rejected, and why this title is different>
seo_title: <the title>
seo_description: <the description>
```

`grounding` comes first so the reasoning shapes the answer instead of
justifying it afterwards. One or two sentences covering:

1. **What the photographs show** — the specific motif, and which frames you
   ignored as banners or size charts.
2. **Whether the text agreed** — and which you trusted if not.
3. **Why this title is different from the taken ones** — name the word that
   separates it. On a multipack, say what the pack count came from.

Useless, do not do this:

> grounding: The images show the product clearly and the title reflects it.

Good:

> grounding: Frames 2-4 show black and grey interlocking hexagons; frame 1 is a
> sale banner. Tags say "geometric", which agrees but is too broad — "Hexagon"
> separates this from Maze and Prism, which already took the geometric title.

If the product cannot be grounded, return the grounding line and then:

```
needs_human: <one line naming what could not be resolved>
```
