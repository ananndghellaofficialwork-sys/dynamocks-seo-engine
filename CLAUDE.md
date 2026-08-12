# Dynamocks SEO Engine

Pipeline that generates and pushes SEO fields for a **live** Shopify store
(~370 products, ~36 collections, real revenue). The AI has no autonomy —
it earns it, one measured gate at a time.

## Source of truth

`docs/DESIGN-v2.md` — read it before writing code.
§6.1 schema · §6.2 proposal state machine · §6.3 priority formula ·
§6.4 staleness guard · §6.5 module list · §7 the six commands.

If code and design disagree, **the design wins** — or the design gets
changed first, deliberately, as its own commit.

## Hard rules

- `push.py` is the **only** module that may write to the store.
  Everything else is read-only by design, not by convention.
- `proposals` is **append-only**. Never UPDATE, never DELETE.
  A revision is a new row pointed at by `superseded_by`.
- The undo row in `pushes` is written **before** the live write, never after.
- **Staleness guard:** re-read at push time; if `store_updated_at` moved
  since the proposal was generated, abort that product and mark it `stale`.
  Someone editing the store by hand mid-run is the normal condition here.
- Default is dry run. `--live` is required to write. Hard cap 10 per batch.
- Never invent product facts. Copy is grounded in the live listing only.

## Working style

- **One module per session. Do not write ahead.**
- Standard library first. No new dependency without asking.
- No argparse, logging framework, ORM, or dataclasses unless asked for.
- SQL uses Allman brackets — opening bracket on its own line, aligned
  with the closing bracket.
- Show the file and stop. **Do not commit.**

## Environment

- macOS. Tag every instruction **TERMINAL** or **IDE/UI**, and flag
  Mac-vs-Windows differences where they exist.
- Secrets live in `.env`, never committed. `.env.example` documents the keys.
- **Shopify Admin GraphQL returns HTTP 200 with an `errors` array.**
  Check `body["errors"]` — `raise_for_status()` alone will pass a
  completely failed query.
- SQLite has foreign keys **off** by default. `PRAGMA foreign_keys = ON`
  per connection, or the `REFERENCES` clauses do nothing.
