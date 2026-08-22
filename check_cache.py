"""Is prompt caching actually working? Ask the providers, do not assume.

Not the pipeline. Makes a handful of small real calls and reports what the
APIs say about their own cache counters.

Why this exists: a cache that is not working fails SILENTLY. The request
succeeds, the answer is correct, and the only difference is the bill. Both
providers report cache usage in the response, so the question is answerable —
but only if something looks.

Method: send the same prompt prefix twice. The first call populates the cache,
the second should read from it. A second call showing zero cached tokens means
caching is off, however good the code looks.

Usage:
    python3 check_cache.py
"""
import os
import sys
import time

import requests

import generate
import verify

PROBE = "Reply with exactly one word: ok"


def anthropic_probe():
    """Two identical calls with cache_control. The second should read from cache."""
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return None, "ANTHROPIC_API_KEY not set"

    def call():
        response = requests.post(
            verify._ANTHROPIC_ENDPOINT,
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": verify.JUDGE_MODEL,
                "max_tokens": 8,
                "messages": [{
                    "role": "user",
                    "content": [
                        # Byte-identical to what verify.py sends, including the
                        # cache_control marker. Probing a different string would
                        # prove caching works in general and nothing about
                        # whether the real rubric is being cached.
                        {"type": "text", "text": verify._RUBRIC,
                         "cache_control": {"type": "ephemeral"}},
                        {"type": "text", "text": PROBE},
                    ],
                }],
            },
            timeout=60,
        )
        response.raise_for_status()
        return response.json().get("usage", {})

    first = call()
    time.sleep(2)          # the cache is written server-side; give it a beat
    second = call()
    return (first, second), None


def gemini_probe():
    """
    Two calls sharing the real prompt prefix. The second should report
    cachedContentTokenCount above zero.

    Gemini's implicit cache has a minimum prefix length, so this probe uses the
    ACTUAL prompt file rather than a short string — a toy probe would report
    "no caching" for a prompt that caches perfectly well in production.
    """
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        return None, "GEMINI_API_KEY not set"

    prompt_text, version = generate.load_prompt("v5")

    def call(suffix):
        response = requests.post(
            generate._GEMINI_ENDPOINT.format(model_id="gemini-2.5-flash"),
            headers={"Content-Type": "application/json", "x-goog-api-key": key},
            json={"contents": [{"parts": [
                {"text": f"{prompt_text}\n\n## PRODUCT\ntitle: {suffix}\n\n{PROBE}"}
            ]}]},
            timeout=90,
        )
        response.raise_for_status()
        return response.json().get("usageMetadata", {})

    # Different suffixes, identical prefix — exactly the shape of a real run.
    first = call("Probe Sock A")
    time.sleep(2)
    second = call("Probe Sock B")
    return (first, second, version, len(prompt_text) // 4), None


print("=" * 78)
print("PROMPT CACHE CHECK — two identical-prefix calls per provider")
print("=" * 78)

# ── ANTHROPIC ────────────────────────────────────────────────────────────────
print(f"\nANTHROPIC — {verify.JUDGE_MODEL}   (used by verify.py)")
print(f"  rubric length: ~{len(verify._RUBRIC) // 4} tokens")
result, error = anthropic_probe()
if error:
    print(f"  SKIPPED — {error}")
else:
    first, second = result
    for label, usage in (("call 1", first), ("call 2", second)):
        print(f"  {label}: input={usage.get('input_tokens', 0):>5}  "
              f"cache_write={usage.get('cache_creation_input_tokens', 0):>5}  "
              f"cache_read={usage.get('cache_read_input_tokens', 0):>5}")

    read = second.get("cache_read_input_tokens", 0)
    if read > 0:
        print(f"  ✅ CACHING WORKS — {read} tokens served from cache at 0.1x price")
    elif first.get("cache_creation_input_tokens", 0) == 0:
        print("  ❌ NOT CACHING — nothing was even written to the cache.")
        print(f"     The rubric is ~{len(verify._RUBRIC) // 4} tokens; it must clear "
              "the model's minimum (512 on Opus 5, 2048 on Haiku).")
    else:
        print("  ⚠️  Written but not read back. Either the two calls differed, or "
              "the 5-minute\n     window elapsed between them.")

# ── GEMINI ───────────────────────────────────────────────────────────────────
print(f"\nGEMINI — gemini-2.5-flash   (used by generate.py)")
result, error = gemini_probe()
if error:
    print(f"  SKIPPED — {error}")
else:
    first, second, version, approx = result
    print(f"  prompt: listing-{version}.md, ~{approx} tokens")
    for label, usage in (("call 1", first), ("call 2", second)):
        print(f"  {label}: prompt={usage.get('promptTokenCount', 0):>6}  "
              f"cached={usage.get('cachedContentTokenCount', 0):>6}")

    cached = second.get("cachedContentTokenCount", 0)
    if cached > 0:
        total = second.get("promptTokenCount", 1)
        print(f"  ✅ CACHING WORKS — {cached} of {total} prompt tokens "
              f"({100 * cached / total:.0f}%) from cache")
    else:
        print("  ❌ NOT CACHING. Implicit caching needs an identical prefix and a")
        print("     minimum length. Check nothing per-product sits above the prompt")
        print("     text in build_message — that alone disables it for every call.")

print("\n" + "=" * 78)
print("These were real calls and cost a few cents. Nothing was written to the")
print("database or the store.")
