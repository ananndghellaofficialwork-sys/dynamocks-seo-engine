"""
generate.py — stage 3 of the pipeline: LLM drafts -> proposals.

Why this file exists:
  Something has to turn a product row into candidate SEO copy. That is one
  job: read products out of the local mirror, send each one through the
  versioned prompt in prompts/, and append the model's answer to the
  proposals table as a draft. Keeping it in its own module means the copy
  rules, the model choice and the database write are all changeable without
  touching fetch.py or push.py.

What it does:
  - Loads prompts/listing-{version}.md and pairs it with one product's real
    field values, so every claim the model can make is grounded in the live
    listing.
  - Dispatches the call to whichever provider SEO_MODEL names, splitting
    "provider:model_id" on the first colon only.
  - Parses seo_title and seo_description out of the reply and appends one
    proposal row per field.

What it is FORBIDDEN from doing:
  - Never calls Shopify. Only push.py may talk to the store, and only push.py
    may write to it.
  - Never writes to the products table. products is a disposable mirror owned
    by fetch.py; this module reads it and nothing more.
  - Never UPDATEs or DELETEs a proposal. proposals is append-only — a revision
    is a new row pointed at by superseded_by, written by a later stage.
  - Never invents a product fact, and never substitutes one when the model
    omits a field. A missing field is returned as None and reported, because
    a silently back-filled field is the exact defect this project exists to
    remove.
  - Never scores, compares, gates or retries. verify.py is a separate module,
    written by a separate pass, so that nothing which generates output also
    judges it.
"""

import datetime
import json
import os
import re
import sqlite3
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()  # populate os.environ from .env; individual keys are read at call time, not here

PROMPT_DIR = Path("prompts")  # versioned copy rules live here, one file per version

# Provider endpoints. Gemini is the only one wired up in this pass; the other
# two are declared so call_model() can fail with a specific message rather than
# "unknown provider" for a provider that is planned but not yet implemented.
_GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model_id}:generateContent"
_NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
_OPENAI_BASE_URL = "https://api.openai.com/v1"

_TIMEOUT = 60  # seconds; a hung provider must not stall a 400-product run indefinitely

# Matches a labelled field line in the model's reply, tolerating the decoration
# models add around labels: "seo.title:", "**seo_title:**", '"seo_title":',
# "## seo title". Group 1 is title|description, group 2 is whatever followed on
# the same line (empty when the label was a heading and the copy is below it).
_FIELD_LINE = re.compile(
    r"[^\w]*seo[._ ]?(title|description)[^\w:]*:?\s*(.*)",
    re.IGNORECASE,
)

# Characters stripped from both ends of a parsed value: quotes, markdown bold,
# backticks and the trailing comma left behind by a JSON-shaped reply.
_DECORATION = " \t\"'`*,"

_INSERT_PROPOSAL = """
INSERT INTO proposals
(
    gid, field, current_value, proposed_value,
    model, prompt_version, created_at, status
)
VALUES
(
    :gid, :field, :current_value, :proposed_value,
    :model, :prompt_version, :created_at, :status
)
"""


def generate_for_products(
    conn: sqlite3.Connection,
    limit: int,
    model_ref: str,
    prompt_version: str,
) -> None:
    """
    WHAT IT DOES:
      Runs the whole generate stage end to end. Loads the prompt once, pulls
      the products that still have no seo_title, and for each one builds a
      message, calls the model, parses the reply, and appends one proposal row
      per field. Prints a line per product so a run can be watched, commits
      once at the end, then reads the proposals count back out of the database
      and prints it — the count is read from the table, not accumulated in a
      variable, so the number printed is evidence the rows actually landed.

      Called by: the operator, from the REPL, once per run. Nothing in the
                 codebase calls it; this module has no main() by design,
                 because at L0 a human decides when a batch is generated.

      In the pipeline: products table (filled by fetch.py)
                         -> generate_for_products()
                         -> proposals table, status 'draft'
                         -> verify.py   [uniqueness gate and rubric score]

    WHY IT IS ITS OWN FUNCTION:
      The rejected alternative was leaving the loop in the REPL — the operator
      calling get_products_needing_seo(), then call_model(), then
      save_proposal() by hand. That would work for three products and fall
      apart at four hundred, and worse, the commit boundary and the
      two-rows-per-product invariant would live in whatever was typed that
      day rather than in the file. Putting the loop here means one commit at
      the end (a failed run leaves the table untouched rather than half
      filled) and means both fields are always written together.

    WHAT IT RETURNS, AND WHO CONSUMES IT:
      None. The return value is not the point — the proposal rows are.

      The side effect is N committed rows in proposals, at most two per
      product, each with status 'draft'. Fewer than two are written when the
      model omitted a field; that case is printed as a MISSING line and left
      as a hole in the table on purpose, so it shows up in a count rather
      than being papered over.

      verify.py reads those draft rows next, sets uniqueness_status and
      eval_score on them, and moves them along the §6.2 state machine.
    """
    prompt_text, version_tag = load_prompt(prompt_version)
    products = get_products_needing_seo(conn, limit)
    print(f"{len(products)} products need seo_title — generating with {model_ref}")

    for product in products:
        message = build_message(product, prompt_text)
        reply = call_model(message, model_ref)
        fields = parse_response(reply)

        for field in ("seo_title", "seo_description"):
            proposed = fields[field]
            if not proposed:
                # Left deliberately unwritten. proposed_value is NOT NULL, and
                # inserting a placeholder would hide the omission from the
                # count that verify.py and the reviewer work from.
                print(f"  ! {product['handle']} — {field} MISSING, no row written")
                continue

            save_proposal(
                conn,
                gid=product["gid"],
                field=field,
                # The live value at generation time, so a later diff shows what
                # actually changed rather than what the store says today.
                current_value=product["seo_description"] if field == "seo_description" else None,
                proposed_value=proposed,
                model=model_ref,
                prompt_version=version_tag,
            )

        print(f"  {product['handle']}: {fields['seo_title']}")

    conn.commit()  # single commit: a crash mid-run leaves proposals untouched
    total = conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0]
    print(f"done — {total} rows in proposals")


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
    path = PROMPT_DIR / f"listing-{version}.md"
    return path.read_text(encoding="utf-8"), version


def get_products_needing_seo(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    """
    WHAT IT DOES:
      Selects products whose seo_title is still empty — NULL on a product that
      has never had one, empty string on a product where the field exists but
      holds nothing — and returns only the columns the prompt is allowed to
      see. Orders by gid so the same limit returns the same products on a
      repeat run; that repeatability is what makes a three-product test run
      worth anything.

      Called by: generate_for_products(), once per run.

      In the pipeline: products table (written by fetch.py)
                         -> get_products_needing_seo()
                         -> build_message()   [one row at a time]

    WHY IT IS ITS OWN FUNCTION:
      The rejected alternative was an inline SELECT inside the orchestrator's
      loop header. Two things go wrong there. First, the column list is the
      grounding contract — it is the complete set of facts the model may use,
      and that decision deserves to be visible in one place rather than buried
      in a loop. Second, the work-queue rule is going to change: §6.3 defines
      a priority_score that prioritise.py will compute, and when the ordering
      moves from "by gid" to "by priority_score" it is this function that
      changes and nothing else.

    WHAT IT RETURNS, AND WHO CONSUMES IT:
      A list of sqlite3.Row, at most `limit` long, each supporting
      row["title"] style access. An empty list means every product already
      has an seo_title — a legitimate result, not an error, and the caller
      simply generates nothing.

      Each row goes to build_message(), which formats its values into the
      message body, and its gid goes on to save_proposal() as the foreign key
      linking the proposal back to the product it describes.
    """
    rows = conn.execute(
        """
        SELECT
            gid,
            handle,
            title,
            product_type,
            tags,
            seo_description
        FROM products
        WHERE seo_title IS NULL OR seo_title = ''
        ORDER BY gid
        LIMIT :limit
        """,
        {"limit": limit},
    ).fetchall()
    return rows


def build_message(product: sqlite3.Row, prompt_text: str) -> str:
    """
    WHAT IT DOES:
      Joins the copy rules to one product's real field values under a clear
      label, producing the single string sent to the model. Tags are stored as
      a JSON array string and are rendered here as a plain comma list, because
      that is what the rest of the message looks like; if the column does not
      parse as JSON the raw text is passed through rather than dropped.
      Nothing is added that is not in the row — no category assumptions, no
      product knowledge, no examples beyond the ones already in the prompt
      file.

      Called by: generate_for_products(), once per product.

      In the pipeline: load_prompt() + one products row
                         -> build_message()
                         -> call_model()   [the message string]

    WHY IT IS ITS OWN FUNCTION:
      The rejected alternative was f-stringing the product fields together at
      the call site inside the loop. The grounding rule — the model sees these
      facts and no others — is the single most important property of this
      stage, and inlining it would spread that rule across the orchestrator
      where an extra field could be added to the string without anyone
      noticing it happened. One function means the model's entire view of the
      world is one function body long and can be read in ten seconds.

    WHAT IT RETURNS, AND WHO CONSUMES IT:
      One string: the full prompt file text, then a PRODUCT block of labelled
      values. Never empty — a product with every optional column NULL still
      produces the labels, with "(none)" as the value, so the model is told a
      field is absent rather than being left to infer it.

      It goes to call_model(), which is the last function that sees it before
      it leaves the machine.
    """
    try:
        tags = ", ".join(json.loads(product["tags"] or "[]"))
    except (json.JSONDecodeError, TypeError):
        tags = product["tags"]  # not valid JSON — pass the stored text through untouched

    return (
        f"{prompt_text}\n"
        "\n"
        "## PRODUCT\n"
        "These are the only facts about this product. Use nothing else.\n"
        "\n"
        f"title: {product['title']}\n"
        f"handle: {product['handle']}\n"
        f"product_type: {product['product_type'] or '(none)'}\n"
        f"tags: {tags or '(none)'}\n"
        f"current seo.description: {product['seo_description'] or '(none)'}\n"
    )


def call_model(message: str, model_ref: str) -> str:
    """
    WHAT IT DOES:
      Reads "provider:model_id" and routes the call to the matching provider
      function. Splits on the FIRST colon only, because model ids themselves
      contain slashes and sometimes colons — "nvidia:meta/llama-3.3-70b-instruct"
      must arrive at the provider as "meta/llama-3.3-70b-instruct", intact.
      Checks the provider's key is present before dispatching, and raises on
      an unknown provider or a missing key. It never substitutes a different
      provider, and it never retries.

      Called by: generate_for_products(), once per product.

      In the pipeline: build_message()
                         -> call_model()
                         -> _call_gemini() / _call_openai_compatible()
                         -> parse_response()   [the raw reply text]

    WHY IT IS ITS OWN FUNCTION:
      The rejected alternative was calling _call_gemini() directly from the
      orchestrator and adding an if-statement there later when a second
      provider arrived. That fails on the thing this project cares about:
      comparing providers on the same catalog. With the choice living in one
      function driven by one string in .env, switching from Gemini to NVIDIA
      is an .env edit and the proposal rows record which model wrote which
      copy. With the choice inlined it is a code edit, and code edits made to
      run an experiment are how a fallback gets quietly added.

      The silent-fallback ban is enforced here rather than in the provider
      functions on purpose: this is the only place that knows a provider was
      requested but is unavailable, so it is the only place that could be
      tempted to pick another one.

    WHAT IT RETURNS, AND WHO CONSUMES IT:
      The raw text the model produced, exactly as it came back — unparsed and
      untrimmed. Never None: a provider that returns nothing usable raises
      instead, because an empty string here would travel downstream as two
      missing fields and read as a model that declined rather than a call that
      broke.

      It goes to parse_response(), which pulls seo_title and seo_description
      out of it.
    """
    provider, separator, model_id = model_ref.partition(":")  # partition splits once, on the first colon
    if not separator or not model_id:
        raise ValueError(
            f"SEO_MODEL must look like 'provider:model_id', got {model_ref!r}"
        )

    if provider == "gemini":
        return _call_gemini(message, model_id)

    if provider == "nvidia":
        return _call_openai_compatible(
            message, model_id, _NVIDIA_BASE_URL, _require_key("NVIDIA_API_KEY", provider)
        )

    if provider == "openai":
        return _call_openai_compatible(
            message, model_id, _OPENAI_BASE_URL, _require_key("OPENAI_API_KEY", provider)
        )

    raise ValueError(
        f"unknown provider {provider!r} in SEO_MODEL={model_ref!r} — "
        "known providers are: gemini, nvidia, openai"
    )


def _require_key(env_var: str, provider: str) -> str:
    """
    WHAT IT DOES:
      Reads one API key out of the environment and raises a message naming
      both the missing variable and the provider that wanted it, instead of
      the KeyError or the provider's own 401 that would otherwise surface.

      Called by: call_model(), once per call, for whichever provider was
                 selected — never for the others, so a Gemini run does not
                 require an NVIDIA key to exist.

      In the pipeline: .env -> _require_key() -> the provider function's
                       api_key argument.

    WHY IT IS ITS OWN FUNCTION:
      It is three lines and it is called from three branches of call_model().
      The rejected alternative was os.environ["GEMINI_API_KEY"] at each
      branch: that raises KeyError('GEMINI_API_KEY'), which does not say what
      was being attempted, and it invites the .get() form whose None sails
      into the request and comes back as an opaque 401 from the provider.

      Reading keys here rather than at module import — the style fetch.py uses
      — is deliberate: importing this module must not require every provider's
      key to be set, only the one actually being used.

    WHAT IT RETURNS, AND WHO CONSUMES IT:
      The key string. Never returns None or "" — both raise, because an empty
      key produces a 401 that looks like a revoked credential rather than an
      unset one.

      It goes straight into the provider function's Authorization header.
    """
    key = os.environ.get(env_var, "").strip()
    if not key:
        raise RuntimeError(
            f"{env_var} is not set in .env — required for provider {provider!r}. "
            "Refusing to fall back to another provider."
        )
    return key


def _call_gemini(message: str, model_id: str) -> str:
    """
    WHAT IT DOES:
      POSTs the message to Google's generateContent endpoint for the given
      model id, with GEMINI_API_KEY in the x-goog-api-key header, and digs the
      generated text out of the response. Raises on a non-2xx status, and
      raises separately when the response is well formed but carries no text —
      a stop for safety filters or a truncation both produce a 200 with no
      usable part, and the finishReason is named in the error so the two can
      be told apart.

      Called by: call_model(), once per product, when SEO_MODEL names gemini.

      In the pipeline: call_model()
                         -> _call_gemini()  [the only network call in this file]
                         -> parse_response()

    WHY IT IS ITS OWN FUNCTION:
      Private and separate from call_model() because it is the half that
      changes for reasons that have nothing to do with the other half. The
      rejected alternative was one function holding both the routing and the
      HTTP: Google's request shape, its header name and its response nesting
      are all specific to Google and none of them are stable, while the
      routing rule — split once on the colon, never fall back — is a project
      rule that should not be edited when a vendor moves a JSON key.

    WHAT IT RETURNS, AND WHO CONSUMES IT:
      The text of the first candidate, as a string, with no cleanup applied —
      markdown fences, labels and preamble all still attached. Never None and
      never "": both raise, so that "the call failed" and "the model omitted a
      field" stay separable further down.

      It goes to parse_response(), which is the function that knows how to
      find seo_title and seo_description inside it.
    """
    response = requests.post(
        _GEMINI_ENDPOINT.format(model_id=model_id),
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": _require_key("GEMINI_API_KEY", "gemini"),
        },
        json={"contents": [{"parts": [{"text": message}]}]},
        timeout=_TIMEOUT,
    )
    response.raise_for_status()  # Gemini signals failure with the HTTP status, unlike Shopify GraphQL
    body = response.json()

    candidates = body.get("candidates") or []
    if not candidates:
        # Blocked before generation started; promptFeedback carries the reason.
        raise RuntimeError(f"gemini returned no candidates: {body.get('promptFeedback')}")

    parts = candidates[0].get("content", {}).get("parts") or []
    text = "".join(part.get("text", "") for part in parts)
    if not text.strip():
        raise RuntimeError(
            f"gemini returned an empty candidate, finishReason="
            f"{candidates[0].get('finishReason')!r}"
        )
    return text


def _call_openai_compatible(
    message: str,
    model_id: str,
    base_url: str,
    api_key: str,
) -> str:
    """
    WHAT IT DOES:
      Not implemented in this pass — raises NotImplementedError.

      When filled in it will POST to {base_url}/chat/completions with a Bearer
      token, a messages list holding one user message, and read the reply from
      choices[0].message.content — the request and response shape both NVIDIA
      NIM and OpenAI accept unchanged.

      Called by: call_model(), once per product, for provider 'nvidia' or
                 'openai'. Both routes land here; they differ only in the
                 base_url and api_key passed in.

      In the pipeline: call_model()
                         -> _call_openai_compatible()
                         -> parse_response()

    WHY IT IS ITS OWN FUNCTION:
      It is one function rather than two — _call_nvidia() and _call_openai()
      was the rejected alternative — because the difference between those two
      providers is entirely a hostname and a key, and two functions would mean
      two copies of the same parsing that then drift when one gets a fix. Both
      are parameters, so both are arguments.

      It is separate from _call_gemini() for the opposite reason: Gemini's
      shape genuinely differs (x-goog-api-key rather than Bearer, contents
      rather than messages, candidates rather than choices), so folding them
      together would produce a function that is mostly branching on which
      vendor it is talking to.

      It exists now, empty, so that call_model()'s routing table is complete
      and testable today: asking for an unimplemented provider raises
      NotImplementedError, which is a different and more honest failure than
      "unknown provider" for something that is merely unfinished.

    WHAT IT RETURNS, AND WHO CONSUMES IT:
      Nothing yet. Once implemented: the assistant message content as a
      string, matching _call_gemini()'s contract exactly — raw text, never
      None, never empty, raising rather than returning "" — because
      parse_response() consumes both without knowing which provider it came
      from, and that only holds if the two return the same thing.
    """
    raise NotImplementedError(
        "_call_openai_compatible is not implemented yet — "
        "only provider 'gemini' works in this pass"
    )


def parse_response(text: str) -> dict:
    """
    WHAT IT DOES:
      Scans the reply for lines labelled seo_title and seo_description and
      pulls the value off each, tolerating the decoration models put around
      labels — markdown bold, headings, list bullets, JSON quoting. When the
      label was a heading with nothing after it, the following non-empty line
      is taken as the value. Surrounding quotes, asterisks, backticks and a
      trailing comma are stripped from what it finds. First match wins.

      A field that is absent, or present but empty, comes back as None. It is
      never guessed at, never defaulted, and never filled from the product
      title — that substitution is the defect this whole project exists to
      remove, and it would be invisible in the output if it happened here.

      Called by: generate_for_products(), once per product.

      In the pipeline: call_model()  [raw model text]
                         -> parse_response()
                         -> save_proposal()   [one call per non-None field]

    WHY IT IS ITS OWN FUNCTION:
      The rejected alternative was parsing inline in the orchestrator. Model
      output shape is the part of this pipeline most likely to break — a new
      model version starts wrapping its answer in a code fence, or in JSON,
      and everything downstream sees two missing fields. Isolated here, that
      is one function to fix and one function to test against a saved reply,
      rather than surgery on a loop that also owns the database transaction.

      Known limitation, left in on purpose: if a model echoes the prompt's own
      "## seo.title" headings back before answering, the rule text underneath
      is what gets captured. That produces visibly wrong copy that verify.py
      and the reviewer will catch, which is preferable to adding heuristics
      here that guess which occurrence was meant.

    WHAT IT RETURNS, AND WHO CONSUMES IT:
      A dict with exactly two keys, always present: {"seo_title": str|None,
      "seo_description": str|None}. Both keys exist even when both values are
      None, so the caller can loop over the pair without checking membership.
      None means "the model did not give us this" — a fact, and one the caller
      prints rather than repairs.

      generate_for_products() reads both keys; each non-None value becomes one
      proposals row via save_proposal(), and each None becomes a printed
      MISSING line and no row at all.
    """
    fields = {"seo_title": None, "seo_description": None}
    if not text:
        return fields

    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = _FIELD_LINE.match(line)
        if not match:
            continue

        key = "seo_" + match.group(1).lower()
        if fields[key] is not None:
            continue  # first match wins; a later repeat is an echo, not a correction

        value = match.group(2).strip(_DECORATION)
        if not value:
            # Heading form: the label was on its own line, the copy is below it.
            for following in lines[index + 1:]:
                if not following.strip():
                    continue
                if _FIELD_LINE.match(following):
                    break  # ran straight into the next label — this field is empty
                value = following.strip(_DECORATION)
                break

        fields[key] = value or None  # empty after stripping is an omission, not a value

    return fields


def save_proposal(
    conn: sqlite3.Connection,
    gid: str,
    field: str,
    current_value: str | None,
    proposed_value: str,
    model: str,
    prompt_version: str,
) -> None:
    """
    WHAT IT DOES:
      Appends one row to proposals: the product it belongs to, which field it
      is for, the live value snapshotted at generation time, the candidate
      text, and the model and prompt version that produced it. Stamps
      created_at as ISO-8601 UTC and status as 'draft'. Does not commit — the
      caller commits once after the whole batch.

      Called by: generate_for_products(), once per field per product, so twice
                 per product when the model returns both.

      In the pipeline: parse_response()  [one field's text]
                         -> save_proposal()
                         -> proposals table
                         -> verify.py     [reads the draft rows next]

    WHY IT IS ITS OWN FUNCTION:
      The rejected alternative was conn.execute() with the INSERT inline in
      the loop. The append-only rule is the thing being protected: proposals
      is never UPDATEd and never DELETEd, because the table is the audit trail
      that makes a bad batch traceable to the prompt and model that caused it.
      One function holding the only write to that table means the rule is
      enforceable by reading one function, and a stray UPDATE anywhere else in
      the module is obviously out of place.

      It also fixes the two values that must not vary per call site —
      created_at's format and status's initial state — so a row generated in a
      later session sorts and filters alongside the ones generated today.

    WHAT IT RETURNS, AND WHO CONSUMES IT:
      None. The side effect is one row staged in the caller's open
      transaction, not yet durable — nothing is readable by another connection
      until generate_for_products() commits.

      The row's consumer is verify.py, which selects the draft rows, sets
      uniqueness_status, max_similarity and eval_score on them, and advances
      status along the §6.2 state machine. review.py exports them to CSV after
      that, and push.py only ever touches rows that have passed both gates.
    """
    conn.execute(
        _INSERT_PROPOSAL,
        {
            "gid": gid,
            "field": field,
            "current_value": current_value,
            "proposed_value": proposed_value,
            "model": model,  # full provider:model_id — two providers can serve the same id
            "prompt_version": prompt_version,
            "created_at": datetime.datetime.now(datetime.timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "status": "draft",
        },
    )
