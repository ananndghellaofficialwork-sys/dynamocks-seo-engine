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
- **Nothing that generates output may also judge it.** Candidates come from
  one set of models, scores come from a model outside that set.

## Documentation — every function, no exceptions

Every function opens with a docstring covering **three sections, in this
order**. Written so someone who has never seen the file understands the
*purpose* before reading a line of code.

### 1. WHAT IT DOES

The mechanics in plain language, then two lines placing it in the system:

- **Called by:** which function calls this, and how often — once per run,
  once per product, once per field. The frequency is part of the design.
- **In the pipeline:** what flows in → this function → what flows out,
  naming the files and functions on either side.

Someone should be able to read this section alone and know where the
function sits without opening another file.

### 2. WHY IT IS ITS OWN FUNCTION

Why this is separate rather than inlined into its caller or folded into a
neighbouring function.

**Name the alternative that was rejected, and what would have gone wrong
if we had taken it.** "Splits out the parsing" is worthless. "The
alternative was parsing inline in the orchestrator; models change their
output shape and this is the part most likely to break, so it gets one
function to fix rather than surgery on the loop" is the point.

If there is no real reason for it to be separate, **say so** — that is a
signal it probably should not be a function at all.

### 3. WHAT IT RETURNS, AND WHO CONSUMES IT

The return type and shape, including what `None` or an empty result means.

Then **trace the value forward**: which function receives it next, and what
that function does to it. Not just the type — the next transformation step.

### Worked example — match this shape

```python
def load_prompt(version: str) -> tuple[str, str]:
    """
    WHAT IT DOES:
      Reads prompts/listing-{version}.md off disk and hands back its text
      along with the version tag it was loaded from.

      Called by: generate_for_products(), once at the start of a run — not
                 once per product, because the prompt does not change
                 mid-run.

      In the pipeline: prompts/listing-v1.md (file on disk)
                         -> load_prompt()
                         -> build_message()   [the text]
                         -> save_proposal()   [the version tag]

    WHY IT IS ITS OWN FUNCTION:
      The rejected alternative was pasting the prompt directly into
      build_message() as a string literal. That would mean editing Python
      every time the copy rules change, and it would make the prompt
      invisible to git diff as prose. Keeping it as a file loaded by one
      function means the copy rules are versioned as their own artifact,
      and listing-v2.md can be added without touching any code.

    WHAT IT RETURNS, AND WHO CONSUMES IT:
      A tuple (prompt_text, version_tag).

      prompt_text goes to build_message(), which glues it to one product's
      real field values to form the message sent to the model.

      version_tag goes through to save_proposal(), which stores it on every
      proposal row — so when a batch of output turns out to be bad, the
      prompt that produced it is a query, not a guess.
    """
```

### Still true

**Do not restate the code.** "Increments the counter" is worthless.
"Counts rows so the operator can confirm the fetch actually landed data
before running the generator against an empty table" is the point.

Anything non-obvious gets a short inline comment saying **why**, not what.

Every file also opens with a module-level docstring explaining why the file
exists and what it is forbidden from doing.

> **Placement note — pick one and stay consistent.** This rule specifies a
> `"""..."""` docstring as the first thing inside the function. `db.py`
> currently uses `#` comment blocks *above* each function instead. Both
> read fine; only the docstring form is reachable from `help()` and by
> tooling. Decide once, then bring `db.py` into line as its own commit.

## Function order — top-down, in call order

Functions are laid out in the order they are called, so the file reads
top to bottom like prose.

If the module has a `main()`, it goes first, then whatever `main()` calls,
in the order it calls them. **Library modules with no entry point** — `db.py`,
`generate.py` — start with the function the REPL calls first and follow the
same rule from there.

Then their helpers, depth-first. Private helpers (`_name`) sit directly
under the function that uses them.

Where an `if __name__ == "__main__":` block exists it stays at the bottom.

Never alphabetical, never public-then-private. Someone reading the file
from line 1 should follow the flow of control without scrolling back.

## Working style

- **One module per session. Do not write ahead.**
- Standard library first. No new dependency without asking.
- No argparse, logging framework, ORM, or dataclasses unless asked for.
- SQL uses Allman brackets — opening bracket on its own line, aligned
  with the closing bracket.
- Show the file and stop. **Do not commit.**
- **Verify against the artifact, never the exit code.** A script printing
  `done` is not evidence. Read the rows back, count them, look at them.

## Environment

- macOS. Tag every instruction **TERMINAL** or **IDE/UI**, and flag
  Mac-vs-Windows differences where they exist.
- Secrets live in `.env`, never committed. `.env.example` documents the keys.
- **Model access is config, not code.** `SEO_MODEL` names the model as
  `provider:model_id`; the dispatcher splits on the **first colon only**,
  because some providers' model ids contain slashes. An unknown provider or
  a missing key raises — it never falls back to another provider silently.
- **Shopify Admin GraphQL returns HTTP 200 with an `errors` array.**
  Check `body["errors"]` — `raise_for_status()` alone will pass a
  completely failed query.
- SQLite has foreign keys **off** by default. `PRAGMA foreign_keys = ON`
  per connection, or the `REFERENCES` clauses do nothing.
