# SEO Field & Aspect Inventory — Dynamocks

**Compiled 7 Aug 2026. Every "current state" below was read from the live store, not assumed.**

This is the full surface the pipeline should **audit**. It is not the surface it is allowed to **write** — write scope widens with the autonomy ladder in `DESIGN-v2.md`. Tier column = when it becomes writable.

---

## A. Product-level fields

| # | Field | API path | Current state (live) | Why it matters | Risk | Tier |
|---|-------|----------|---------------------|----------------|------|------|
| A1 | **Meta title** | `product.seo.title` | **null on ~99% of 472 products** | The clickable headline in Google. Null = Google falls back to the product title, which you don't control the length or shape of | None — empty slot | **1** |
| A2 | **Meta description** | `product.seo.description` | Populated, **known byte-identical duplicate clusters** | Doesn't rank, but drives click-through. Duplicates across siblings signal thin content | Low — reversible | **2** |
| A3 | **Product title (H1)** | `product.title` | Recently rewritten by hand | Strongest single on-page signal. Also the meta-title fallback | **Brand voice — your wife owns this** | 5 |
| A4 | **Body description** | `product.descriptionHtml` | Good quality — **but every one ends in a keyword-stuffed pipe string** | Keyword depth, entity coverage, answers buyer questions | Customer-facing | 3 |
| A5 | **URL handle** | `product.handle` | **Legacy stuffed slugs**, e.g. `allure-colorful-cool-design-fashionable-casual-mens-socks-stylish-designer-womens-socks-collection` | Minor ranking signal, major user/CTR signal | **🔴 404s + lost authority unless a redirect is written first** | 5 |
| A6 | **Product type** | `product.productType` | Populated (`Crew`) | Feeds faceted navigation and Google Shopping taxonomy | Low | 2 |
| A7 | **Tags** | `product.tags` | Populated (`Artistic Step`, `Crew Length`, `Crew Pack Of 1`…) | Drives automated collections → which are indexable pages | Low, but can silently move products between collections | 2 |
| A8 | **Vendor** | `product.vendor` | `Dynamocks` | Brand entity consistency | None | 2 |
| A9 | **Status / publication** | `product.status`, publications | Some `ACTIVE` with `totalInventory: 0` | An indexed, unbuyable page is a poor result and wastes crawl budget | Commercial decision | — |
| A10 | **Variant option names** | `variants.selectedOptions` | Sizes 6–12 | Long-tail size queries ("size 13 crew socks") | Low | 3 |
| A11 | **Price / availability** | — | — | **Read-only for SEO.** Feeds structured data and Shopping. Never written by this pipeline | — | ❌ never |

---

## B. Collection-level — **the biggest untapped surface on the store**

**26 of your 36 collections have a null `seo.title`.** Collection pages compete for category-level terms ("cotton crew socks men", "novelty socks"), which have far higher volume than any single product term.

| Collection | Products behind it | `seo.title` | Note |
|---|---|---|---|
| **All Products** | 382 | ❌ null | Probably should be `noindex` rather than optimised |
| **Crew Pack of 1** | **149** | ❌ null | Largest genuinely optimisable gap |
| **Black Friday Deal** | 110 | ❌ null | Out of season — seasonal handling needed, not copy |
| **Best Sellers** | **109** | ❌ null | Highest commercial intent page on the site |
| **Ankle Pack of 1** | **92** | ❌ null | |
| **Thinnest Summer Invisible Socks** | 68 | ❌ null | Strong long-tail term already in the title |
| **Holiday Gifts** | 64 | ❌ null | Seasonal, high intent |
| **The Fall Favorites** | 55 | ❌ null | Seasonal |
| **Crew Combos** | 52 | ❌ null | |
| **Deals** | 52 | ❌ null | |
| **The Geometrics Collection** | 51 | ❌ null | |
| **Invisible Pack of 1** | 39 | ❌ null | |
| **Monochrome Magic** | 34 | ❌ null | |
| **Dotty Delight** | 32 | ❌ null | |
| **New Arrival** | 31 | ❌ null | |
| **Ankle Combos / packs, Low Ankle, Mystery Box, Artistry, Home page** | 4–24 each | ❌ null | Long tail |
| Quarter Length, Crew Length, Solid Steps, Savvy Stripes, Cotton Excellence, Bamboo, Food & Fun, Signature No-Show, Ankle Combos, 6-Pair Multipacks | 12–228 | ✅ written | Use these as the house style reference |

**Fields per collection:** `collection.seo.title`, `collection.seo.description`, `collection.descriptionHtml`, `collection.handle`, collection image + alt.

**🔴 Defect found:** `Ankle Pack of 3` has raw product copy pasted into its meta description — it literally begins *"Product description … Technical specification …"*. That's a broken field, not a thin one.

**⚠️ Collection-level cannibalisation:** `Crew Length` (228), `Crew Pack of 1` (149), `Crew Combos` (52) and `Crew Pack Of 3/4` all target overlapping intent. Needs a decision on which page owns "crew socks" — the others should support it, not compete with it.

---

## C. Media

| # | Field | Current state | Why it matters | Tier |
|---|-------|--------------|----------------|------|
| C1 | **Image alt text** | **Empty on roughly half the images sampled** (Allure 2 of 3 blank, Ace 1 of 3 blank) | Google Images traffic, accessibility compliance, on-page context | **1** — lowest-risk field on the store |
| C2 | Image filenames | Not audited | Weak signal, but free at upload time | 4 |
| C3 | Image count / quality per product | Varies | Engagement + conversion, indirect ranking | — |
| C4 | Video / 3D media | Not audited | Dwell time | — |

---

## D. Metafields & structured data

| # | Item | Current state | Why it matters | Tier |
|---|------|--------------|----------------|------|
| D1 | `global.description_tag` | Present | Legacy meta-description metafield — **must be reconciled with `seo.description` or the two can disagree** | 2 |
| D2 | `mm-google-shopping.*` | `age_group`, `gender`, `condition`, `color`, `custom_product` present | Google Shopping feed quality → free Shopping surfaces | 4 |
| D3 | `judgeme.badge` / `judgeme.widget` | Present | **Review data already exists — the raw material for ★ rating rich snippets** | 4 |
| D4 | Product JSON-LD (schema.org `Product`, `Offer`, `AggregateRating`) | Theme-level, not audited | Rich results: price, stock, stars in the SERP. Highest visible-CTR win available | 4 |
| D5 | `BreadcrumbList` JSON-LD | Not audited | Breadcrumb display in results | 4 |
| D6 | `Organization` / `LocalBusiness` JSON-LD | Not audited | Brand entity, knowledge panel | 4 |
| D7 | FAQ content + `FAQPage` markup | Absent | Long-tail question queries | 4 |

---

## E. Site & technical

| # | Item | Where | Why it matters | Tier |
|---|------|-------|----------------|------|
| E1 | Title/description templates in theme | `theme.liquid` | Sets the fallback shape for every page that has no explicit meta title | 4 |
| E2 | Canonical tags | Theme | Variant and filtered URLs create duplicates | 4 |
| E3 | `robots.txt` / `noindex` rules | Shopify settings | `All Products` (382) and expired seasonal collections should not compete with real pages | 4 |
| E4 | `sitemap.xml` coverage | Auto-generated | Confirms what Google can actually find | audit only |
| E5 | URL redirects | Shopify redirects | **The prerequisite for ever touching a handle.** Redirect written before the change, always | 5 |
| E6 | Core Web Vitals / page speed | Theme + images | Real ranking factor and a conversion factor | audit only |
| E7 | Mobile rendering | Theme | Majority of traffic | audit only |
| E8 | Internal linking between collections and products | Theme + body copy | How authority flows through the site. Currently one hand-written link found | 4 |
| E9 | Pagination / faceted URL handling | Theme | Crawl budget, duplicate content | 4 |
| E10 | Hreflang | — | Only if you sell into multiple regions | — |
| E11 | 404s and broken links | — | Lost authority | audit only |

---

## F. Content & off-page — not code, but real

| # | Item | Current state | Note |
|---|------|--------------|------|
| F1 | Blog / articles | Not audited | Where informational queries get captured ("how to stop socks slipping") |
| F2 | Buying guides, size guides | Not audited | Long-tail + conversion support |
| F3 | Customer reviews on-page | Judge.me installed | Fresh UGC + rich snippet eligibility |
| F4 | Backlinks / PR | — | Off-platform, outside this pipeline |
| F5 | Google Business Profile | — | Brand entity |
| F6 | Google Search Console | Not connected to the pipeline | **The only source of truth for whether any of this worked.** Impressions, clicks, position, per URL |

---

## G. Anti-patterns to REMOVE — audit findings, not gaps to fill

**These look like SEO and are liabilities. An audit that only counts empty fields will miss all of them.**

| # | Anti-pattern | Evidence | Fix |
|---|-------------|----------|-----|
| G1 | **Keyword-stuffed footer strings in body copy** | Every product ends with *"Allure Colorful Designer Cotton Crew Socks \| Colorful Crew Socks Men \| Cotton Crew Socks \| Comfortable Crew Socks \| Breathable Cotton Socks \| Allure Crew Socks \| Men & Women Crew Socks"* | Strip. 2015-era tactic, now a low-quality signal |
| G2 | **Stuffed URL handles** | `allure-colorful-cool-design-fashionable-casual-mens-socks-stylish-designer-womens-socks-collection` | Shorten — **only with a 301 redirect written first** |
| G3 | **Duplicate meta descriptions across siblings** | The 8 renamed packs share byte-identical descriptions | The uniqueness gate exists for this |
| G4 | **Raw copy dumped into a collection meta description** | `Ankle Pack of 3` begins *"Product description … Technical specification"* | Rewrite |
| G5 | **Indexable near-empty / duplicate collections** | `All Products` (382), `Home page` (0), `Low Ankle Pack of 3` (0) | `noindex` or consolidate |
| G6 | **Collection cannibalisation** | Crew Length / Crew Pack of 1 / Crew Combos / Crew Pack of 3/4 all chase "crew socks" | Decide which page owns the term |
| G7 | **Indexed out-of-stock products** | Active products at `totalInventory: 0` | Commercial decision + crawl budget |
| G8 | **Expired seasonal pages left live** | `Black Friday Deal` (110 products), in August | Seasonal handling policy |

---

## H. What the audit step must therefore measure

Not "which fields are empty" — that misses half of section G. The audit produces, per entity:

1. **Coverage** — is the field populated at all
2. **Quality** — length, keyword presence, readability, does it match the product
3. **Uniqueness** — semantic distance from every other entity's same field
4. **Risk flags** — the section G anti-patterns
5. **Opportunity value** — coverage gap × revenue × traffic potential

**Then, and only then, a prioritised queue.** A field being empty is not the same as a field being worth filling: `Crew Pack of 1` (149 products, null meta title) and `Low Ankle Pack of 3` (0 products, null meta title) are both "empty" and only one is worth a session.

---

## I. Where to start — highest value, zero risk

All three are empty slots. Nothing existing can be damaged.

1. **26 collection `seo.title` + `seo.description`** — hundreds of products' worth of category-intent traffic sitting behind unoptimised pages. Start with **Best Sellers (109)**, **Crew Pack of 1 (149)**, **Ankle Pack of 1 (92)**.
2. **472 product `seo.title`** — null on ~99%.
3. **Image alt text** — blank on roughly half the media, and the single lowest-risk field on the entire store.

**Measurement:** none of this is provable without **Google Search Console** connected. Impressions and average position per URL, before and after, is the only honest answer to "did it work?" — and it's the number your wife should see, not a count of fields filled.
