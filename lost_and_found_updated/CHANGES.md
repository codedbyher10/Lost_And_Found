# Knit — Change Log

This document records every change made to the project, grouped by area. It
covers the full **Knit brand redesign**, the backend reliability fixes, and
the improved multi-modal AI matching engine.

---

## 0. Brand Redesign — Knit

The UI was redesigned the way a brand manager would approach it: an identity
first, a system second, then applied consistently to every screen.

### Identity
- **Name: Knit** — "knit things back together."
- **Mark**: two interlocked circles joined by a bond line (two halves knit
  back together), rendered as inline SVG via `window.brand()` in `app.js`
  (used in nav, footer, and the claimed confirmation page).
- **Voice**: warm, calm, precise. Short sentences. No corporate filler.
  Tagline on the hero: *"Everything finds its way back home."*

### Color — refined blue
- Accent moved from flat `#5b8def` to a **premium, slightly desaturated blue**
  (`#6e9bff` dark / `#2e5bd8` light) with proper strong/soft/border tokens.
- Deeper charcoal neutrals with **three surface steps** (`--surface`,
  `--surface-2`, `--surface-3`) for real elevation instead of flat panels.
- Semantic colors (success/warning/danger/info) refined to match.

### Typography
- Sora (display) + Manrope (body) retained; hierarchy tightened with a fluid
  scale (`clamp`), tighter display letter-spacing (`-0.02em`…`-0.03em`), and
  consistent heading weights.

### System & components
- **Buttons**: disciplined 40px height (34/48 for sm/lg), inner highlight,
  hover shadow, pressed state, consistent focus ring.
- **Nav**: pill-shaped active state (brand-soft) instead of the underline,
  glass blur retained, brand mark left, CTA right, mobile drawer.
- **Hero**: signature display headline, brand ornament watermark (solid tint,
  no gradient/blur), refined meta strip.
- **Cards**: subtle 1px borders + three-step surfaces, gentle hover lift and
  image zoom; hierarchical featured card.
- **Forms**: uppercase-leaning labels, taller inputs, unified focus ring,
  refined dropzone/upload and location chips.
- **Empty states**: dedicated `.empty-state` component with a soft tile.
- **Motion**: purposeful micro-interactions; honors `prefers-reduced-motion`.
- **Accessibility**: global `:focus-visible` ring, `aria-hidden` ornaments.

### Match results — surfaces the AI
`matches.html` now shows the **fused confidence** as a large headline
percentage + quality chip (Strong / Good / Possible), the main score bar, and
two sub-bars — **Visual** and **Description** — driven by the new
`visual_score` / `text_score` fields from the backend. Copy updated to reflect
that matching is no longer "same-category only."

### Pages
All seven pages re-skinned and copy-walked: `index.html` (new hero, features,
steps, footer), `report-found.html`, `report-lost.html`, `matches.html`,
`item.html`, `my-items.html`, `claimed.html` (brand knot instead of the old
checkmark, confetti retained). Old wordmark and `Lost & Found` references
removed everywhere.

---

## 1. AI Matching Engine — Multi-Modal Fusion (v8.2.0)

The single biggest accuracy upgrade. Previously the app ranked candidates
**by photo similarity alone** (`client.query_points` on one image vector).
Now it fuses **visual + textual** similarity produced by the *same* CLIP
model, plus a soft category bonus.

### Why it matters
- A photo taken at a bad angle/lighting can score low visually even though the
  text clearly describes the same object ("black leather wallet" vs.
  "leather wallet, black").
- The old engine also used a **hard exact-category filter**, which silently
  discarded real matches whenever two people labeled the same item with
  different categories (e.g. "Electronics" vs. "Other").
- Result ranking was non-deterministic when scores tied.

### What changed
1. **Two named vectors per collection** (`image` and `text`, both 512-dim,
   COSINE) in a shared CLIP latent space — `ensure_collection`.
2. **New helpers**
   - `get_text_embedding(text)` — CLIP-embeds an item's `category + name`.
   - `cosine_similarity(a, b)` — numpy cosine for the text re-rank.
3. **Fused scoring** — `search_potential_matches(...)`:
   - Qdrant pre-filters candidates by image similarity (`VIS_PREFILTER = 0.45`).
   - Final score = `0.55 * visual + 0.35 * text + 0.08 (if categories agree)`.
   - `MATCH_THRESHOLD = 0.6` filters the *fused* score.
   - **Deterministic ranking**: fused score desc, ties broken by newest upload
     first.
4. **Endpoints store both vectors** at report time
   (`report_found_item`, `report_lost_item` upsert `{"image":…, "text":…}`).
5. **`get_potential_matches_for_item`** reads the stored image + text vectors
   and re-runs the fused search.
6. **Response transparency**: each match now returns `score` (fused),
   `visual_score` and `text_score` so the UI can show *why* an item matched.

### Tunable constants (in `main.py`)
```
MATCH_THRESHOLD = 0.6    # min fused score to be a match
MATCH_LIMIT      = 6      # max matches returned
VIS_PREFILTER    = 0.45   # image-only candidate cutoff
W_VISUAL         = 0.55   # visual weight
W_TEXT           = 0.35   # text weight
CATEGORY_BONUS   = 0.08   # bonus when categories agree
PREFETCH_FACTOR  = 5      # candidate oversampling before re-rank
```

---

## 2. Backend Reliability Fixes (`main.py`)

- **Data persistence**: removed the on-startup database wipe. Collections are
  created only if missing (`ensure_collection` still preserves existing data),
  so reported items survive restarts.
- **Error handling correctness**: added `except HTTPException: raise` so client
  errors (e.g. 400 "item already claimed") no longer get wrapped into 500s.
- **400s instead of silent 500s** for invalid collection names and for claiming
  two items from the same collection.
- **Claim / returned status flow**: claim sets both sides to `claimed` with
  `matched_with_id` / `matched_with_name`; `mark_as_returned` cascades the
  `returned` status to the matched opposite item.
- **Path robustness (fixes broken images)**: all filesystem paths were relative
  to the process working directory, so uploaded images and static assets 404'd
  when uvicorn was launched from another folder. Now resolved against the
  script location via `BASE_DIR`:
  ```
  BASE_DIR      = dirname(abspath(__file__))
  TEMPLATES_DIR
  STORAGE_DIR   # qdrant_storage
  IMAGES_DIR    # uploaded_images
  STATIC_DIR    # static
  ```
  These are used by the Jinja2 env, `QdrantClient`, `save_image`, and the
  `/images` + `/` static mounts.
- **Version string** bumped to `8.2.0`.

---

## 3. Frontend Redesign — Polished, Non-"AI-Slop" UI (static/)

All seven pages were rewritten to a shared design system.

### Design tokens (`static/css/styles.css`)
- **4px spacing grid**: `--s1` … `--s16`.
- **Small deliberate radii**: `--r-sm: 6px`, `--r-md: 8px`.
- **Dark mode is the default** (`#0b0f19` bg, `#12161f` surface, `#f3f4f6` text)
  with a light theme via a `data-theme` attribute.
- **Single accent color** `--brand: #5b8def`; subdued semantic
  `--success / --warning / --danger`.
- **No purple/gradient/blur decorations.** No generic "sparkle" icons.
- **Typography**: Sora (display) + Manrope (body). No Inter/Roboto/Arial.

### Theme toggle & icons (`static/js/app.js`)
- **`window.icon(name)`** returns hand-rolled inline SVG per icon
  (no feather/tailwind/icon-font dependency).
- **`[data-theme-toggle]`** moon/sun button toggles
  `data-theme` + `localStorage('lf-theme')`. A tiny per-page inline script
  re-applies the saved theme *before* the CSS loads to avoid a flash.
- Hamburger/mobile nav toggle.

### Page list
| Current file | Replaces (old 404s) | Purpose |
|---|---|---|
| `index.html` | `index.html` | Hero + recent found/lost grids, features, steps |
| `report-found.html` | `found.html` | Upload/webcam photo, geolocation, category; POST |
| `report-lost.html` | `lost.html` | Same, for lost items |
| `matches.html` | `results.html` | Match results + claim; animated score bars |
| `item.html` | `item-details.html` | Item detail + mark-as-returned |
| `my-items.html` | `my-items.html` | Real lookup by email (`/api/get-items-by-email`) |
| `claimed.html` | `claim-success.html` | Success/confetti page after claim |

Old filenames intentionally return 404 so bookmarks don't hit stale pages.

### Client flows
- `sessionStorage['reportDetails']` carries matching results to `matches.html`.
- `localStorage['lf_email']` persists the user's email for `my-items.html`.
- All cross-links between pages updated to the new restful filenames.

---

## 4. Verification Performed

- Node/HTML audits confirmed no references to the old design leftovers
  (`badge-cat`, `badge-loss`, `btn-teal`, `hero-glow`, `fuchsia`, `indigo`,
  `violet`, …).
- Old page names 404; new names return 200.
- End-to-end report → match → my-items → claim → re-claim-reject →
  mark-returned-cascade verified.
- Re-claiming an already-claimed item correctly returns 400.
- Confirmed an identical image reported under **different categories** can be
  matched (category is only a soft bonus, never a hard filter).
- Confirmed the paths fix so images load regardless of launch directory.

---

## 5. How to Run

```bash
cd Lost_And_Found/lost_and_found_updated
pip install -r requirements.txt          # includes sentence-transformers, qdrant-client
uvicorn main:app --host 127.0.0.1 --port 8010
```

Open `http://127.0.0.1:8010/`. The first launch downloads/loads the CLIP model
(`clip-ViT-B-32`) and can take 60+ seconds to become ready.