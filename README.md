# Knit — knit things back together

**Knit** is an AI-powered lost-and-found web app. Someone reports a lost item, someone else reports a found item, and the app's matching engine figures out they're the *same* thing — then puts both parties in touch.

It uses **CLIP** (a multi-modal vision+text model) to embed every report's photo *and* written description into one shared 512-dimensional space. When a new report comes in, Knit searches the opposite collection (lost vs. found) by cosine similarity, fuses visual and textual signals into a single confidence score, and surfaces the strongest candidates.

---

## Table of contents

1. [How it works](#how-it-works)
2. [Feature tour](#feature-tour)
3. [Tech stack](#tech-stack)
4. [Project structure](#project-structure)
5. [Prerequisites](#prerequisites)
6. [Setup](#setup)
7. [Running the app](#running-the-app)
8. [The matching engine](#the-matching-engine)
9. [API reference](#api-reference)
10. [Security features](#security-features)
11. [Configuration reference](#configuration-reference)
12. [Deploying](#deploying)
13. [Troubleshooting](#troubleshooting)

---

## How it works

```
User reports an item (photo + description)
                    │
                    ▼
        FastAPI (main.py)
                    │
        ┌───────────┴───────────┐
        │ validate + sanitize   │  8 MB cap, HTML-stripping, coord/phone/name cleaning
        │ generate embeddings  │  CLIP → 512-dim image vector + 512-dim text vector
        └───────────┬───────────┘
                    ▼
        Qdrant (on-disk vector DB)
        found_items_collection  ·  lost_items_collection
        (each point stores image + text vectors + payload)
                    │
        opposite collection searched with the new item's vectors
                    │
                    ▼
        Fused score = 0.55·visual + 0.35·text + 0.08·category bonus
        (threshold 0.60)  →  ranked candidates, best up to 6
                    │
                    ▼
        User reviews matches → claims → both items marked "claimed"
        → email notification sent to BOTH parties (Jinja2 template)
```

### Item lifecycle

Every reported item starts as **`available`**, then moves through the lifecycle as it's matched:

```
available ──claim──▶ claimed ──return──▶ returned
```

| Status     | Meaning                                                      |
| ---------- | ------------------------------------------------------------ |
| `available`| Reported but not yet matched with anything.                  |
| `claimed`  | Matched with an item in the opposite collection; both are now off the market. |
| `returned` | The reunion happened — an owner marked the pair as returned. |

A claimed item is no longer returned in match searches, so the same lost phone won't be offered to five different finders.

### Why two collections?

Found reports live in `found_items_collection`, lost reports in `lost_items_collection`. A match is *always* across collections — a found item is matched against the lost pool and vice-versa. `item.html` never mixes the two, and the claim endpoint rejects claims within the same collection.

### Why multi-modal?

A phone reported as "lost black iPhone" might be reported as "found phone with cracked screen" — the text barely overlaps but the photos could be the same device. CLIP embeds images and text in the same latent space, so Knit can say "these two *pictures* look alike" (visual) *and* "these two *descriptions* agree" (text), and blend both. A bad photo can still match via the description, and vice-versa.

---

## Feature tour

| Page | What it does |
| ---- | ------------ |
| `static/index.html` | Home — shows recent found/lost items, quick report buttons. |
| `static/report-found.html` | Report an item you found: photo (file or webcam), description, category, last-known location (geolocation button). |
| `static/report-lost.html` | Same form for an item you lost. |
| `static/matches.html` | Lists the AI's ranked candidates for the report you just submitted, each with a combined confidence score plus the visual and description sub-scores, and a **Claim** button. |
| `static/item.html` | Detail page for any single item (`?collection=…&id=…`); "Find potential matches" re-runs the search for that item; owners can mark a claimed pair as **returned**. |
| `static/my-items.html` | Enter your email to see every item you've reported, with its current status. |
| `static/claimed.html` | Confirmation page after a successful claim. |

Other touches:

- **Dark/light theme** toggle (persisted in `localStorage`), Sora + Manrope fonts, no external icon libraries (inline SVG icon set in `static/js/app.js`).
- **Email notifications** — the whole point: contact info is exchanged by the *system*, not shown publicly.

---

## Tech stack

| Component | Technology |
| --------- | ---------- |
| Language | Python 3.10+ (Pipfile targets 3.12) |
| Backend | FastAPI + Uvicorn |
| AI model | `sentence-transformers` → `clip-ViT-B-32` |
| Vector database | Qdrant (local on-disk mode) |
| Image handling | Pillow (re-encode, pixel cap) |
| Templating | Jinja2 (autoescaped; used for claim emails) |
| Email | `smtplib` + SMTP (Gmail app-password compatible) |
| Frontend | Plain HTML/CSS/JS (no build step) |

---

## Project structure

```
lost_and_found_updated/
├── main.py                    # Everything: FastAPI app, embeddings, matching, email, security
├── requirements.txt           # pip dependencies
├── Pipfile                    # pipenv alternative
├── CHANGES.md                 # change log
├── static/                    # served at /
│   ├── index.html             # home
│   ├── report-found.html
│   ├── report-lost.html
│   ├── matches.html
│   ├── item.html
│   ├── my-items.html
│   ├── claimed.html
│   ├── css/styles.css         # Knit design system
│   ├── js/app.js              # brand mark, icons, theme, nav
│   ├── logo.png
│   └── webcon.ico
├── templates/
│   ├── claim_notification.html    # email sent on claim
│   └── match_notification.html
├── uploaded_images/           # runtime: re-encoded item photos ({item_id}.jpg)
└── qdrant_storage/            # runtime: on-disk vector database (persists!)
```

`uploaded_images/` and `qdrant_storage/` are runtime data — they are **not** committed to git and must be backed up if you care about your data (they persist across restarts by design).

---

## Prerequisites

- **Python 3.10+** (tested on 3.12)
- A machine with enough memory to hold the CLIP model (~500 MB loaded, ~1 GB+ RAM headroom)
- Internet on first run (to download the `clip-ViT-B-32` model weights into `~/.cache/huggingface/`)

---

## Setup

### 1. Get the code

```bash
git clone https://github.com/Arghya1901/Lost_And_Found.git
cd Lost_And_Found/lost_and_found_updated
```

### 2. Create a virtual environment

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

> The heavy one is `sentence-transformers`, which pulls in PyTorch. It can take a few minutes.
> Alternatively, with pipenv: `pipenv install` (uses the Pipfile, Python 3.12).

### 4. Configure email (optional but recommended)

Claims notify both parties by email, so matching only reaches its full purpose when SMTP is configured. Set these **environment variables** (never edit secrets into `main.py`):

| Variable | Purpose | Example |
| -------- | ------- | ------- |
| `KNIT_SMTP_EMAIL` | Sending account (sender) | `kindred.app@gmail.com` |
| `KNIT_SMTP_PASSWORD` | App password for that account | `xxxx xxxx xxxx xxxx` |
| `KNIT_SMTP_SERVER` | SMTP host | `smtp.gmail.com` |
| `KNIT_SMTP_PORT` | SMTP port | `587` |

**Windows (PowerShell):**

```powershell
$env:KNIT_SMTP_EMAIL = "you@gmail.com"
$env:KNIT_SMTP_PASSWORD = "your-app-password"
$env:KNIT_SMTP_SERVER = "smtp.gmail.com"
$env:KNIT_SMTP_PORT = "587"
```

**macOS / Linux:**

```bash
export KNIT_SMTP_EMAIL="you@gmail.com"
export KNIT_SMTP_PASSWORD="your-app-password"
export KNIT_SMTP_SERVER="smtp.gmail.com"
export KNIT_SMTP_PORT="587"
```

For Gmail, enable **2-Step Verification** and generate an **App Password** (Settings → Security → App passwords). If SMTP is not configured, the app runs fine — claims just log a warning instead of sending email.

---

## Running the app

```bash
uvicorn main:app --host 127.0.0.1 --port 8010
```

Notes:

- The first start loads the CLIP model — you'll see a progress bar for the model weights, then `Application startup complete.` and `Uvicorn running on http://127.0.0.1:8010`. Give it a moment.
- You don't need `--reload` for production. On Windows, you may prefer without `--reload` (reload can be unstable with `multiprocessing`/Windows).
- **Important:** the app intentionally does **not** wipe `qdrant_storage/` on startup. Data persists across restarts.

Then open:

- App: http://127.0.0.1:8010/
- Interactive API docs (Swagger): http://127.0.0.1:8010/docs
- OpenAPI schema: http://127.0.0.1:8010/openapi.json

---

## The matching engine

All tunables live at the top of `search_potential_matches` in `main.py`.

### Two signals, one shared space

For every report, the backend computes two CLIP embeddings:

| Embedding | Built from | Stored as |
| --------- | ---------- | --------- |
| `image` | the uploaded photo (resized to 512×512, LANCZOS) | named vector `image` |
| `text`   | `"{category} {item_name}"` | named vector `text` |

Both live in Qdrant as named vectors inside the same point, which is what allows fusion *at query time*.

### Candidate pre-filter (visual only)

When a new item arrives, Qdrant is asked for image-similar points in the **opposite** collection:

```
query_points(
  using="image",
  score_threshold=0.45,     # VIS_PREFILTER
  limit=30,                 # MATCH_LIMIT(6) × PREFETCH_FACTOR(5)
  filter={status == "available"},
)
```

The prefilter is deliberately loose — it casts a wide net so the re-rank step gets enough candidates to be fair.

### Re-rank (fusion)

Each pre-filtered candidate gets a **fused score**:

```
fused = 0.55 · visual_score      (image-image cosine similarity)
      + 0.35 · text_score        (text-text cosine similarity)
      + 0.08 · category_bonus    (+0.08 when categories agree exactly)
```

- Candidates below `MATCH_THRESHOLD` (0.60) are dropped.
- Results are sorted by fused score **descending**, ties broken newest-first, capped at `MATCH_LIMIT` (6).
- Each result carries `score` (fused), `visual_score`, and `text_score` so the UI can render the sub-score bars.

### Confidence labels (frontend)

`matches.html` turns the fused score into a label:

| Score | Label |
| ----- | ----- |
| ≥ 0.78 | **Strong** |
| ≥ 0.65 | **Good** |
| < 0.65 | **Possible** |

### Why category is only a bonus

The same item is frequently reported under different categories ("phone" vs "Electronics" vs "Other") by different people. Making category a hard filter would hide real matches, so it's a soft +0.08 nudge instead — visual + textual similarity carry the ranking.

---

## API reference

All responses have contact fields (`finder_contact_email`, `loser_contact_email`, `finder_phone`, `loser_phone`) **redacted** (`••••••••`). Contact is exchanged via the claim email, never exposed publicly.

### `POST /api/report-found` — report a found item

Multipart form data:

| Field | Notes |
| ----- | ----- |
| `item_name` | ≤ 120 chars, HTML stripped |
| `user_name` | ≤ 60 chars |
| `user_contact_email` | valid email |
| `user_phone` | digits/`+()- .` only |
| `latitude` / `longitude` | numbers or "Not Provided" |
| `category` | one of: `Electronics, Clothing, Pets, Keys, Documents, Bags & Luggage, Other` |
| `image` | file, ≤ 8 MB, JPG/PNG/WEBP |

Response: `{ reported_item_id, reported_item_collection, reported_item_payload, potential_matches[] }` — the matches are computed and returned **in the same request**.

### `POST /api/report-lost` — report a lost item

Identical contract, same fields; stores into the lost collection and searches the found collection.

### `POST /api/claim-item` — claim a match

JSON body:

```json
{
  "reporting_item_id": "<uuid>",
  "reporting_item_collection": "found_items_collection",
  "matched_item_id": "<uuid>",
  "matched_item_collection": "lost_items_collection"
}
```

Validates that both items exist, are `available`, and come from *different* collections. Marks **both** `claimed` (recording `matched_with_id` / `matched_with_name`), then emails both parties with the other's contact details. Returns `{ message, status: "success" }`.

### `POST /api/mark-as-returned/{collection_name}/{item_id}`

Marks a `claimed` item (and its matched partner) as `returned`. Returns `{ message, status: "success" }`.

### `POST /api/get-potential-matches`

JSON: `{ "item_id": "<uuid>", "collection_name": "found_items_collection" }`. Re-runs the matching search for an existing item (used by `item.html`).

### `POST /api/get-items-by-email`

JSON: `{ "email": "user@example.com" }`. Returns every item reported with that email (both collections), newest first. Powers `my-items.html`.

### `GET /api/get-found-items`

Latest up-to-12 found items (newest first). Powers the home page grid.

### `GET /api/get-lost-items`

Latest up-to-12 lost items (newest first). Powers the home page grid.

### `GET /api/item-details/{collection_name}/{item_id}`

Single item payload (contact redacted).

### Static

- `/images/{item_id}.jpg` — uploaded photos (re-encoded JPEGs)
- `/` and `/css/…`, `/js/…`, `/*.html` — the frontend (served from `static/`)

---

## Security features

The app is hardened against the usual web attacks:

- **Stored/reflected XSS** — every user field is sanitized server-side (`clean_text` strips HTML tags + control chars, caps length); `sanitize_name`, `sanitize_phone`, `sanitize_coord`, `validate_category`, `sanitize_email_header`. The frontend additionally HTML-escapes user strings (`esc()`) as defense-in-depth.
- **PII redaction** — contact info is masked on every public endpoint; exchanged only via email.
- **Rate limiting** — per-IP in-memory limiter: write endpoints (`report-*`, `claim-item`) allow 8 req / 90 s; lookup endpoints allow 30 / 300 s. Excess → `429`.
- **Upload hardening** — 8 MB cap (→ `413`), `Image.MAX_IMAGE_PIXELS = 4096²` defeats decompression bombs, and images are **re-encoded** to a fresh `{item_id}.jpg` (strips HTML payloads, EXIF, and attacker-controlled extensions).
- **Security headers** — CSP (`default-src 'self'`), `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Permissions-Policy`, and `X-XSS-Protection: 0`.
- **SMTP header injection** — CR/LF stripped from the email subject.
- **Jinja2 autoescape** — enabled for all templates.
- **Secrets** — SMTP credentials come from environment variables, never hardcoded.

---

## Configuration reference

All constants are at the top of `main.py`:

| Constant | Default | Meaning |
| -------- | ------- | ------- |
| `MAX_UPLOAD_BYTES` | 8 MB | upload cap |
| `MAX_TEXT_LEN` / `MAX_NAME_LEN` / `MAX_PHONE_LEN` | 120 / 60 / 30 | input caps |
| `ALLOWED_CATEGORIES` | 7 categories | validation whitelist |
| `MATCH_THRESHOLD` | 0.60 | minimum fused score |
| `MATCH_LIMIT` | 6 | max matches returned |
| `VIS_PREFILTER` | 0.45 | visual candidate cutoff |
| `W_VISUAL` / `W_TEXT` / `CATEGORY_BONUS` | 0.55 / 0.35 / 0.08 | fusion weights |
| `PREFETCH_FACTOR` | 5 | candidate over-fetch |
| `_RATE_WINDOW` | write (8,90) · lookup (30,300) | rate limits |
| `Image.MAX_IMAGE_PIXELS` | 4096² | decompression-bomb cap |
| `EMAIL_CONFIG` | from env vars | SMTP settings |

---

## Deploying

The app is a single long-running process; the local Qdrant stores data on a **persistent disk**. Choose a host that gives you persistent storage and enough RAM for PyTorch/CLIP.

**Recommended: Oracle Cloud Free Tier (ARM, Ampere A1)**
- 4 OCPUs / 24 GB RAM free forever, block volume that persists across restarts — fits the model + a growing DB comfortably.
- Run under a process manager (systemd) and expose port 80/443.

**Avoid serverless / ephemeral-disk hosts** for production: Render, Railway, Hugging Face Spaces, Cloudflare Workers/Pages, and Vercel either evict the disk, lack enough RAM for CLIP, or can't hold a long-running model in memory. **Fly.io's free tier** also uses ephemeral storage — your `qdrant_storage/` and `uploaded_images/` would vanish on restart (OK for a demo only).

**Production checklist:**
1. Set `KNIT_SMTP_*` env vars.
2. Run `uvicorn main:app --host 0.0.0.0 --port 8000` behind a reverse proxy with TLS.
3. Back up `qdrant_storage/` and `uploaded_images/` regularly.
4. Optionally raise rate limits per your traffic.

---

## Troubleshooting

| Symptom | Fix |
| ------- | --- |
| `Uvicorn running` line takes a long time | CLIP is downloading/loading — watch the `Loading weights: 100%` progress bar; first boot is slowest. |
| `ModuleNotFoundError: sentence_transformers` / `qdrant_client` | `pip install -r requirements.txt`; ensure your venv is active. |
| Emails never send | Verify `KNIT_SMTP_EMAIL` / `KNIT_SMTP_PASSWORD`; for Gmail use an app password, not the account password. App works without SMTP, it just logs a warning. |
| `429 Too many requests` | You hit the rate limit (8 writes / 90 s). Slow down; wait a moment. |
| `413 Image too large` | Image exceeds 8 MB — compress/resize before uploading. |
| Port 8000/8010 already in use | Change the port: `uvicorn main:app --port 8011`. |
| Photos 404 after restart | `uploaded_images/` was deleted or not backed up (ephemeral host). Data lives on disk. |
| Two identical items don't match | Check both are `available` (one may be `claimed`), categories/descriptions are reasonable, and the fused score exceeded 0.60. Matching is probabilistic — not every pair reaches the threshold. |
| Windows: `--reload` crash | Run without `--reload`; reload is unstable on Windows. |

---

## Changelog

See [`lost_and_found_updated/CHANGES.md`](lost_and_found_updated/CHANGES.md) for the full history — brand redesign, AI matching engine v8.2.0, and security hardening v8.3.0.
