# The Build Loop

How one module gets built. Same six steps every file, every project.

The point is not ceremony. It is that **the design stays mine while the
typing gets delegated** — and that I can debug this code at 11pm in six
months without re-reading all of it.

---

## 1. One sentence — what this file is for, and what it must never do

> `fetch.py` pulls the store into SQLite. It never writes to Shopify,
> and it never writes SQL itself.

The **never** half is the important half. It is what stops a module
quietly growing a second job. If I cannot write the sentence, the module
has no boundary yet and the code will not have one either.

## 2. List the functions before any code exists

Name, what goes in, what comes out. On paper.

| Function | Takes | Returns |
|---|---|---|
| `count_products(conn)` | connection | `int` |

**If I cannot name the return value, I do not understand the function
yet.** That is cheap to find out now and expensive after it is written.
This is also where the "does it need X?" questions get answered as
decisions rather than discussions.

## 3. Write the prompt with three things

1. **Scope** — this file only. Name the files it must NOT create.
2. **The function list** from step 2.
3. **What NOT to add** — no argparse, no logging framework, no ORM,
   no new dependencies.

Item 3 is the one that gets skipped, and it is why generated code arrives
carrying three abstractions nobody asked for. Standing rules live in
`CLAUDE.md` so they do not have to be retyped.

End with: **show me the file and stop. Do not commit.**

## 4. Read it top to bottom, once. Mark what I cannot explain

Do not fix anything yet. The marks are the list for step 6.

Reading generated code produces the *feeling* of understanding without
the evidence of it. The marks are what convert one into the other.

## 5. Exercise it in the REPL, against real data

Pick the one behaviour that could be **silently wrong** and prove it with
my own eyes.

```python
>>> db.upsert_product(c, row); c.commit()
>>> db.count_products(c)          # 1
>>> row["title"] = "CHANGED"
>>> db.upsert_product(c, row); c.commit()
>>> db.count_products(c)          # still 1 -> ON CONFLICT updates, not duplicates
```

A script that exits cleanly has proved nothing. `python db.py` printed
`done` while writing to Monday's schema. **Verify against the artifact —
the table, the store, the file — never against the exit code.**

## 6. Explain-back cold, then commit

Close the file. Answer the step-4 marks out loud, from memory. A fuzzy
answer names the exact line to go read — not the whole file.

Then commit, with a message saying **why**, not what.

---

## The rule underneath all six

**Decide, write it where it repeats, move.**

A good decision applied by hand once is a one-off. The same decision
written into `CLAUDE.md`, the design doc, or a reusable prompt is a
system. The difference between a designer and an architect is entirely
in where the decision gets stored.

## And the one about pace

Slow per file is fine. **Unfinished is not.**

Measured record on this project: work done inside a booked session ships
8 times out of 8. Work left as homework ships 0 times out of 10. So the
constraint is not speed — it is that **every session ends with something
committed**, however small.
