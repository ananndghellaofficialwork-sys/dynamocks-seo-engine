# Listing copy — v2

Corrected 2026-08-17. v1 produced structurally identical output across
different products — same clause order, same filler phrases, only the
noun swapped. That is a template, not copy. This version exists to fix it.

## Language and audience

- American English only — spelling (color, favorite, personalize), vocabulary
  (sneakers not trainers), and idiom. The audience is US-based; nothing British.
- Do not state pack count in the title unless it deviates from the default
  single pair (e.g., a 6-pair gift box). "1 Pair" on a standard single-pair
  listing is noise — every listing is one pair unless stated otherwise.
- Do not assign gender ("for Men" / "for Women") unless the product's actual
  tags or variant data say so. Default grounding across this catalog is
  "men & women" — stating anything narrower without a real tag to back it is
  an invented fact, which is against the grounding rule below.

## Grounding the visual descriptor

- The product's internal name (e.g. "Banger," "MONO," "TRIOS") is GUIDANCE
  only — a weak hint toward the theme, never itself a search term and never
  sufficient grounding on its own for what the design actually looks like.
- The real source of truth is the existing body description (descriptionHtml)
  and tags. Use the name to help interpret them when they're ambiguous — not
  the other way around. If the name suggests one thing and the body copy
  says another, the body copy wins.
- If the body description does not clearly state the visual pattern (color,
  motif, style) even with the name as a hint, do not guess. Set the proposal
  status to needs_human instead of inventing a plausible-sounding pattern —
  a wrong guess published to a live store is worse than a delayed one.
- (Next build, not this version: grounding directly from the product image
  via multimodal input -- see DESIGN-v2.md S12a. Until then, this rule is
  the safeguard against guessing.)

## seo.title

- Under 60 characters.
- Lead with the search term / product theme -- not the brand.
- End with " | Dynamocks" ONLY if it fits without shortening the keyword
  phrase. The keyword phrase gets priority for the character budget --
  it is what actually matches search intent; the brand suffix is a
  bonus, not a requirement.
- NEVER leave empty.

Approved examples (deliberately different structures -- do not converge on one shape):
- "Banana Novelty Crew Socks | Dynamocks"
- "Geometric Print Crew Socks | Dynamocks"
- "Bubble Polka Dot Crew Socks | Dynamocks"

## seo.description

- Ground every claim in the product's actual title, tags, and existing body
  description. Never invent a detail that isn't already there -- including
  pack count, gender, and visual pattern, per the rules above.
- Lead the sentence with what makes THIS product specific -- the pattern,
  theme, or visual detail -- not with generic sock attributes.
- Material and fit details ("breathable combed cotton," "secure fit") are
  OPTIONAL, not mandatory filler. Include them only when they are genuinely
  part of what distinguishes this listing. Do not include them in every
  description just because they were in the last one.
- Sizing / audience language ("sizes 6-12, men & women") is OPTIONAL --
  include only if it adds real information the buyer needs, and vary the
  phrasing every time you do use it.
- SELF-CHECK BEFORE FINALIZING: does this description follow the shape
  "[Name] [theme] crew socks in breathable combed cotton with a secure fit.
  Dynamocks [Name], sizes 6-12, men & women"? If yes, it has converged on
  the old template -- rewrite it with a different opening and a different
  clause order.

Approved examples (four different structures, on purpose):
- Theme-first: "Playful banana print that turns a plain outfit into a
  conversation piece -- soft combed cotton, sizes 6-12."
- Use-case-first: "Everyday crew socks with a bold geometric print, made
  from combed cotton for all-day comfort at the office or out."
- Material-first: "Combed cotton crew socks in a vivid polka-dot print --
  the softness holds up wash after wash."
- Benefit-first: "A fun food-themed pattern for sock drawers that need
  more personality -- cotton crew fit, men and women."

## Closing rule

Both fields. Never one without the other.
