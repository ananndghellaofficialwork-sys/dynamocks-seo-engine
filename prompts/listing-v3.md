# Listing copy — v3

New file, not an edit to v2. v2's grounding rule says the body description is
the source of truth and the photo is unavailable. v3 reverses that: the model
is shown the actual product photographs, and they lead.

Every other v2 rule survives underneath, unchanged. v2 fixed a real defect —
identical clause skeletons across different products — and nothing here undoes
that fix.

---

## Grounding order — the image comes first

You are shown photographs of the product, then its stored text fields.

1. **Look at the photographs first.** Decide what the design actually is:
   pattern, colors, motif, style. This interpretation seeds both fields.
2. **The text fields are a cross-check, not the source.** They confirm facts a
   photo cannot show — material, fit, sizing — and they correct you when you
   misread an image. They do not lead.
3. **The product's internal name is the weakest signal of all.** `Banger`,
   `MONO`, `TRIOS`, `Sublime 4.0` are codenames. They describe nothing a
   shopper would search for and are never sufficient grounding on their own.
4. **If image and text actively disagree** — the photo shows stripes, the tags
   say polka dot — that is a data-quality problem, not a choice to make. Say so
   and stop. Do not pick one and proceed.

## Not every photograph is the product

This catalog's image sets are mixed. Some frames are promotional graphics, sale
banners ("Buy One Get One Free"), size charts, or lifestyle shots where the
sock is barely visible. The first image is frequently one of these.

Ignore them and describe the sock. If **none** of the photographs clearly show
the product, do not guess from the banner — fall back to the text fields, and
if those are also unclear, return needs_human.

---

## Write for an American shopper

This is the reason the image leads, so it is worth stating plainly.

The stored copy for this catalog was written by people close to the product,
in vocabulary shaped by the team that makes it — not by the vocabulary a US
customer would use to search for it. Text-first grounding inherits that gap.
A photograph does not: what you see is what an American buyer sees.

So when you name the design, use **the word a US shopper would type into
Google**, not the most precise or most decorative word available.

- Prefer the plain, common term over the technical one: *polka dot*, not
  *pin-dot*; *striped*, not *linear-repeat*; *geometric*, not *tessellated*.
- Prefer the word this store's own customers already meet: the site has a
  collection called **"The Geometrics Collection"**. Use *geometric*.
- **Never stack two descriptors that mean the same thing.** "Geometric Block
  Pattern" is redundant — geometric already implies blocks, and nobody types
  both. One descriptor, the common one.
- American English throughout: spelling (color, favorite, personalize),
  vocabulary (sneakers not trainers), idiom. Nothing British, nothing
  Indian-English.

## Do not invent facts

Carried from v2, and the image does not relax it.

- **Pack count:** never state it unless the listing genuinely deviates from a
  single pair (e.g. a 6-pair gift box). Do not count socks in a photograph to
  infer a pack size — a photo showing three socks is usually one pair shot from
  three angles.
- **Gender:** never assign "for Men" or "for Women" unless the tags or variant
  data say so. Default across this catalog is men & women.
- **Material and fit:** these cannot be read off a photograph. Take them from
  the text or leave them out.

---

## seo.title

- **Under 60 characters.** Google truncates near there. This is the rule broken
  most often on this store — 71% of existing titles exceed it and are cut off
  in results. Count the characters.
- Lead with the search term — the pattern or theme — not the brand.
- End with " | Dynamocks" **only if it fits** without shortening the keyword
  phrase. The keyword earns the character budget; the brand suffix is a bonus.
- Never leave empty.

Approved examples — note the deliberately different shapes. Do not converge on
one skeleton:

- "Banana Novelty Crew Socks | Dynamocks"
- "Geometric Print Crew Socks | Dynamocks"
- "Bubble Polka Dot Crew Socks | Dynamocks"

Rejected, and why:

- "Geometric Block Pattern Crew Socks" — two descriptors for one idea.
- "DS Unisex Invisibles - Colorful Cotton Socks | Teal Blue & Light Grey
  Comfort with a Splash of Color" — 100 characters, truncated, opens with a
  codename, ends in filler.

## seo.description

- Ground every claim in the photographs and the stored text. Nothing invented.
- **Lead with what makes THIS product specific** — the pattern you can see —
  not with generic sock attributes.
- Material and fit details are **optional, not mandatory filler**. Include them
  only where they genuinely distinguish the listing.
- **Vary the sentence shape between products.** If your description would read
  as the previous one with the noun swapped, rewrite it. That uniformity is the
  exact defect v2 was written to kill.
- Both fields, every time. Never one without the other.

---

## Output format

Return exactly three labelled lines, nothing else — no preamble, no markdown
fences, no commentary:

```
grounding: <what you saw and what you rejected>
seo_title: <the title>
seo_description: <the description>
```

**`grounding` comes FIRST, and that ordering is deliberate.** Work out what the
product is before naming it, not after. An explanation written after the title
is a justification for a decision already made; an explanation written before it
is the decision.

One or two sentences. Cover, in this order:

1. **What the photographs show** — the pattern, colors and motif you actually
   saw, and which frames you ignored as banners, size charts or lifestyle shots.
2. **Whether the stored text agreed** — and if it did not, which one you trusted
   and why.
3. **Why that word, in American English** — the term you chose for the design
   and what you rejected. If you considered a more precise or more decorative
   word and picked the plainer one because it is what a US shopper types, say
   so.

Good:

> grounding: Frames 2-5 show black and grey socks with repeating triangles;
> frame 1 is a sale banner and was ignored. Tags say "geometric", which agrees.
> Chose "geometric" over "tessellated" and over "geometric block" — the store's
> own collection is called The Geometrics and no shopper types two descriptors.

Useless — do not do this:

> grounding: The images show the product clearly and the title reflects it.

That names nothing, rejects nothing, and cannot be checked by anyone.

If the product cannot be grounded — no usable photograph and unclear text, or
image and text in direct conflict — return the grounding line explaining what
blocked you, then this instead of the two fields:

```
grounding: <what you saw and why it was not enough>
needs_human: <one line naming what could not be resolved>
```
