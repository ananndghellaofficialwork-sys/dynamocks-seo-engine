"""verify.py — stage 4 of the pipeline: score every draft proposal.

Why this file exists:
  Nothing so far has asked whether the generated copy is any good. The
  generator cannot answer that — CLAUDE.md: nothing that generates output may
  also judge it — so the question needs a different model, a written rubric,
  and a record of the answer. This is that step.

What it does:
  - Reads draft proposals that have not been scored yet.
  - Shows a judge model the product PHOTOGRAPHS alongside the proposed title
    and description, and asks for two separate scores.
  - Appends one row per field to `scores`, with proposal_id pointing at the
    exact row judged.
  - Writes the accuracy score back onto proposals.eval_score as the cached
    gate value push.py will read.

What it is FORBIDDEN from doing:
  - Never calls Shopify, never writes to the store.
  - Never uses a model from the same family that wrote the copy. The judge is
    configured separately from SEO_MODEL and the two must not overlap; that
    separation is the only thing making a score evidence rather than an
    opinion about itself.
  - Never rewrites a proposal. A bad score is a finding, not a licence to fix
    — regenerate.py does that, deliberately, as its own step.

Usage:
    python3 verify.py            score everything unscored
    python3 verify.py 20         score the next 20 products
"""
import base64
import json
import os
import re
import sys
import time

import requests

import db
import generate

LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else None

JUDGE_MODEL = os.environ.get("JUDGE_MODEL", "claude-opus-5").strip()
_ANTHROPIC_ENDPOINT = "https://api.anthropic.com/v1/messages"
RUN_LABEL = "verify-" + generate.datetime.date.today().isoformat()

# The gate. A proposal scoring below this on accuracy is not fit to push, no
# matter how well it reads: accuracy failures put a claim on a live listing
# that the product does not support.
PASS_ACCURACY = 4

# Room for the model to think AND answer. The answer itself is three short
# lines; almost all of this is headroom so a thinking budget can never crowd
# out the reply.
_MAX_TOKENS = 2000

_RUBRIC = """\
You are scoring SEO copy written for one product on an American e-commerce
store that sells socks. The product photographs are attached.

Score the copy on two SEPARATE axes. Do not merge them and do not average.

1. ACCURACY (0-5) — does it describe what is actually in the photographs?
   5  every claim is visible in the photos or stated in the product data
   3  broadly right, one vague or unsupported detail
   0  confidently describes a pattern, color or product that is not there

   A confident wrong description scores 0 however well written it is. This is
   the axis that decides whether copy may go on a live store.

2. SEARCH QUALITY (0-5) — would an American shopper type this into Google?

   5 looks like these — the owner's own approved titles:
       "Banana Novelty Crew Socks | Dynamocks"
       "Hexagon Print Crew Socks | Dynamocks"
       "Bubble Polka Dot Crew Socks | Dynamocks"
     Short, leads with the search term, brand last, under 60 characters.

   1 looks like these — real titles that were live on this store:
       "DS Unisex Invisibles - Colorful Cotton Socks | Teal Blue & Light
        Grey Comfort with a Splash of Color"      (100 chars, truncated)
       "Cotton Ankle Socks Combo - Mint, Aqua, Purple, Maroon, Teal Blue &
        Green | Vibrant & Versatile"              (94 chars)
     Long, leads with a codename, ends in marketing filler.

   2 covers stacked redundant descriptors — "Geometric Block Pattern Crew
     Socks" — where two words carry one idea and no shopper types both.

MATERIAL IS AN ACCURACY FAILURE, NOT A STYLE POINT.
The true fibre composition is given below, taken from the store's own spec.
  - Copy naming a fibre that contradicts it scores accuracy 0. This store has
    products that are 75% polyester whose old marketing copy called them
    "cotton-rich" — that error is exactly what this axis exists to catch.
  - If the composition reads "(NOT STATED", any material word in the copy is
    an invention. Score accuracy no higher than 2.
  - Naming the dominant fibre only ("combed cotton") is CORRECT. Do not
    penalise copy for omitting the percentages.

Notes on this catalog, so you do not penalise correct choices:
  - Internal names like Polka, Rogue, TANGY, MONO and TRIOS are MERCHANDISING
    GROUP LABELS, not descriptions. Copy that ignores them and describes what
    is in the photograph is CORRECT, not inaccurate.
  - A product sold as a multipack should lead with the pack count and an
    occasion, not with a single pattern. Judge it on that.

Reply in exactly this format, nothing else:

TITLE: accuracy=N search=N
DESCRIPTION: accuracy=N search=N
WHY: one sentence naming the specific word or claim that cost the most points
"""

# Deliberately loose. The first version required
#     TITLE: accuracy=5 search=4
# on one line with nothing between the parts, and 62% of Opus replies failed to
# match — markdown bold, a newline between the two numbers, or "accuracy: 5"
# instead of "accuracy=5" were all enough to break it. Every one of those is a
# correct answer in a slightly different costume.
#
# DOTALL so the two numbers may sit on separate lines; non-greedy so TITLE's
# scores cannot be read from DESCRIPTION's line further down.
_SCORE_BLOCK = re.compile(
    r"(TITLE|DESCRIPTION)\W{0,4}\s*[:\-]?.*?"
    r"accuracy\s*[:=]?\s*(-?\d+)"
    r".*?search(?:\s*quality)?\s*[:=]?\s*(-?\d+)",
    re.IGNORECASE | re.DOTALL,
)
_WHY_LINE = re.compile(r"WHY\W{0,4}\s*[:\-]?\s*(.+)", re.IGNORECASE)


# Running totals for the cache counters, read back off every response.
CACHE = {"write": 0, "read": 0, "fresh": 0}


class FatalJudgeError(RuntimeError):
    """
    A judge failure that will not fix itself: no credit, bad key, disabled key.

    Its own class, rather than a flag on a generic error, because the per-
    product handler catches Exception on purpose — one malformed reply must not
    end a 300-product run. That same catch turned a billing failure into 261
    identical failures and half an hour of nothing. This type is the one thing
    that handler is allowed to let through.
    """


def _error_message(response) -> str:
    """The provider's own explanation, or the raw body if it is not JSON."""
    try:
        return response.json().get("error", {}).get("message", "")[:200]
    except Exception:
        return (response.text or "")[:200]


def unscored(conn, limit=None):
    """
    Products with live draft proposals that carry no score yet.

    Grouped per product rather than per proposal because one judge call sees
    the photographs once and scores both fields — asking twice would double
    the image tokens to answer half a question each time.

    Resumable by construction: a product already in `scores` under any run
    label is skipped, so an interrupted run picks up where it stopped rather
    than re-paying for work already done.
    """
    rows = conn.execute(
        """
        SELECT pr.gid,
               p.title, p.images, p.material,
               MAX(CASE WHEN pr.field = 'seo_title' THEN pr.id END)             AS title_id,
               MAX(CASE WHEN pr.field = 'seo_title' THEN pr.proposed_value END) AS proposed_title,
               MAX(CASE WHEN pr.field = 'seo_description' THEN pr.id END)       AS desc_id,
               MAX(CASE WHEN pr.field = 'seo_description' THEN pr.proposed_value END) AS description
        FROM proposals pr
        JOIN products p ON p.gid = pr.gid
        WHERE pr.status = 'draft'
          AND pr.superseded_by IS NULL
          -- A row in `scores` is not the same as a SCORE. 190 rows were
          -- written with accuracy NULL when the judge's reply failed to parse,
          -- and treating those as done would have quietly left a third of the
          -- catalog unjudged while the count said otherwise. Only a real
          -- number counts as scored.
          AND NOT EXISTS (
              SELECT 1 FROM scores s
              WHERE s.gid = pr.gid AND s.accuracy IS NOT NULL
          )
        GROUP BY pr.gid
        ORDER BY pr.gid
        """
    ).fetchall()
    return rows[:limit] if limit else rows


def ask_judge(product_title, images, title, description, material=None):
    """
    One judge call. Returns the raw reply text.

    The photographs go with it. A text-only judge cannot check whether the
    words match the sock, which is the whole of the accuracy axis — it would
    end up scoring how well the copy reads and calling that accuracy.
    """
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set — required for the judge")

    # The ground truth the judge checks the copy against. Material comes from
    # the store's own composition line, not from its marketing copy — without
    # it the judge cannot tell "combed cotton" on a 75% polyester sock from a
    # correct claim, and would score the sentence rather than the fact.
    facts = (f"\n\nSTORED PRODUCT NAME: {product_title}\n"
             f"TRUE MATERIAL COMPOSITION: "
             f"{material or '(NOT STATED — copy must not name any material)'}\n")

    # No photographs means the accuracy axis has nothing to check the PATTERN
    # against. Say so rather than letting the judge quietly score it from the
    # text — a 5 awarded by reading the copy back to itself is worse than no
    # score, because it looks identical to a verified one in the table.
    if not images:
        facts += (
            "\nNO PHOTOGRAPHS ARE AVAILABLE for this product.\n"
            "Return accuracy=-1 for both fields. Do not guess an accuracy "
            "score from the text.\n"
            "Score SEARCH QUALITY normally — that axis does not need an image.\n"
        )

    # The rubric is identical on all 325 calls, so it is marked for caching and
    # kept in its OWN block. Concatenating it with `facts` — which changes every
    # product — would make the whole block unique and cache nothing, which is
    # what the first version did.
    #
    # Caching covers everything up to and including the marked block, so the
    # rubric must also come first. At ~680 tokens it clears Opus 5's 512-token
    # minimum; a shorter rubric would silently be processed uncached.
    #
    # Cost shape: a cache WRITE is 1.25x normal input, a cache READ is 0.1x.
    # One write then 324 reads is the trade.
    content = [
        {
            "type": "text",
            "text": _RUBRIC,
            "cache_control": {"type": "ephemeral"},
        },
        {"type": "text", "text": facts},
    ]
    for part in images:
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": part["inline_data"]["mime_type"],
                    "data": part["inline_data"]["data"],
                },
            }
        )
    content.append(
        {"type": "text",
         "text": f"\nTITLE: {title}\nDESCRIPTION: {description}\n"}
    )

    for attempt in range(1, generate._ATTEMPTS + 1):
        response = requests.post(
            _ANTHROPIC_ENDPOINT,
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": JUDGE_MODEL,
                # 300 was not enough. Opus 5 emits thinking blocks before its
                # answer, and a budget that small was spent before any text was
                # produced — the call succeeded, returned only thinking, and
                # this code joined the (empty) set of text blocks into "".
                # 397 rows were written recording an empty reply.
                "max_tokens": _MAX_TOKENS,
                "messages": [{"role": "user", "content": content}],
            },
            timeout=120,
        )
        # Credit exhaustion, a revoked key and a disabled account are all
        # conditions that will not improve by waiting. They must stop the run,
        # not be retried four times and then swallowed per-product for another
        # 260 products — which is exactly what happened on the first attempt.
        if response.status_code in (401, 403) or (
            response.status_code == 400
            and "credit" in (response.text or "").lower()
        ):
            raise FatalJudgeError(
                f"HTTP {response.status_code}: {_error_message(response)}"
            )

        if response.status_code == 429 and "credit" in (response.text or "").lower():
            raise FatalJudgeError(f"out of credit: {_error_message(response)}")

        if response.status_code in generate._RETRY_STATUS and attempt < generate._ATTEMPTS:
            print(f"    .. HTTP {response.status_code}, retry {attempt}")
            time.sleep(2 ** attempt)
            continue
        response.raise_for_status()
        body = response.json()

        # Read the cache counters back off the response. A cache_control block
        # that is too short, or preceded by something variable, is ignored
        # SILENTLY — the call succeeds and simply costs full price. The only
        # way to know it is working is to look.
        usage = body.get("usage", {})
        CACHE["write"] += usage.get("cache_creation_input_tokens", 0)
        CACHE["read"] += usage.get("cache_read_input_tokens", 0)
        CACHE["fresh"] += usage.get("input_tokens", 0)

        blocks = body.get("content", [])
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()

        if not text:
            # An empty answer must name WHY it was empty. stop_reason and the
            # block types are the two facts that separate "hit the token
            # ceiling" from "refused" from "returned only thinking" — and
            # storing none of them is what made this take two runs to find.
            raise RuntimeError(
                f"judge returned no text: stop_reason="
                f"{body.get('stop_reason')!r}, blocks="
                f"{[b.get('type') for b in blocks]}, "
                f"output_tokens={body.get('usage', {}).get('output_tokens')}"
            )

        return text

    raise RuntimeError("judge unreachable after retries")


def parse_scores(reply):
    """
    Pull the two score pairs and the reason out of the judge's reply.

    Missing rather than defaulted when the model does not answer in shape: a
    score of 0 and "the judge did not say" are different facts and storing
    them as the same number would silently create failures that never happened.
    """
    found = {}
    for label, accuracy, search in _SCORE_BLOCK.findall(reply):
        label = label.upper()
        if label in found:
            continue  # first match wins; a later repeat is an echo
        value = int(accuracy)
        # -1 is the judge reporting it had no photograph to check against.
        # Stored as None: "could not be checked" and "checked and scored 0"
        # are opposite findings and must never share a value.
        found[label] = (None if value < 0 else value, max(int(search), 0))

    why = _WHY_LINE.search(reply)
    why = why.group(1).strip() if why else None

    # When nothing parsed, hand back the raw reply as the reason. The first
    # version returned None here and wrote 190 rows that recorded only that
    # something had gone wrong, not what — which made the failure undiagnosable
    # without paying for all 152 products again. An unparseable answer is
    # evidence and has to be kept.
    if not found and not why:
        why = "UNPARSED JUDGE REPLY: " + reply.replace("\n", " ")[:400]

    return found, why


def main():
    conn = db.connect()
    db.init_schema(conn)

    rows = unscored(conn, LIMIT)
    if not rows:
        print("nothing left to score")
        return

    print(f"judge    : {JUDGE_MODEL}")
    print(f"products : {len(rows)}")
    print(f"gate     : accuracy >= {PASS_ACCURACY}")
    print("=" * 96)

    scored = failed = blind = 0
    below = []

    for number, row in enumerate(rows, 1):
        if number > 1:
            time.sleep(generate._PRODUCT_PAUSE)

        try:
            images = generate.fetch_image_parts(row["images"])
            if not images:
                blind += 1
            reply = ask_judge(
                row["title"], images, row["proposed_title"], row["description"],
                material=row["material"],
            )
            found, why = parse_scores(reply)
        except FatalJudgeError as error:
            # Stop, do not continue. Everything scored so far is committed and
            # the next run resumes from here.
            print(f"\nSTOPPED — {error}")
            print(f"{scored} product(s) scored and saved before this. "
                  f"Fix the account and re-run; it resumes automatically.")
            break
        except Exception as error:
            failed += 1
            print(f"[{number}/{len(rows)}] {row['title'][:50]} — FAILED {type(error).__name__}")
            continue

        for label, field, proposal_id, value in (
            ("TITLE", "seo_title", row["title_id"], row["proposed_title"]),
            ("DESCRIPTION", "seo_description", row["desc_id"], row["description"]),
        ):
            if proposal_id is None:
                continue
            accuracy, search = found.get(label, (None, None))

            db.save_score(
                conn,
                {
                    "proposal_id": proposal_id,
                    "gid": row["gid"],
                    "run_label": RUN_LABEL,
                    "arm": field,
                    "seo_title": value if field == "seo_title" else None,
                    "seo_description": value if field == "seo_description" else None,
                    "accuracy": accuracy,
                    "search": search,
                    "won": None,
                    "reason": why,
                    "judge_model": JUDGE_MODEL,
                },
            )

            # eval_score on the proposal is a CACHED GATE VALUE, not the record.
            # The record is the scores row above, which is append-only and keeps
            # every judgement ever made. This column exists because push.py has
            # to answer "may I write this" with one indexed lookup, and joining
            # to the newest score row for that proposal on every push is the
            # kind of query that eventually gets written wrong.
            if accuracy is not None:
                conn.execute(
                    "UPDATE proposals SET eval_score = :score WHERE id = :id",
                    {"score": accuracy / 5.0, "id": proposal_id},
                )

        conn.commit()   # per product, so an interrupted run keeps what it paid for
        scored += 1

        title_scores = found.get("TITLE", (None, None))
        flag = ""
        if title_scores[0] is not None and title_scores[0] < PASS_ACCURACY:
            flag = "   <-- BELOW GATE"
            below.append((row["title"], title_scores, why))
        print(f"[{number}/{len(rows)}] acc={title_scores[0]} search={title_scores[1]}  "
              f"{row['title'][:44]}{flag}")

    print("\n" + "=" * 96)
    print(f"scored : {scored}")
    print(f"failed : {failed}")
    print(f"below the accuracy gate : {len(below)}")
    if blind:
        print(f"scored WITHOUT a photo  : {blind}  (accuracy left NULL — nothing to check against)")
    for title, (accuracy, search), why in below[:15]:
        print(f"  acc={accuracy} search={search}  {title[:44]}")
        if why:
            print(f"      {why[:88]}")

    total = CACHE["read"] + CACHE["write"] + CACHE["fresh"]
    if total:
        print(f"\nPROMPT CACHE")
        print(f"  cache reads  : {CACHE['read']:>9,} tokens at 0.1x price")
        print(f"  cache writes : {CACHE['write']:>9,} tokens at 1.25x price")
        print(f"  uncached     : {CACHE['fresh']:>9,} tokens at full price")
        if CACHE["read"] == 0 and scored > 1:
            print("  !! nothing was read from cache — the rubric block is not "
                  "being cached.\n     Check it still exceeds the model's minimum "
                  "cacheable length.")

    print("\nScores are in the scores table. To see the worst first:")
    print('  sqlite3 data/seo.db "SELECT accuracy, search, arm, seo_title FROM scores '
          'ORDER BY accuracy, search LIMIT 20;"')
    conn.close()


if __name__ == "__main__":
    main()
