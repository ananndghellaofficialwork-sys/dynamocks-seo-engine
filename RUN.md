# Run order

Every command below is safe to interrupt and re-run. Each stage skips work
already done, so a restart resumes rather than repeats.

Nothing here writes to the live store. `push.py` is the only module that can,
and it is not in this list yet — it runs after your wife's review comes back.

---

## 0. One-time migrations

Only needed once. Harmless to re-run — SQLite errors with `duplicate column
name` and changes nothing.

```bash
sqlite3 data/seo.db "ALTER TABLE products  ADD COLUMN images TEXT;"
sqlite3 data/seo.db "ALTER TABLE products  ADD COLUMN body_html TEXT;"
sqlite3 data/seo.db "ALTER TABLE products  ADD COLUMN material TEXT;"
sqlite3 data/seo.db "ALTER TABLE products  ADD COLUMN delisted_at TEXT;"
sqlite3 data/seo.db "ALTER TABLE proposals ADD COLUMN grounding TEXT;"
sqlite3 data/seo.db "ALTER TABLE proposals ADD COLUMN reviewer_note TEXT;"
```

Check `.env` has all four keys:

```
SHOPIFY_STORE=  SHOPIFY_TOKEN=  SHOPIFY_API_VERSION=2025-07
GEMINI_API_KEY=      # writes the copy
ANTHROPIC_API_KEY=   # judges the copy — must be a different family
SEO_MODEL=gemini:gemini-2.5-flash
```

---

## 1. Is caching on?

```bash
python3 check_cache.py
```

Costs a few cents. Two identical-prefix calls per provider; the second should
report tokens read from cache. Worth running once before a 300-product batch,
because a broken cache fails silently and only shows up on the bill.

---

## 2. Pull the catalog

```bash
python3 fetch.py
```

Read-only against Shopify. Mirrors products, all photos, body copy and the
fibre composition. Products missing from the store are marked `delisted`, not
deleted — `proposals` and `pushes` reference them and `pushes` is the undo log.

Expect: `done — N live products in db`, plus any delisted count.

---

## 3. See where things stand

```bash
python3 status.py
```

Reads only. Every number comes off the database, not from memory of which
scripts were run. It also names the work that is NOT in the database —
collections, H1, body copy, alt text — because a silent omission reads as
zero work remaining.

---

## 4. Generate the copy

Preview first — writes nothing:

```bash
python3 preview.py 5
open preview.html
```

Then for real, in batches:

```bash
for i in $(seq 1 13); do
  python3 -c "import db, generate; c=db.connect(); \
    generate.generate_for_products(c, limit=25, \
    model_ref='gemini:gemini-2.5-flash', prompt_version='v5'); c.close()"
  sleep 30
done
```

Repeat until it prints `0 products need seo_title`. Failed products come back
automatically on the next round.

---

## 5. Fix what came out wrong

Each takes a dry run first. Add `--write` when the list looks right.

```bash
python3 regenerate.py duplicates      # two products competing for one phrase
python3 regenerate.py too_long        # titles over 60 chars
python3 regenerate.py prompt:v3       # copy from an older prompt version
python3 regenerate.py orphans         # repair broken supersede pointers
```

For the refusals, sit with it — it asks you what is true and remembers the
answer for every future run:

```bash
python3 resolve.py
```

---

## 6. Score it

```bash
python3 verify.py 10      # sanity check the scores first
python3 verify.py         # then all of them, ~40 min
```

Claude judges what Gemini wrote. Two axes, never averaged: **accuracy** (is it
true) and **search** (will anyone find it). Accuracy below 4 is a gate, not a
suggestion.

Watch for `scored WITHOUT a photo` — those have accuracy NULL because there was
nothing to check against, which is honest rather than a guessed 5.

---

## 7. Send it for review

```bash
python3 review.py
open review.html
```

Two files:

- **`review.html`** — photo beside current copy, proposed copy, scores and the
  model's reasoning. Sorted worst-first. Built for someone who does not use a
  terminal.
- **`review.csv`** — opens in Excel. Filter `CHECK_FIRST` to see only the rows
  that need a human.

---

## Not yet built

- **Importing her decisions** back from the CSV. `proposals` is append-only, so
  a verdict has to be written as a new row under that rule, not bolted onto the
  export.
- **`push.py` at batch scale.** It works and is proven against the live store
  on one product, with the undo row written before the write. It has not been
  run as a batch.
- **Collections.** 26 of 36 have no SEO title and one collection write reaches
  hundreds of products at once. Per `SEO-Field-Inventory.md` §I this is the
  highest revenue-per-hour target still untouched.
- **Image alt text.** ~600 photos blank. It is what Google reads for image
  search.
- **`DESIGN-v2.md` §6.1 is behind the schema** — it documents five tables and
  there are now seven (`seo_exclusions`, `scores`). The rule is that the design
  wins or the design changes first, deliberately, as its own commit. That commit
  has not been made.
