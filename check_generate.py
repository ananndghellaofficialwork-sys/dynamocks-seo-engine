"""Throwaway harness for generate.py. Not part of the pipeline.

Usage:  python3 check_generate.py [limit]
"""
import sys, collections
from db import connect
import generate

LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 5
MODEL = "gemini:gemini-2.5-flash"

print(f"python   : {sys.executable}")
conn = connect()

if conn.in_transaction:
    print("!! open transaction found — rolling back")
    conn.rollback()

before = conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0]
print(f"proposals: {before} before\n" + "-" * 60)

generate.generate_for_products(conn, limit=LIMIT, model_ref=MODEL, prompt_version="v1")

after = conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0]
new = after - before
print("-" * 60)
print(f"proposals: {after} after   new={new}   expected={LIMIT * 2}")
if new != LIMIT * 2:
    print("!! WRONG ROW COUNT — expected two rows per product")

rows = conn.execute(
    "SELECT proposed_value FROM proposals WHERE field='seo_title' "
    "ORDER BY id DESC LIMIT ?", (new // 2 or LIMIT,)
).fetchall()

print("\nTITLES\n" + "-" * 60)
bad = 0
for r in rows:
    v = r["proposed_value"] or ""
    flags = []
    if not v.strip():                 flags.append("EMPTY")
    if len(v) > 60:                   flags.append(f"TOO LONG {len(v)}")
    if not v.endswith("| Dynamocks"): flags.append("NO SUFFIX")
    bad += bool(flags)
    print(f"{len(v):>3}  {v}" + (f"   <-- {', '.join(flags)}" if flags else ""))

print("-" * 60)
print(f"{len(rows) - bad}/{len(rows)} passed the mechanical checks")

lead = collections.Counter(v["proposed_value"].split()[0].lower()
                           for v in rows if (v["proposed_value"] or "").strip())
print("\nLEAD WORDS (repetition = keyword cannibalisation)")
for word, n in lead.most_common(8):
    print(f"  {n:>2}x  {word}" + ("   <-- too generic, fix the prompt" if n > 1 else ""))

dupes = [t for t, n in collections.Counter(
    r["proposed_value"] for r in rows).items() if n > 1]
print(f"\nexact duplicate titles: {len(dupes)}")
for d in dupes:
    print(f"  {d}")

conn.close()
