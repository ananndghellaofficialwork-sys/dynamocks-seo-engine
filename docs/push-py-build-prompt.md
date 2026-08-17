# `push.py` — build prompt

Written 2026-08-16. Component 5. The only module allowed to write to the store.

---

## 1. Scope for today — one product, not ten

`DESIGN-v2.md` §7 shows `push.py --live --limit 10`. **That's next session's shape.**
Today's done-condition (per this morning's plan): one product, one field, pushed live,
verified by re-read, then undone and re-verified. The batch loop (`--limit`, cap of 10)
is a thin wrapper around the same single-item function once that function is proven —
don't build it today.

**verify.py and review.py don't exist yet**, so there's no `status = 'approved'` gate
to select from. Today's stand-in for human approval is you naming the exact `gid` and
`field` on the command line — that *is* L0: human decides, human triggers, the code
only executes what it's told. `get_proposal()` below is written to be trivially
replaced by a status-based query once `verify.py`/`review.py` ship; it doesn't need to
anticipate that today.

## 2. Functions, in call order

| Function | Takes | Returns |
|---|---|---|
| `push_one(conn, gid, field, live)` | connection, product gid, `'seo_title'` or `'seo_description'`, bool | `dict` — summary of what happened (or would happen, if `live=False`) |
| `get_proposal(conn, gid, field)` | connection, gid, field | latest non-superseded proposal row as `dict`, or `None` |
| `fetch_live_field(gid, field)` | gid, field | `(value: str \| None, store_updated_at: str)` |
| `write_undo_row(conn, proposal_id, batch_id, before_value, autonomy_level)` | as named | `push_id: int` |
| `push_field_to_shopify(gid, field, new_value)` | gid, field, new value | raw API response `dict` |
| `verify_push(conn, push_id, gid, field, expected_value)` | as named | `bool` — matched on re-read |
| `undo_push(conn, push_id)` | connection, push_id | `bool` — reverted and re-verified |

**Why `push_one` and not straight into a batch loop:** the batch is `push_one` called
up to 10 times with a shared `batch_id` — that's the rejected alternative to writing
loop logic and single-item logic tangled together in one function. Get the single-item
path right and provably safe first; the loop is then almost free.

**Why `fetch_live_field` reads Shopify directly instead of trusting the local `products`
mirror:** the mirror can be stale — that's the exact failure `DESIGN-v2.md` §6.4 exists
to prevent. `push_one` calls it twice: once before the write, to snapshot the real
`before_value` and check for staleness (compare against the `store_updated_at` captured
when the proposal was generated — abort and mark `stale` if it moved); once after, to
verify. Never assume the second read will match just because the first API call
returned 200.

## 3. The Claude Code prompt — paste this

> Write `push.py` for the dynamocks-seo-engine repo.
>
> **Read first, and follow them:** `CLAUDE.md` (standing rules — this is the ONLY
> module allowed to write to the store; undo row before the live write, always;
> docstring format; functions in call order top-down), `docs/DESIGN-v2.md` §6.1
> (schema — `pushes` table), §6.4 (staleness guard), §7 (the six commands), `db.py`
> (existing connect/init_schema pattern), `generate.py` (the `proposals` rows this
> file consumes).
>
> **What this file is for, in one sentence:** it takes one proposal already sitting in
> `proposals`, writes an undo row into `pushes` *before* touching Shopify, calls
> `productUpdate`, then re-reads the live product to confirm the write actually landed.
> **It never generates copy and it never calls a model.** It is the only file in this
> repo allowed to write to the store.
>
> **Functions, in this order:**
>
> 1. `push_one(conn, gid, field, live=False)` — the entry point, called from the REPL.
>    Calls `get_proposal`, then `fetch_live_field` to snapshot the current live value
>    and its `store_updated_at`. **Staleness check here:** compare that
>    `store_updated_at` against the value on the matching row in the local `products`
>    table — if the live one is newer, print why and stop, don't write. If `live` is
>    `False` (the default), print what it *would* write and return without calling
>    `write_undo_row` or `push_field_to_shopify` — a dry run must not touch the undo
>    log either, since no write is happening. If `live` is `True`: call
>    `write_undo_row` with the snapshotted before-value, THEN `push_field_to_shopify`,
>    THEN `verify_push`. Return a dict summarizing gid, field, before, after, verified
>    (bool), push_id.
>
> 2. `get_proposal(conn, gid, field)` — `SELECT` the most recent row from `proposals`
>    for this `gid` + `field` where `superseded_by IS NULL`, ordered by `id DESC`,
>    limit 1. Return it as a dict, or `None` if nothing exists. Today there is no
>    status gate to filter on (`verify.py`/`review.py` aren't built yet) — the caller
>    naming the exact gid on the command line **is** the approval step at L0. Note
>    that in the docstring so it's not mistaken for an oversight later.
>
> 3. `fetch_live_field(gid, field)` — Shopify Admin GraphQL query (not the local
>    mirror) for exactly this product's `seo { title, description }` plus
>    `updatedAt`. **Check `body["errors"]`** — a 200 with an errors array is a failed
>    query, per `CLAUDE.md`. Return `(value, store_updated_at)`.
>
> 4. `write_undo_row(conn, proposal_id, batch_id, before_value, autonomy_level="L0")`
>    — one `INSERT` into `pushes`: `proposal_id`, `batch_id`, `autonomy_level`,
>    `pushed_at` (ISO-8601 UTC, now), `before_value`, `after_value` (the proposal's
>    `proposed_value`), `api_response` NULL for now, `verified_at` NULL,
>    `rolled_back_at` NULL. Commits immediately — **this row must exist in the
>    database before the next function runs, not just be about to.** Return the new
>    row's id.
>
> 5. `push_field_to_shopify(gid, field, new_value)` — the only function in this entire
>    codebase that calls `productUpdate`. Build the GraphQL mutation for the one
>    field (`seo.title` or `seo.description`), send it, check `body["errors"]` the
>    same way as `fetch_live_field`, return the raw response dict.
>
> 6. `verify_push(conn, push_id, gid, field, expected_value)` — calls
>    `fetch_live_field` again (a fresh read, not the response from step 5) and
>    compares to `expected_value`. If it matches: `UPDATE pushes SET verified_at = ?,
>    api_response = ? WHERE id = ?`. If it does NOT match: leave `verified_at` NULL
>    and print a loud warning — **do not raise and crash; a failed verify is data,
>    not an exception.** Return `True`/`False`.
>
> 7. `undo_push(conn, push_id)` — reads `before_value` off the named row in `pushes`,
>    calls `push_field_to_shopify` to write it back, then calls `verify_push` again
>    to confirm the revert landed, then `UPDATE pushes SET rolled_back_at = ? WHERE
>    id = ?`. This is the function that makes "the undo log is written before the
>    write" a proven claim instead of an assertion — it must actually run once this
>    session, on the same product `push_one` just wrote.
>
> **Constraints:**
> - No `main()`, no `argparse`. I drive this from the REPL, same as `db.py` and
>   `generate.py`.
> - Default is dry run — `live=False` unless explicitly passed `True`. Do not add the
>   `--limit 10` batch loop this session; single-gid only.
> - Every function gets the `CLAUDE.md` docstring format: what it does (+ called by /
>   pipeline position), why it's its own function (+ rejected alternative), what it
>   returns and who consumes it. Module-level docstring at the top stating the "only
>   this file may write" rule explicitly.
> - No new dependencies — `requests`, `python-dotenv`, stdlib only.
> - **Do not** build the `--limit`/batch loop, `agent.py`, or anything touching
>   `product.title`, `price`, or `status` — those are permanently out of scope per
>   `DESIGN-v2.md` §2 and §3.
>
> Show me the file and stop. Do not commit.

## 4. How you check it — today's actual done-condition

```python
from db import connect
import push

conn = connect()

# 1. Dry run first — must print what it WOULD do, must NOT touch `pushes`.
before_count = conn.execute("SELECT COUNT(*) FROM pushes").fetchone()[0]
result = push.push_one(conn, gid="gid://shopify/Product/XXXXXXXXX", field="seo_title", live=False)
assert conn.execute("SELECT COUNT(*) FROM pushes").fetchone()[0] == before_count

# 2. Live, on ONE product only.
result = push.push_one(conn, gid="gid://shopify/Product/XXXXXXXXX", field="seo_title", live=True)
print(result)

# 3. Verify against the STORE, not this dict — Rule #8.
#    Open the product in the Shopify admin UI, or re-query, and read it with your own eyes.
row = conn.execute("SELECT * FROM pushes ORDER BY id DESC LIMIT 1").fetchone()
print(dict(row))   # verified_at must be non-null, before_value must be the OLD title

# 4. Exercise the undo. Don't just read the function — run it.
push.undo_push(conn, push_id=row["id"])
row2 = conn.execute("SELECT * FROM pushes WHERE id = ?", (row["id"],)).fetchone()
assert row2["rolled_back_at"] is not None
# Then check the live product again — it must show the OLD title, not the new one.
```

Four things to look for, matching today's five done-conditions from the morning plan:
1. The dry run changed nothing in `pushes` — `before_count == count after`.
2. `pushes.before_value` was written before `push_field_to_shopify` ran — true by
   construction if you kept the call order in `push_one`, but confirm it by reading
   the function, not assuming it.
3. `verified_at` is non-null after the live push — that's the re-read proving the
   write landed, not the API's 200.
4. After `undo_push`, the live product shows the OLD value again, confirmed by a
   fresh read — not by trusting `rolled_back_at` being set.

If #3 or #4 fail, that's a real bug in `push.py`, not a config issue — this is the
file with the least room for "close enough."
