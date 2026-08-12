# Dynamocks SEO Engine — Design v2

**Author:** Anand Ghela
**Date:** 7 August 2026
**Status:** Design frozen. Code starts 8 August.
**Supersedes:** `DESIGN.md` (v1, 4 Aug 2026)

---

## 0. The one-line summary

A human-controlled SEO pipeline for a live 472-product Shopify store, built so that an AI agent can **earn** autonomy one gate at a time — starting at zero.

---

## 1. Why v2 exists — the decision that changed

v1 assumed the goal was an autonomous agent that edits the store. **That assumption is now rejected.**

This is a real business with real revenue. An agent writing unreviewed copy to a live storefront is an unbounded risk taken for no commercial benefit. The correct sequence is the opposite of the interesting one:

> **Build the machine manually first. Automate the steps that have proven themselves. Autonomy is earned, never granted.**

Every component below is written so the *same code* serves both the manual and the automated path. Nothing gets thrown away when the agent arrives — the agent simply takes over steps a human is already performing with the same functions.

**This is the design principle the whole system is organised around, and it is the thing worth defending in a room.**

---

## 2. The autonomy ladder

The agent does not get permission. It gets promoted, and only against evidence.

| Level | Name | Who decides | Who writes | Promotion gate |
|-------|------|-------------|-----------|----------------|
| **L0** | **Manual** *(where we start)* | Human | Human | — |
| **L1** | **Assisted** | Human | Human, from generated candidates | 30 consecutive proposals where the human accepts the generated text with no edit or a trivial edit |
| **L2** | **Supervised** | Agent proposes, human approves each batch | Agent writes the approved batch | 3 consecutive batches of 10 pushed with zero rollbacks, and eval score ≥ 0.9 on all 30 |
| **L3** | **Autonomous, bounded** | Agent | Agent, within a hard blast radius | 90 days at L2 with no incident. Blast radius stays: max 10 products/run, `seo` fields only, never `title`, never `price`, never `status` |

**Rules that hold at every level:**

1. **Blast radius never widens with the level.** L3 is not "more products" — it is "fewer humans." Ten products per run is a permanent ceiling until there is a reason on paper to change it.
2. **Demotion is automatic.** Any rollback drops the system one level. No discussion.
3. **The undo log is written before the write, at every level, including manual.** An undo path that has never executed is not an undo path.
4. **`product.title`, `price`, `status` and inventory are out of scope forever.** The business owner owns those. The system touches `seo.title` and `seo.description` only.

> **Interview framing:** *"The agent's autonomy is a function of its measured track record, not of my confidence in it. I built the promotion criteria before I built the agent, because criteria written after the fact are just rationalisations."*

---

## 3. Scope — what gets generated

| Field | Current state | In scope | Why |
|-------|--------------|----------|-----|
| `seo.title` | **null on ~99% of the catalog** | ✅ Primary target | An empty, uncontested slot. Writing it cannot destroy existing copy. Highest value, lowest risk — this is where to start. |
| `seo.description` | Populated, but with **known duplicate clusters** | ✅ Secondary target | Duplicates across sibling products cause cannibalisation. Fixing them is measurable. |
| `product.title` | Owner-managed, recently improved by hand | ❌ Out of scope | Brand voice belongs to the business owner. |
| `price`, `status`, inventory | — | ❌ Out of scope, permanently | Not an SEO concern. Including them would make the blast radius unjustifiable. |

---

## 4. Tech stack — and what was rejected

| Layer | Choice | Why | Rejected alternative |
|-------|--------|-----|---------------------|
| Language | **Python 3.11+** | Existing codebase; the ecosystem is here | — |
| Store API | **Shopify Admin GraphQL API** (`productUpdate(product: ProductUpdateInput!)`) | The REST equivalent and `productUpdate(input:)` are deprecated — **verified against the live schema, not assumed** | REST Admin API — deprecated |
| State | **SQLite** (single file, stdlib) | Zero infrastructure, transactional, one file to back up, trivially inspectable with any SQL client. At 472 rows anything larger is theatre. | **Postgres** — operational overhead for a dataset that fits in RAM. **CSV** — no transactions, no state machine, no audit trail. |
| Similarity | **Embeddings + cosine similarity in-process**, vectors stored as SQLite BLOBs | Semantic near-duplicates ("Blue Geometric Crew Socks" vs "Geometric Blue Crew Socks") are invisible to string comparison and are the collisions that actually hurt. A brute-force scan over 472 vectors is sub-millisecond. | **Chroma / a vector DB** — earns its place at ~50k documents when filtered ANN search matters. At 472 it is a dependency that buys nothing. *Knowing when not to reach for it is the point.* |
| Generation | **LLM with a forced output schema** (Pydantic) | The schema is the runtime contract between generation and everything downstream — predictable shape on every call | Free-text prompting — unparseable, untestable |
| Review | **CSV export → business owner → CSV import** | She already works in spreadsheets. The tool should meet the reviewer where she is. | A web UI — a week of work to replace a file she can already sort and filter |
| Secrets | **`.env`, git-ignored, never committed** | Verified 7 Aug: `.env` has never entered git history | — |
| Version control | **Git, public repo** | github.com/ananndghellaofficialwork-sys/Clean-AI-Agent | — |

---

## 5. High-level design

```
                        ┌──────────────────┐
                        │  Shopify store   │  ← single source of truth
                        └────────┬─────────┘
                                 │ 1. FETCH (read-only)
                                 ▼
   ┌──────────────────────────────────────────────────────┐
   │                    SQLite: seo.db                    │
   │  products (mirror)   metrics (priority)              │
   │  proposals (append-only)   pushes (undo log)         │
   │  embeddings (BLOB vectors)                           │
   └───┬──────────────────────────────────────────────┬───┘
       │ 2. PRIORITISE                                │
       ▼                                              │
   ┌────────────────┐                                 │
   │  Work queue    │  revenue × missing-seo_title    │
   │  (top N)       │  × duplicate-cluster × in-stock │
   └───────┬────────┘                                 │
           │ 3. GENERATE (LLM, forced schema)         │
           ▼                                          │
   ┌────────────────┐                                 │
   │  Candidates    │──► 4. UNIQUENESS GATE ──────────┤
   └───────┬────────┘     cosine vs all live +        │ writes back
           │              vs others in batch          │ status
           │ 5. EVAL (rubric score)                   │
           ▼                                          │
   ┌────────────────┐                                 │
   │ review.csv     │──► 6. HUMAN APPROVAL ───────────┘
   └───────┬────────┘     (business owner)
           │ 7. PUSH — max 10 products, undo written first
           ▼
   ┌────────────────┐
   │ Shopify store  │──► 8. VERIFY: re-read, confirm, else rollback
   └────────────────┘
```

**The property that makes this recoverable:** `products` is a disposable cache, `proposals` is append-only, `pushes` is the undo log. Delete the database, re-fetch, and nothing of value is lost.

---

## 6. Low-level design

### 6.1 Schema

```sql
-- A MIRROR of the store. Overwritten on every fetch. Never edited locally.
CREATE TABLE products (
    gid                TEXT PRIMARY KEY,       -- gid://shopify/Product/...
    handle             TEXT NOT NULL,
    sku                TEXT,                   -- from first variant
    title              TEXT NOT NULL,
    product_type       TEXT,
    vendor             TEXT,
    tags               TEXT,                   -- JSON array
    status             TEXT,                   -- ACTIVE / DRAFT / ARCHIVED
    total_inventory    INTEGER,
    seo_title          TEXT,                   -- NULL on ~99% today
    seo_description    TEXT,
    store_updated_at   TEXT NOT NULL,          -- Shopify's updatedAt — the staleness guard
    fetched_at         TEXT NOT NULL
);

-- Prioritisation inputs, from sales_by_product export.
CREATE TABLE metrics (
    gid                TEXT PRIMARY KEY REFERENCES products(gid),
    revenue_12mo       REAL DEFAULT 0,
    units_12mo         INTEGER DEFAULT 0,
    priority_score     REAL,
    computed_at        TEXT
);

-- APPEND-ONLY. Never updated in place, never deleted. The audit trail.
CREATE TABLE proposals (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    gid                TEXT NOT NULL REFERENCES products(gid),
    field              TEXT NOT NULL,          -- 'seo_title' | 'seo_description'
    current_value      TEXT,                   -- snapshot at generation time
    proposed_value     TEXT NOT NULL,
    model              TEXT NOT NULL,
    prompt_version     TEXT NOT NULL,          -- so a regression is traceable to a prompt
    created_at         TEXT NOT NULL,
    uniqueness_status  TEXT,                   -- pass | fail | not_checked
    max_similarity     REAL,                   -- cosine vs nearest neighbour
    nearest_gid        TEXT,                   -- WHICH product it collided with
    eval_score         REAL,
    status             TEXT NOT NULL,          -- see state machine below
    reviewer_note      TEXT,
    superseded_by      INTEGER                 -- points at the retry, if any
);

-- The undo log. One row per live write. Written BEFORE the write.
CREATE TABLE pushes (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id        INTEGER NOT NULL REFERENCES proposals(id),
    batch_id           TEXT NOT NULL,
    autonomy_level     TEXT NOT NULL,          -- L0..L3 at time of write
    pushed_at          TEXT NOT NULL,
    before_value       TEXT,                   -- THE UNDO
    after_value        TEXT NOT NULL,
    api_response       TEXT,
    verified_at        TEXT,
    rolled_back_at     TEXT
);

CREATE TABLE embeddings (
    gid                TEXT NOT NULL,
    field              TEXT NOT NULL,
    vector             BLOB NOT NULL,
    model              TEXT NOT NULL,
    computed_at        TEXT NOT NULL,
    PRIMARY KEY (gid, field)
);
```

### 6.2 Proposal state machine

```
generated ──► unique_pass ──► scored ──► approved ──► pushed ──► verified
     │             │                         │                      │
     │             └──► unique_fail          └──► rejected          └──► rolled_back
     │                     │
     │                     └──► retry (once, collision named in prompt)
     │                              │
     └──────────────────────────────┴──► needs_human
```

**Failure policy:** one retry with the colliding text quoted back into the prompt, then `needs_human`. Retrying until success hides a bad prompt — a generator that always eventually succeeds gives no signal.

### 6.3 Prioritisation

```
priority_score =
      log1p(revenue_12mo)            -- money first
    × (2.0 if seo_title IS NULL else 1.0)   -- empty slots are free wins
    × (1.0 + 0.5 × duplicate_cluster_size)  -- cannibalisation is compounding damage
    × (0.2 if total_inventory <= 0 else 1.0)  -- don't optimise what can't be sold
    × (0.0 if status != 'ACTIVE' else 1.0)
```

The inventory term matters: sampling the catalog on 7 Aug found active products sitting at `totalInventory: 0`. SEO effort on an unbuyable product is wasted effort.

### 6.4 The staleness guard

**The failure this exists to prevent:** on 4 Aug the agent held a hardcoded catalog that no longer matched the store, and a live run would have overwritten good copy with a rewrite of months-old text.

**Rule, enforced in code:**

1. Re-fetch before every generate run. No exceptions.
2. Store `store_updated_at` on every product row.
3. At push time, re-read the product. **If `store_updated_at` is newer than the value captured when the proposal was generated, abort that product** and mark the proposal `stale`.

Someone editing the store by hand between generation and push is not an edge case here — it is the normal operating condition.

### 6.5 Modules

**Seven files. One job each. Every one runs on its own from the terminal.**

| # | Module | Responsibility | Writes to store? |
|---|--------|---------------|------------------|
| 1 | `db.py` | Schema and queries. Nothing else. | ❌ |
| 2 | `fetch.py` | Shopify API → `products`. Stamps `store_updated_at`. | ❌ |
| 3 | `prioritise.py` | Compute `priority_score` → `metrics`. No LLM. | ❌ |
| 4 | `generate.py` | LLM call, forced schema → `proposals` | ❌ |
| 5 | `verify.py` | Uniqueness gate + rubric score → sets proposal status | ❌ |
| 6 | `review.py` | `proposals` → CSV out; approved CSV → back in | ❌ |
| 7 | `push.py` | Undo row first, then `productUpdate`, max 10, then re-read | ✅ **only this one** |
| — | `agent.py` | Chooses which step to run next | ❌ **L2+, not built yet** |

**Three simplifications from the 7 Aug version, decided 11 Aug:**

1. **`uniqueness.py` + `evaluate.py` merged into `verify.py`.** Both answer the same question — is this draft good enough to show a human? Two files, one decision, no reason to separate them.
2. **`fetch.py` writes straight to SQLite.** The old `fetch_catalog.py` wrote a CSV because no database existed. One exists now, so the CSV hop is gone. There is no `load.py`.
3. **No `run.py` and no `config.py`.** At L0 the six commands in §7 *are* the flow. Settings live in `.env` until there is a reason for more.

**Only `push.py` can write to the store.** That is one file to audit, one file to test, one file to be careful in. Everything else is read-only by design, not by convention.

**Every module except `agent.py` is used from day one.** The agent, when it comes, calls the same functions a human calls today. That is what makes progressive autonomy real rather than a slogan.

---

## 7. The manual-first flow (L0) — what gets built

**Six commands. One line each. Run in order.**

```bash
python fetch.py                    # 1. store        → SQLite
python prioritise.py --top 10      # 2. scores       → work queue
python generate.py --queue         # 3. LLM drafts   → proposals
python verify.py --pending         # 4. gate         → pass / fail / needs_human
python review.py --export          # 5. review.csv   → business owner
python review.py --import approved.csv
python push.py --live --limit 10   # 6. undo, write, re-read
```

**The straight-line version:**

```
fetch  →  prioritise  →  generate  →  verify  →  review  →  push
(read)     (score)        (LLM)        (gate)     (human)    (write)
                             ▲                       │
                             └───── rejected ────────┘
                                    max 1 retry
```

One loop, one place. A draft that fails `verify` goes back to `generate` once, with the colliding text quoted into the prompt. Fail twice and it stops and waits for a person. Nothing else loops.

**Two gates before anything is live:** `verify.py` (machine) and `review.py` (your wife). `push.py` refuses any row that has not passed both.

**Default is dry run.** `push.py` prints what it would do. It needs `--live` to write, and it stops after 10.

**L2 is the same six commands with `agent.py` choosing which to run.** The commands do not change when autonomy widens — only who types them.

---

## 8. The gold set — an asset that already exists

Between 4 and 7 August, **49 listings were improved by hand**, reviewed by the business owner. Those 49 are not just completed work:

> **49 human-approved, known-good outputs on this exact catalog = the evaluation baseline.**

Obtaining a labelled set is normally the hardest part of building an eval. Here it was produced as a by-product of doing the work manually. `evaluate.py` scores every generated candidate against this set.

**This is the strongest argument for the manual-first approach: doing it by hand produced the data needed to automate it properly.**

---

## 9. What could go wrong, and what stops it

| Risk | Control |
|------|---------|
| Overwrites good copy | `seo.title` targeted first — it is null; nothing to destroy. Staleness guard on every push. |
| Duplicate/cannibalising copy | Embedding-based uniqueness gate vs all live copy **and** vs others in the same batch |
| Bad batch reaches the store | Max 10 per run; human approval at L0–L2; `pushes` holds before-values for rollback |
| Prompt injection from product descriptions | Product copy is untrusted input. Instruction-shaped content is stripped before the model sees it, with a deliberate trap listing in the test suite. *(15-day plan, Day 12)* |
| Secrets leak | `.env` git-ignored; history audited 7 Aug — token has never been committed |
| Silent model regression | `prompt_version` on every proposal; eval score recorded per proposal |
| Runaway cost | Tokens and latency logged per run; one-line cost summary at the end |

---

## 10. Open decisions — to close before code

1. **Embedding model** — a local sentence-transformer (free, offline, no key) vs an API embedding model (better quality, per-call cost). *Recommendation: local. 472 short strings, quality is more than sufficient, and it keeps the uniqueness gate free to run as often as we like.*
2. **Similarity threshold** — the cosine value above which two pieces of copy count as duplicates. *Recommendation: don't guess it. Compute pairwise similarity across the existing 472 descriptions, look at the distribution, and set the threshold where the known-duplicate clusters actually sit.*
3. **Batch size for review** — 10 per push is fixed. Whether the owner reviews 10 or 50 at a time is her preference, not an architecture decision. Ask her.

---

## 11. Build order

**Rebased 11 Aug. The 7 Aug schedule slipped four days — this is the honest version, not the optimistic one.**

| Day | Deliverable | Done when |
|-----|------------|-----------|
| Fri 7 Aug | This document + architecture diagram | ✅ Design frozen |
| Mon 11 Aug | Clean public repo + `db.py` | ✅ `dynamocks-seo-engine` live, three tables created |
| Tue 12 Aug | `db.py` rewritten to §6.1 schema + `fetch.py` | `SELECT COUNT(*)` returns the live catalog, `store_updated_at` populated |
| Wed 13 Aug | `prioritise.py` | Top 10 queue returned from data. **No product name anywhere in the code.** |
| Thu 14 Aug | `verify.py` on existing copy | Duplicate clusters named, threshold derived from the actual distribution — not guessed |
| Fri 15 Aug | `generate.py` → 10 proposals | 10 candidates stored, each with a uniqueness verdict |
| Sat 16 Aug | `review.py` export/import | Your wife has a CSV she can actually edit |
| Sun 17 Aug | `push.py` — **first 10-product live batch, L0** | 10 listings live, undo rows written before the write, verified by re-read |
| Mon 18 Aug | README + prompt-injection guard | A stranger understands it in two minutes; a trap listing is refused |
| Tue 19 Aug | Explain every component cold, no notes | Anything unexplainable goes back on the list |

**Rule that produced today's result: it ships in a booked session or it does not ship.** Homework record on this project is 0 for 10. Booked sessions are 8 for 8.

**Cut line.** If time runs short, ship `fetch → prioritise → generate → verify` and demo it with `push.py` in dry run. A working read-and-propose pipeline with a safety gate beats a half-finished write path.

---

## 12. The sentence this buys

> *"I built an SEO pipeline for a live 472-product store. It prioritises by revenue, generates copy against a forced schema, blocks semantically duplicate output with an embedding gate, scores every candidate against 49 human-approved examples, and pushes in batches of ten with an undo log written before the write. The AI has no autonomy — it earns it, one measured gate at a time, and any rollback demotes it. I designed the promotion criteria before I built the agent."*

Every clause of that is checkable against this repository.
