"""Spike: does showing the model the PHOTO beat text-only grounding?

NOT the pipeline. Writes nothing to proposals, writes nothing to the store.
This exists to answer one question with evidence before DESIGN-v2 S12a steps
3 and 4 get built, because writing listing-v3.md and reshaping _call_gemini()
is real work and it should not be done on a hunch.

The method is A/B on the SAME product with the SAME rules:
    arm A  text only          - what generate.py does today
    arm B  images + text      - image-first grounding per S12a

One arm alone would only show that the output looks fine. Effectiveness is a
comparison, so both run on every product.

The scoring is done by Claude Sonnet, NOT by the model that wrote the copy.
CLAUDE.md: candidates come from one set of models, scores come from a model
outside that set. Gemini generates here, Claude judges, and the two never
overlap. Three things make the verdict worth reading:

  - the judge SEES THE PHOTOS. A text-only judge cannot check whether the
    words match the sock, which is the entire claim arm B is making, so it
    would end up rewarding whichever line simply reads better.
  - the arms are UNLABELLED and their order is SHUFFLED per product, so the
    judge cannot favour a position or know which one is the new idea.
  - it scores accuracy and search-term quality separately, because the
    2026-08-16 TRIOS spike produced "Geometric Block Pattern Crew Socks" --
    visually accurate and still useless as a search term. One number would
    have hidden that.

Nothing here writes to proposals or to the store.

Usage:
    python3 spike_images.py           10 products
    python3 spike_images.py 3         3 products, for a cheap first look

Requires ANTHROPIC_API_KEY in .env for the judge. Without it the arms still
run and print, and the scoring is skipped.
"""
import base64
import datetime
import json
import os
import random
import re
import sys
import time

import requests

import db
from db import connect
import generate

# Stamps every score row from this run. Sorting the backlog later is only
# meaningful if you can tell which prompt and which approach produced a number.
RUN_LABEL = "spike-image-vs-text-" + datetime.date.today().isoformat()

COUNT = int(sys.argv[1]) if len(sys.argv) > 1 else 10
# 5 rather than 3, now that nothing pre-filters the set. Position 0 is often a
# promo banner on this catalog, so a small sample can be mostly junk by
# accident; sending more dilutes that and lets the model pick the real frames.
# Cost and latency both scale directly on this number.
MAX_IMAGES = 5          # per product, per call
MODEL_ID = "gemini-2.5-flash"
MAX_BYTES = 4_000_000   # skip an image larger than this rather than stall the run

# Judge model is config, not code — same rule SEO_MODEL follows. Defaults to
# Opus because this spike decides whether listing-v3.md and a multimodal
# _call_gemini get built at all, and a wrong call there costs days. For routine
# scoring of all 250 products later, a cheaper judge is the sensible swap; what
# must NOT change is that it stays outside the generating family.
JUDGE_MODEL = os.environ.get("JUDGE_MODEL", "claude-opus-5").strip()
_ANTHROPIC_ENDPOINT = "https://api.anthropic.com/v1/messages"

# Prepended to the v2 rules for arm B only. This is the S12a grounding order,
# kept here rather than in prompts/ because listing-v3.md is a decision this
# spike exists to inform -- writing the file first would assume the answer.
_IMAGE_FIRST = """\
# Grounding order for this task — the IMAGE comes first

You are shown one or more photographs of the product, followed by its stored
text fields.

1. Look at the photographs FIRST. Decide what an American shopper would call
   this design if they saw it with no other information — the pattern, the
   colors, the motif. That interpretation is the seed for both fields.
2. The text fields below are a CROSS-CHECK, not the source. They confirm
   facts a photo cannot show (material, fit) and they correct you when you
   misread the image. They do not lead.
3. The product's internal name is the weakest signal of all. Codenames like
   "Banger", "MONO" or "TRIOS" describe nothing a shopper would search for.
4. If the photographs and the text actively disagree — the photo shows
   stripes and the tags say polka dot — say so and set needs_human. Do not
   pick one and proceed.

Some photographs may be promotional graphics, sale banners, or size charts
rather than the product. Ignore those and describe the sock.

---

"""


def pick_images(images_json):
    """
    Take the first MAX_IMAGES photos in store order. No filtering.

    An earlier version preferred images carrying alt text, on the theory that
    blank alt text marks the promo graphics. Withdrawn 2026-08-20, and the
    reason is worth keeping: alt text presence measures whether SOMEONE
    BOTHERED TO WRITE IT, not whether the image shows the product. 745 of
    2,859 images are blank and around 90 products have none at all, so the
    rule was silently skipping real product photography on any listing whose
    alt text was simply never filled in -- the false negative was invisible
    and the whole point of this arm is to give the model MORE to look at.

    It was also a heuristic built on an 8-product observation, which is the
    identical error that set _MAX_MEDIA to 10 and lost 32 photos the same day.

    Junk images are handled where they should be: the prompt tells the model
    that some frames are banners or size charts and to describe the sock
    instead. If that turns out not to work, the accuracy scores will show it,
    and then a filter can be built against evidence rather than a hunch.

    Alt text is still stored, still passed to the judge as context, and still
    useful. It just does not get a vote on which photos are sent.
    """
    images = json.loads(images_json) if images_json else []
    return images[:MAX_IMAGES]


def fetch_image_part(url):
    """Download one image and wrap it as a Gemini inline_data part; None on failure."""
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
    except requests.RequestException as error:
        # A dead CDN link is data about the catalog, not a reason to stop the
        # spike. It is also the exact failure mode flagged against the
        # staleness guard: that guard watches store_updated_at, not image URLs.
        print(f"    !! image fetch failed: {type(error).__name__} {url[:70]}")
        return None

    if len(response.content) > MAX_BYTES:
        print(f"    -- skipped {len(response.content) // 1000}KB image (over cap)")
        return None

    mime = "image/png" if url.split("?")[0].lower().endswith(".png") else "image/jpeg"
    return {
        "inline_data": {
            "mime_type": mime,
            "data": base64.b64encode(response.content).decode("ascii"),
        }
    }


_RETRY_STATUS = {429, 500, 502, 503, 504}
_ATTEMPTS = 4


def post_with_retry(**kwargs):
    """
    POST, retrying the failures that are the provider's problem rather than ours.

    429 and 5xx mean overloaded or briefly broken, and the correct response to
    both is to wait and ask again. A 400 or 401 means the request itself is
    wrong and retrying it just spends money making the same mistake, so those
    raise immediately.

    This exists because a single 503 on product 7 of 10 previously killed the
    run and threw away six products of paid calls. Backs off 2s, 4s, 8s.
    """
    for attempt in range(1, _ATTEMPTS + 1):
        try:
            response = requests.post(**kwargs)
        except requests.RequestException as error:
            if attempt == _ATTEMPTS:
                raise
            print(f"    .. {type(error).__name__}, retry {attempt}/{_ATTEMPTS - 1}")
            time.sleep(2 ** attempt)
            continue

        if response.status_code in _RETRY_STATUS and attempt < _ATTEMPTS:
            print(f"    .. HTTP {response.status_code}, retry {attempt}/{_ATTEMPTS - 1}")
            time.sleep(2 ** attempt)
            continue

        response.raise_for_status()
        return response

    raise RuntimeError("unreachable")


def ask_gemini(parts):
    """POST parts to Gemini and return (text, seconds). Mirrors generate._call_gemini."""
    started = time.time()
    response = post_with_retry(
        url=generate._GEMINI_ENDPOINT.format(model_id=MODEL_ID),
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": generate._require_key("GEMINI_API_KEY", "gemini"),
        },
        json={"contents": [{"parts": parts}]},
        timeout=120,          # higher than the pipeline's 60: images are slower
    )
    elapsed = time.time() - started

    body = response.json()
    candidates = body.get("candidates") or []
    if not candidates:
        return f"(no candidate: {body.get('promptFeedback')})", elapsed

    texts = [p["text"] for p in candidates[0]["content"]["parts"] if "text" in p]
    return ("".join(texts).strip() or "(empty response)"), elapsed


_JUDGE_BRIEF = """\
You are scoring two candidate SEO titles and meta descriptions written for the
SAME e-commerce sock listing. The product photographs are attached.

Score each candidate on two SEPARATE axes. Do not merge them.

1. ACCURACY (0-5). Does it describe what is actually in the photographs and
   consistent with the stored text? A confident description of a pattern the
   sock does not have scores 0, however well written.

2. SEARCH QUALITY (0-5). Is it a phrase a real American shopper would type
   into Google? Penalise stacked redundant descriptors ("Geometric Block
   Pattern"), internal codenames, and vague filler ("designer", "premium",
   "stylish"). A phrase can be perfectly accurate and still score low here.

CALIBRATION — what the numbers mean on THIS store. Do not score against your
own general idea of good copy; score against these.

search=5 looks like these, the owner's own approved titles:
    "Banana Novelty Crew Socks | Dynamocks"
    "Geometric Print Crew Socks | Dynamocks"
    "Bubble Polka Dot Crew Socks | Dynamocks"
  Short. Leads with the thing a shopper searches for. Brand last, and only
  because it fits. Under 60 characters, because Google truncates near there.

search=1 looks like these, real titles currently live on the store:
    "DS Unisex Invisibles - Colorful Cotton Socks | Teal Blue & Light Grey
     Comfort with a Splash of Color"                        (100 chars)
    "Cotton Ankle Socks Combo - Mint, Aqua, Purple, Maroon, Teal Blue &
     Green | Vibrant & Versatile"                           (94 chars)
  Long, truncated in results, leads with a codename, and ends in marketing
  filler nobody types into a search box.

search=2 also covers stacked redundant descriptors such as "Geometric Block
Pattern Crew Socks" -- geometric already implies blocky shapes, and no
shopper types both.

A title being LIVE ON THE STORE ALREADY is not evidence that it is good. 71%
of this catalog's existing titles break the 60-character rule.

Then name a WINNER: 1, 2, or TIE. Accuracy outranks search quality when they
conflict -- wrong copy on a live store costs more than dull copy.

Reply in exactly this format, nothing else:

CANDIDATE 1: accuracy=N search=N
CANDIDATE 2: accuracy=N search=N
WINNER: 1|2|TIE
WHY: one sentence, naming the specific word or claim that decided it
"""


def judge(product, image_parts, candidate_one, candidate_two):
    """
    Ask Claude Sonnet which candidate is better, showing it the same photos.

    Returns the verdict text, or None when no key is configured -- a missing
    judge degrades the spike to "print both and read them", which is still
    useful, rather than killing the run.

    The candidates arrive already shuffled and are labelled only 1 and 2. This
    function does not know which arm produced which, and neither does the
    model: that is the point, not an oversight.
    """
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return None

    facts = (
        f"stored title: {product['title']}\n"
        f"stored description: {product['seo_description'] or '(none)'}\n"
    )

    content = [{"type": "text", "text": _JUDGE_BRIEF + "\n" + facts}]

    # Re-wrap the Gemini inline_data parts into Anthropic's image block shape.
    # Same bytes, already downloaded -- no second trip to the CDN.
    for part in image_parts:
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
        {
            "type": "text",
            "text": f"\nCANDIDATE 1:\n{candidate_one}\n\nCANDIDATE 2:\n{candidate_two}\n",
        }
    )

    response = post_with_retry(
        url=_ANTHROPIC_ENDPOINT,
        headers={
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": JUDGE_MODEL,
            "max_tokens": 300,
            "messages": [{"role": "user", "content": content}],
        },
        timeout=120,
    )
    body = response.json()
    return "".join(b["text"] for b in body["content"] if b["type"] == "text").strip()


_SCORE_LINE = re.compile(
    r"CANDIDATE\s*([12])\s*:.*?accuracy\s*=\s*(\d+).*?search\s*=\s*(\d+)",
    re.IGNORECASE,
)
_WINNER_LINE = re.compile(r"WINNER\s*:\s*(1|2|TIE)", re.IGNORECASE)
_WHY_LINE = re.compile(r"WHY\s*:\s*(.+)", re.IGNORECASE)


def parse_verdict(verdict):
    """
    Pull numbers out of the judge's reply so they can be sorted later.

    Returns (scores, winner, why) where scores maps candidate number to
    {"accuracy", "search"}. Anything the model did not return in the expected
    shape comes back missing rather than defaulted -- a score of 0 and "the
    judge did not answer" are completely different facts and must not be
    stored as the same number.

    Written as its own function for the same reason parse_response exists:
    model output shape is the part most likely to break, and when a future
    judge starts wrapping its answer in JSON this is the one place to fix.
    """
    scores = {}
    for number, accuracy, search in _SCORE_LINE.findall(verdict):
        scores[int(number)] = {"accuracy": int(accuracy), "search": int(search)}

    winner = _WINNER_LINE.search(verdict)
    why = _WHY_LINE.search(verdict)
    return (
        scores,
        winner.group(1).upper() if winner else None,
        why.group(1).strip() if why else None,
    )


def as_candidate(text):
    """Flatten one arm's raw model reply into the two lines the judge compares."""
    fields = generate.parse_response(text)
    return (
        f"title: {fields.get('seo_title') or '(none returned)'}\n"
        f"description: {fields.get('seo_description') or '(none returned)'}"
    )


def show(label, text, elapsed, extra=""):
    """Print one arm's parsed result in a fixed shape so the two can be compared."""
    fields = generate.parse_response(text)
    title = fields.get("seo_title")
    description = fields.get("seo_description")

    print(f"  {label}  ({elapsed:.1f}s{extra})")
    if title:
        print(f"    title [{len(title):>2}]  {title}")
    else:
        print("    title       -- NOT RETURNED --")
    print(f"    desc        {description or '-- NOT RETURNED --'}")


def main():
    prompt_text, version = generate.load_prompt("v2")
    conn = connect()
    # Creates scores (and seo_exclusions) on a database that predates them.
    # connect() only opens the file; CREATE TABLE IF NOT EXISTS lives in
    # init_schema, so any script that touches a NEW table has to call it.
    db.init_schema(conn)
    products = generate.get_products_needing_seo(conn, limit=COUNT)

    has_judge = bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())

    print(f"prompt   : listing-{version}.md")
    print(f"generator: {MODEL_ID}")
    print(f"judge    : {JUDGE_MODEL}" if has_judge
          else "judge    : DISABLED — set ANTHROPIC_API_KEY in .env to score the arms")
    print(f"products : {len(products)} (asked for {COUNT})")
    print(f"images   : first {MAX_IMAGES} in store order, unfiltered")
    print("=" * 100)

    text_time = 0.0
    image_time = 0.0
    no_photo = 0
    wins = {"text-only": 0, "image+text": 0, "tie": 0}
    failures = []

    for number, product in enumerate(products, 1):
        message = generate.build_message(product, prompt_text)
        chosen = pick_images(product["images"])

        print(f"\n[{number}/{len(products)}] {product['title'][:78]}")
        print(f"  stored text: {(product['seo_description'] or '(no description)')[:90]}")

        # Each arm is wrapped separately. A product that dies is recorded and
        # skipped, never fatal: the run has already spent money on the products
        # before it, and losing that to a provider hiccup on this one is the
        # wrong trade. Same principle as CLAUDE.md's "a failed verify is data,
        # not an exception" -- applied here to the call itself.
        #
        # ARM A -- text only. The current pipeline, unchanged, as the baseline.
        try:
            text_answer, elapsed = ask_gemini([{"text": message}])
        except Exception as error:
            failures.append((product["title"], f"arm A: {type(error).__name__}"))
            print(f"  A text-only  -- FAILED after retries: {error}")
            continue
        text_time += elapsed
        show("A text-only ", text_answer, elapsed)

        # ARM B -- images first, then the same text.
        if not chosen:
            no_photo += 1
            print("  B images    -- NO PHOTO ON THIS PRODUCT, cannot be image-grounded --")
            continue

        parts = [{"text": _IMAGE_FIRST + message}]
        for image in chosen:
            part = fetch_image_part(image["url"])
            if part:
                parts.append(part)

        if len(parts) == 1:
            print("  B images    -- every image failed to download --")
            continue

        image_parts = parts[1:]
        try:
            image_answer, elapsed = ask_gemini(parts)
        except Exception as error:
            # The image arm fails more than the text arm: the payload is
            # megabytes of base64 rather than a few KB of text, so it is
            # likelier to hit a capacity limit. Worth logging as its own
            # count -- if arm B fails materially more often at catalog scale,
            # that is a cost of the approach, not noise.
            failures.append((product["title"], f"arm B: {type(error).__name__}"))
            print(f"  B image+text -- FAILED after retries: {error}")
            continue
        image_time += elapsed
        show("B image+text", image_answer, elapsed, extra=f", {len(image_parts)} img")

        # ── JUDGE ────────────────────────────────────────────────────────────
        # Shuffle before labelling, so position 1 is not always the baseline.
        # arms[0] becomes CANDIDATE 1; the mapping stays here and is never sent.
        arms = [("text-only", text_answer), ("image+text", image_answer)]
        random.shuffle(arms)

        try:
            verdict = judge(
                product, image_parts, as_candidate(arms[0][1]), as_candidate(arms[1][1])
            )
        except Exception as error:
            failures.append((product["title"], f"judge: {type(error).__name__}"))
            print(f"  JUDGE        -- FAILED after retries: {error}")
            continue
        if verdict is None:
            continue

        # Translate the blind labels back to arm names. The human needs to know
        # which approach won; the model deliberately did not.
        parsed, winner, why = parse_verdict(verdict)
        winner_name = (
            "tie" if winner == "TIE"
            else arms[0][0] if winner == "1"
            else arms[1][0] if winner == "2"
            else None
        )
        if winner_name:
            wins[winner_name] += 1

        print("  JUDGE")
        for number, (arm_name, answer) in enumerate(arms, start=1):
            score = parsed.get(number, {})
            accuracy = score.get("accuracy")
            search = score.get("search")
            print(f"    {arm_name:<11} accuracy={accuracy if accuracy is not None else '?'}"
                  f"  search={search if search is not None else '?'}")

            fields = generate.parse_response(answer)
            db.save_score(
                conn,
                {
                    "gid": product["gid"],
                    "run_label": RUN_LABEL,
                    "arm": arm_name,
                    "seo_title": fields.get("seo_title"),
                    "seo_description": fields.get("seo_description"),
                    "accuracy": accuracy,
                    "search": search,
                    "won": 1 if arm_name == winner_name else 0,
                    "reason": why,
                    "judge_model": JUDGE_MODEL,
                },
            )

        print(f"    WINNER: {winner_name or '(judge reply unparseable)'}")
        if why:
            print(f"    WHY: {why}")

        # Commit per product, not once at the end. A run that dies on product 8
        # keeps the seven it already paid for -- the same reason fetch.py
        # commits per page.
        conn.commit()

    print("\n" + "=" * 100)
    print(f"text-only  total {text_time:6.1f}s")
    print(f"image+text total {image_time:6.1f}s"
          + (f"   ({image_time / text_time:.1f}x slower)" if text_time else ""))

    scored = sum(wins.values())
    if scored:
        print(f"\nVERDICT over {scored} scored product(s)")
        for name in ("text-only", "image+text", "tie"):
            print(f"  {name:<12} {wins[name]}")
        # State the caveat in the output, not just in conversation: a tally
        # this small can swing on one product, and the number is going to be
        # quoted later as if it settled the question.
        if scored < 10:
            print(f"  {scored} products is a sample, not a result — run more before deciding")

    if no_photo:
        print(f"\n{no_photo} product(s) had no photo at all — text-only is the only option there")

    if failures:
        print(f"\n{len(failures)} failure(s) — these products produced no comparison:")
        for title, what in failures:
            print(f"  {what:<22} {title[:60]}")

    # The backlog. This is the reason the scores are written down rather than
    # printed: the worst copy has to be findable later, without re-running and
    # re-paying for every judgement.
    worst = conn.execute(
        """
        SELECT arm, accuracy, search, seo_title, reason
        FROM scores
        WHERE run_label = :run_label
          AND accuracy IS NOT NULL
        ORDER BY accuracy ASC, search ASC
        LIMIT 5
        """,
        {"run_label": RUN_LABEL},
    ).fetchall()

    if worst:
        print(f"\nWEAKEST COPY THIS RUN — regenerate these first")
        print(f"  {'ACC':>3} {'SRCH':>4}  {'ARM':<11}  TITLE")
        for row in worst:
            print(f"  {row['accuracy']:>3} {row['search']:>4}  {row['arm']:<11}  "
                  f"{(row['seo_title'] or '(none)')[:56]}")

    print(f"\nScores saved to the scores table under run_label = {RUN_LABEL!r}.")
    print("Nothing was written to proposals or to the store.")
    print("\nTo pull the backlog again later:")
    print("  sqlite3 data/seo.db \"SELECT accuracy, search, arm, seo_title FROM scores"
          " ORDER BY accuracy, search LIMIT 20;\"")
    conn.close()


if __name__ == "__main__":
    main()
