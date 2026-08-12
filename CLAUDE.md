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

## Documentation — every function, no exceptions

Every function opens with a docstring (`"""..."""` as the first line inside
the function, not a `#` block above it) covering three things, in this order:

1. **Why this function exists** — the problem it solves, in plain business
   language. Not "wrapper around sqlite3.connect".
2. **What it does** — the steps, described functionally.
3. **What it returns** — type and meaning, including what `None` or an
   empty result signifies.

Written so someone who has never seen the file understands the *purpose*
before reading a line of code.

**Do not restate the code.** "Increments the counter" is worthless.
"Counts rows so the operator can confirm the fetch actually landed data
before running the generator against an empty table" is the point.

Anything non-obvious gets a short inline comment saying **why**, not what.

## Function order — top-down, in call order

Functions are laid out in the order they are called, so the file reads
top to bottom like prose.

`main()` first. Then whatever `main()` calls, in the order it calls them.
Then their helpers, same rule, depth-first. Private helpers (`_name`) sit
directly under the function that uses them.

The `if __name__ == "__main__":` block stays at the bottom — it is the
only part that executes on import order.

Never alphabetical, never public-then-private. Someone reading the file
from line 1 should follow the flow of control without scrolling back.

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
