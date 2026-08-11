# Spec — Media Extraction Pipeline

**Status:** draft for implementation
**Scope:** extract figures from PPTX and PDF course material, convert vector artwork to SVG, write
assets to S3, derive metadata, and make them searchable.
**Out of scope:** the MCP tool surface (`search_media`, `get_media`, `preview_media`,
`attach_media`) and the course-portal media browser. Both consume this pipeline and are specified
separately. This spec must produce a manifest they can read.

---

## 0. Architecture

The work splits across two existing services along one line: **state lives in TypeScript, computation
lives in Python.**

```
┌──────────────────────────────────────────────────────────────┐
│  hslu-aire-server                    TypeScript, App Runner   │
│  the portal — exists, 1 vCPU / 2 GB                           │
│                                                               │
│  owns    MongoDB · S3 keys · auth · @anthropic-ai/sdk ·       │
│          the embeddings client                                │
│  does    job orchestration, metadata, descriptors, embeddings,│
│          review UI, search                                    │
└────────────────────────┬─────────────────────────────────────┘
                         │ HTTP, server-to-server, bearer token
                         │ (the pattern /training/convert already uses)
                         ▼
┌──────────────────────────────────────────────────────────────┐
│  hslu-aire-media                     Python, App Runner   NEW │
│  stateless — request in, data out                             │
│                                                               │
│  can      parse PPTX/PDF, drive LibreOffice, sanitise SVG,    │
│           produce raster derivatives, write blobs to S3       │
│  has no   database, no auth, no LLM, no memory between calls  │
└──────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────┐
│  hslu-aire-doc-service               Python, App Runner       │
│  unchanged — /convert for learners                            │
└──────────────────────────────────────────────────────────────┘
```

**The rule: only TypeScript writes to MongoDB.** The media service receives a document, computes,
writes image blobs to S3, and returns JSON. It remembers nothing between calls.

This is the boundary the system already has — the portal calls the doc service, the doc service has
no database. Extending that boundary rather than crossing it is what keeps the media service a
plain request/response App Runner service: no queue worker, no lease, no background loop on a
platform that is not built for one.

### 0.1 Why not put everything in the portal

Two reasons, one of them hard.

**TypeScript does not have the libraries.** `python-pptx` is effectively the only mature way to walk
a PPTX shape tree with EMU geometry; for PDF content streams PyMuPDF has no Node equivalent. The
alternative is hand-rolling OOXML parsing.

**The portal runs on 2 GB with `MaxConcurrency: 100`.** A single 300 dpi page bitmap is ~144 MB
(§3.3) and LibreOffice adds 300–500 MB. Rendering does not belong in the process that serves
learners.

### 0.2 Why a second Python service rather than reusing the doc service

`hslu-aire-doc-service` runs at `MaxConcurrency: 2` because each request holds a document in memory
(`delta-spec-doc-convert-cells.md` §12.4). A multi-minute render occupying one of two slots starves
the learner-facing conversion path. A separate service gets its own pool, its own concurrency and
its own instance size.

**Same repository, same language, second image.** `Dockerfile.media` carries LibreOffice and no
Torch; `Dockerfile.amd64` keeps Docling and no LibreOffice. One CodeBuild project builds both from
one source zip.

### 0.3 Decisions

| | Decision | Status |
|---|---|---|
| **D1** | State in the portal, computation in the media service; only TypeScript touches MongoDB | ✅ decided (§0) |
| **D2** | The media service is separate from `hslu-aire-doc-service` | ✅ decided (§0.2) |
| **D3** | Two images from one repository — media without Torch, doc-service without LibreOffice | ✅ decided (§0.2) |
| **D4** | Metadata and job state live in the portal's existing MongoDB Atlas cluster | ✅ decided (§7.3) |
| **D5** | **How assets are served to browsers** — presigned URLs, portal proxy, or CloudFront | ⬜ open (§10.1) |

Open items that are not architecture: §14.

---

## 1. Design constraints

**Fully automatic.** A run over a document set completes without human input. Every asset is
written, tagged and made addressable. No step blocks on review.

**Review is deferred, not skipped.** Every asset carries a `review` block with machine-assigned
confidence values. Review happens as a batch pass after the run, against the queue in §9. Nothing
in the pipeline waits for it.

**Binary never transits the authoring context.** The pipeline writes to S3 and returns URLs and
metadata. Consumers reference by `assetId`. Only `preview_media` (separate spec) ever returns image
bytes, and only on explicit request.

**Immutability.** Blobs are content-addressed and never overwritten. Corrections create new assets.
Retirement is a flag, never a delete — course module revisions are append-only and an old revision
must not dangle.

---

## 2. Job model

The job lives in the portal. The media service has no notion of a job.

```
POST /training/media/ingest        (portal, authenticated)
  multipart file | { documentKey }, courseId?, moduleHint?, options
  → 202 { jobId }

GET  /training/media/ingest/{jobId}
  → { state, progress, counts, errors[], assetIds[] }
```

`state ∈ { queued, preparing, enumerating, rendering, deriving, indexing, done,
completed_with_errors, failed }`.

`completed_with_errors` exists because §1 requires every extractable asset to be written: a run
where 3 of 118 candidates failed is neither `done` nor `failed`. `errors[]` carries the per-candidate
locator.

Jobs are idempotent on the source document's SHA-256, enforced by a unique index (§7.3).
Re-submitting an unchanged document returns the prior `jobId` and does not re-process. Re-submitting
a changed document creates a new job; blobs whose content hash is unchanged are reused rather than
rewritten.

### 2.1 The orchestration loop

The portal drives the phases; each call to the media service is a normal HTTP request.

| Phase | Call | Shape |
|---|---|---|
| `preparing` | `POST /v1/media/prepare` | once per document — deck → PDF + per-slide PNGs into S3 |
| `enumerating` | `POST /v1/media/candidates` | per slide range — returns candidates with geometry |
| `rendering` | `POST /v1/media/render` | per slide — returns `sha256`, dimensions, technical flags |
| `deriving` | portal-internal | descriptors + embeddings (§7.2) |
| `indexing` | portal-internal | write assets, run the duplicate check (§8.3) |

**The source is named, not carried.** Every call to the media service takes a `sourceKey` — the S3
key the portal wrote at ingest — rather than the document as a multipart body. Forwarding the bytes
is tolerable at 16 MB and absurd at 285: one deck × one `prepare` + one `candidates` + one `render`
per slide is the whole file crossing the wire some 120 times, and it forces the portal to hold a
copy in memory for the length of the job. With a key, the portal never reads the source after
storing it, and the media service downloads it once per instance and keeps it on local disk for the
remaining calls of that job (LRU, two entries, keyed on S3 key + ETag). A multipart `file` field
still works and is what local runs and `curl` use.

**The browser uploads straight to S3.** The portal never sees a source document at all:

```
POST /admin/media/upload-url   { filename, contentType, size }  → { key, url, expiresIn }
PUT  <url>                     the file, from the browser, direct to S3
POST /admin/media/ingest       { documentKey, filename }        → 202 { jobId }
```

The signature commits to the exact key and content type, expires in 15 minutes, and is issued only
to an authenticated administrator. This requires bucket CORS allowing `PUT` from the portal's origin
— without it the browser refuses the request before making it. The CORS rule is set on the bucket
directly and is **not** in Terraform; if the bucket is ever recreated it must be restored.

`ingest` then HEADs the object to confirm the upload landed, and asks the media service for the
SHA-256 (`POST /v1/media/hash`) because §2's idempotency key is the source hash and this side no
longer has the bytes. Hashing on the extraction side is the stronger claim anyway — it is the hash
of what was actually read, not of what a client said it sent — and it warms the source cache that
`prepare` reads seconds later.

A raw-body `POST /admin/media/ingest` still works for curl and local runs. It is now the only path
that puts a source document in portal memory, and is capped well below the S3 path's limit for that
reason.

**`prepare` is the only phase that risks an HTTP timeout.** A 120-slide deck converts in roughly
60–180 s. App Runner's request timeout is configurable; measure a real deck against the configured
value before assuming it fits. If it does not, only this phase needs an async handle — the rest of
the pipeline stays request/response either way. Do not pre-emptively build a job queue for a
timeout that may not exist.

**Where the portal runs the loop.** The portal is `MinSize 1 / MaxSize 25 / MaxConcurrency 100`. A
job loop in that process survives normal operation but not a deployment or a scale-in. Since the
portal owns the job row, the recovery is a row-level lease, not a queue:

```
claim   { state: "queued" }        → { state: "preparing", leasedUntil: now + 15min }
renew   extend leasedUntil every 60 s while working
reclaim state ∉ {done, completed_with_errors, failed} and leasedUntil < now
        → back to "queued", attempts += 1; at attempts > 2 → failed
```

`findOneAndUpdate` makes the claim atomic, so more than one portal instance racing for the same job
is safe.

---

## 3. Source classification (media service)

For each document, enumerate **figure candidates**. A candidate is a region a reader would perceive
as one illustration. Five classes, resolved in this order:

| Class | Detection | Output |
|---|---|---|
| `svg_native` | `a:blip` carries an `svgBlip` in its `a:extLst` | SVG, verbatim |
| `vector_import` | media part is EMF / WMF | SVG, converted |
| `chart` | `p:graphicFrame` referencing `ppt/charts/chartN.xml` | SVG, redrawn from chart data |
| `shape_group` | native DrawingML shapes, grouped or spatially clustered | SVG, see §4 |
| `raster` | embedded PNG / JPEG / TIFF | raster, derivatives only |

`svg_native` takes precedence over the PNG fallback PowerPoint stores alongside it. Emit the SVG and
discard the fallback; do not register both.

### 3.1 PPTX enumeration

Do not walk `ppt/media/` directly. Walk `ppt/slides/slideN.xml` and resolve images through
`ppt/slides/_rels/slideN.xml.rels`. This gives slide attribution for free and makes the layout
filter possible.

**Reject** anything whose relationship originates in `slideLayout` or `slideMaster` — logos, footer
marks, decorative furniture.

**Reject** by frequency: any content hash appearing on more than 10% of slides, floor of 3 slides.
Configurable; catches recurring decoration that survived the layout filter.

**Honour `srcRect`.** The part in `ppt/media/` is the uncropped original; the slide shows a crop.
Apply the `a:srcRect` insets from `a:blipFill` when producing derivatives, and record both the
original and the cropped extent. Registering the uncrop is a known way to produce assets nobody
recognises later.

**Alt text.** Read `p:cNvPr/@descr` and `@title` on the picture or graphic frame. Where present this
is author-supplied and outranks any generated description.

**Refinements:**

- **The frequency filter has a false positive worth naming.** A diagram deliberately reused as a
  running reference — the course's own conceptual model, the thing most worth extracting — appears
  on many slides and is rejected. Exempt any candidate carrying author alt text, and return every
  frequency rejection with its hash and slide list so §9 can review them.
- **Class before frequency.** Resolve `svg_native` first, or an SVG and its PNG fallback count as
  two distinct hashes and neither reaches the threshold.
- **Content-address the derivative, not the part.** The same picture part appears on many slides
  with different `srcRect`; hashing the part would collapse them.

### 3.2 Shape-group candidate detection

> **Superseded by measurement.** The clustering below was implemented and then
> replaced: on a real 128-slide module it cut single diagrams into their parts,
> because a diagram's boxes are routinely further apart than the 0.25 in gap
> threshold. Worst case was one illustration emitted as four assets.
>
> **The rule now: if a slide contains any drawing primitive, emit exactly one
> candidate** covering everything on it except template furniture (title, footer,
> slide number, date) and the top/bottom 8% bands. A reader perceives one
> illustration; one asset is what should come out.
>
> Measured on that module: 24 candidates with a worst slide of 4, against 28
> candidates with a worst slide of 1 — the whole-slide rule also finds *more*,
> because the minimum-shape and minimum-area filters below were discarding whole
> slides. `WHOLE_SLIDE_WHEN_PRIMITIVES` in `app_media/pptx_scan.py` switches back
> for comparison; the clustering code is kept for that reason.
>
> §11.1's hand-labelled set was never built, and this is what it would have caught
> before the thresholds reached production.

The original clustering, retained behind the flag:

Cluster shapes on a slide by bounding-box adjacency:

1. Take all non-placeholder shapes, plus explicit `p:grpSp` groups as pre-formed clusters.
2. Merge shapes whose bounding boxes are within `gap ≤ 0.25 in` of an existing cluster.
3. Discard clusters with fewer than 3 shapes, or area below 8% of the slide.
4. Discard clusters that are pure text (no shape has a non-`noFill` fill and no connector is
   present) — a bullet layout, not a diagram.
5. Where a cluster's bounding box exceeds 85% of the slide's content area, treat the whole content
   area as the candidate — **and only if step 4 still passes**, or a full-bleed background photo
   plus a title becomes a whole-slide candidate.

Emit one candidate per surviving cluster with its bounding box in EMU.

**Unit conversion.** `pt = EMU / 12700`, `in = EMU / 914400`. For the house canvas (26.67 × 15 in)
the slide is 1920 × 1080 pt, which maps 1:1 onto the PDF user space — no scale factor when cropping
a region. *(The arithmetic checks out: 914400/72 = 12700, 26.67 × 72 = 1920.)*

**Refinements:**

- **Merging must be deterministic.** Step 2 merges "into an existing cluster", so the result depends
  on visit order. Sort by `p:cNvPr/@id` ascending and iterate to a fixed point — otherwise two runs
  over one deck produce different candidates and §2's idempotency is false.
- **Rotation is ignored.** `a:xfrm/@rot` and `flipH`/`flipV` mean the axis-aligned box can far
  exceed the shape. Use the rotated extent, or one rotated arrow drags in half a slide.
- **`p:grpSp` nests.** Treat only the outermost group as a pre-formed cluster.

### 3.3 PDF enumeration

XObject extraction is not sufficient: many producers emit one visual image as dozens of horizontal
strips, alpha arrives as separate SMask objects, and vector artwork is not an image at all.

Use a content-stream approach (PyMuPDF or equivalent):

1. Collect drawing operations and image placements per page with their rectangles.
2. Cluster by adjacency as in §3.2.
3. Classify a cluster as `raster` if image placements cover ≥ 90% of its area, else `shape_group`.
4. Render the page at 300 dpi and crop the cluster rectangle for the raster derivative; for
   `shape_group`, take the vector path in §4.

Reject clusters in the header and footer bands (top and bottom 8%) unless they exceed 20% of page
width and are not repeated across pages.

**The 300 dpi render is the memory driver.** A 1920 × 1080 pt page at 300 dpi is 8000 × 4500 px;
as RGBA that is ~144 MB for one page, before cropping. Render **one page at a time and release**,
never a whole document, never two pages concurrently in one process. Same class of mistake as
`read_only=False` in the cells spec: correct-looking code that OOMs at scale.

**PyMuPDF is AGPL-3.0** unless commercially licensed. For an internally-run service that is usually
acceptable, but it is a licence decision, not a technical one. `pypdfium2` (BSD/Apache) is the
alternative, at the cost of a weaker drawing-op API. §14.

---

## 4. Vector conversion (media service)

A ladder, most faithful first. Record which rung produced the result in `provenance.method`.

### 4.1 Pass-through (`svg_native`)

Extract, sanitise (§5), done. `vectorConfidence = 1.0`.

### 4.2 Format conversion (`vector_import`, `chart`)

EMF/WMF via a vector converter (`libemf2svg`, or LibreOffice headless as fallback). Charts are
**redrawn from `chartN.xml`** — series, categories, axis titles and number formats are all present,
and generating SVG from the data produces cleaner output than rendering. Where a chart type is
unsupported, fall back to §4.3 and mark it. `vectorConfidence = 0.9`.

**Chart redraw is its own work item.** §12 scopes it to bar, column, line, pie and doughnut. That is
still axis scales, tick and label placement, `c:numFmt` (Excel format codes, not `printf`), legend
layout, stacked and percent-stacked variants, and theme colour resolution. Make §4.3 the default
until the redraw is proven — a subtly wrong redraw is worse than a faithful render, because
`vectorConfidence = 0.9` tells the review pass not to look.

### 4.3 Render and crop (`shape_group`, default path)

1. Convert the deck to PDF via LibreOffice headless — **once per document**, in `prepare`, cached in
   S3 for the job. Reading this as once per candidate is the difference between ten minutes and
   several hours.
2. Convert the target page to SVG.
3. Set `viewBox` to the candidate bounding box in points; do not rescale geometry.
4. Strip elements fully outside the box; clip elements that straddle it.

**Use LibreOffice's SVG export, not `pdftocairo -svg`.** `pdftocairo` converts text to paths — not
searchable, not correctable, worthless to a screen reader. LibreOffice preserves `<text>`, at the
cost of embedding navigation JavaScript, which §5 removes anyway.

**Invoke LibreOffice with a per-call profile directory.** Two concurrent `soffice` invocations
sharing the default `~/.config/libreoffice` deadlock or silently reuse each other's state:

```
soffice --headless --norestore --invisible --nolockcheck --nodefault \
        -env:UserInstallation=file:///tmp/lo-$REQUESTID \
        --convert-to pdf --outdir /tmp/out deck.pptx
```

Kill on a hard timeout — LibreOffice hangs rather than exits on malformed input often enough to
need it.

**Fonts must be installed on the conversion host.** Without Verdana the metrics shift and labels
overflow. Enumerate fonts referenced in the deck — `ppt/theme/themeN.xml` (`a:latin/@typeface` for
major and minor) **and** per-run overrides in `a:rPr/a:latin/@typeface` across slides; a theme-only
check misses the one hand-set label that overflows.

**Font failure is per-candidate, not per-job.** Failing the whole job on one exotic font yields zero
assets from a 120-slide deck, which contradicts §1. Fail only when a missing font is referenced by
the candidate being rendered; otherwise set `review.reasons += "font_substituted"` and continue.

`vectorConfidence = 0.7`, reduced to `0.4` when the candidate contains a SmartArt graphic frame —
detected on `graphicData/@uri = "…/diagram"`, whose cached fallback lives in `dgm:drawing`.
LibreOffice renders that fallback, which is usually acceptable and occasionally wrong.

### 4.4 Raster fallback

If §4.3 fails or produces an SVG with fewer than 5 **drawing** elements
(`path|rect|circle|ellipse|line|polyline|polygon|text|image`, counted after sanitisation — not all
elements, since LibreOffice wraps output in enough `<g>`/`<defs>` scaffolding to clear a naive
count), emit the cropped raster with `vectorConfidence = 0.0` and
`review.reasons += "vector_conversion_failed"`. The asset is still written and still usable.

---

## 5. SVG sanitisation (media service)

**SVG is executable code.** Rendered inline in the portal, an unsanitised SVG is stored XSS in a
context where admins write and course participants read.

Sanitise on write, with an **allowlist**, never a blocklist:

- **Elements:** `svg, g, defs, title, desc, path, rect, circle, ellipse, line, polyline, polygon,
  text, tspan, textPath, image, use, symbol, marker, linearGradient, radialGradient, stop, clipPath,
  mask, pattern, style`
- **Remove unconditionally:** `script`, `foreignObject`, `animate*`, `set`, `handler`, and every
  `on*` attribute.
- **`href` / `xlink:href`:** permit only same-document fragments (`#id`). Reject `http(s):`,
  `javascript:`, and `data:` other than `data:image/png` and `data:image/jpeg`.
- **`<style>` and `style=`:** permit, but strip `@import`, off-document `url()`, and `behavior` —
  including inside `<style>` blocks, not only in `style=` attributes.
- Normalise IDs with a per-asset prefix so multiple inline SVGs on one page cannot collide.

Reject any SVG exceeding **2 MB** after sanitisation, and any raster exceeding **20 MB** decoded —
checked from header dimensions *before* decoding, or the bound is discovered by running the
decompression bomb.

Sniff MIME from magic bytes; do not trust the extension or the part name.

### 5.1 The sanitiser is load-bearing, not defence in depth

A common formulation is "sanitise *and* serve from an isolated origin with a strict CSP; neither
alone is sufficient." The instinct is right, the mechanism is not. Response headers govern the asset
**as a document** — direct navigation, `<iframe>`, `<object>`. They have no effect when the SVG is
**inlined into the portal's DOM**, where the markup is part of the portal document and governed by
the *portal's* CSP.

| Rendering mode | What actually protects it |
|---|---|
| Inline `<svg>` in the portal DOM | **Sanitisation only** |
| `<img src="….svg">` | The browser's own restriction — no script, no external fetches |
| `<iframe>` / direct navigation | The asset origin's CSP, plus sanitisation |

Two consequences:

1. **Use a parsed-tree sanitiser, never a regex.** `bleach` with an SVG allowlist, or a DOM-based
   sanitiser. Do not hand-roll it.
2. **Prefer `<img>` wherever the SVG does not need to inherit page CSS.** It is a real security
   boundary at no cost, and it is what the media browser wants for thumbnails anyway. Reserve inline
   for the cases that genuinely need it — which may be none. §14.

### 5.2 Decisions inside the allowlist

- **`<a>` is absent**, so LibreOffice-exported hyperlinks vanish silently. Defensible — state it.
- **Filter primitives are absent** (`filter`, `feGaussianBlur`, …). LibreOffice emits them for
  shadows and blurs; without them those effects disappear. Decide and document.
- **`use` + `href="#id"` is an amplification vector.** A `<use>` chain into a `<symbol>` containing
  `<use>` expands exponentially. Cap nesting depth at 3 and total element count after expansion.

---

## 6. Storage layout

**Blobs are content-addressed and immutable. Metadata is `assetId`-addressed and lives in MongoDB.**

```
s3://<bucket>/media/blobs/<sha256[0:2]>/<sha256>/original.<ext>
                                                /web.png       1600 px long edge
                                                /thumb.png      384 px long edge

s3://<bucket>/media/slides/<sourceSha256>/<n>.png              whole slide, for review (§9)
s3://<bucket>/media/work/<jobId>/deck.pdf                      prepare output, deleted on job end
```

Keeping metadata out of the blob path is not a stylistic choice — it is required:

- **Deduplication collides with provenance.** The same figure in two decks yields one `sha256`. A
  `meta.json` under that hash can hold one `provenance.sourceDoc`; the second ingest would either
  overwrite the first — violating §1 — or be silently dropped. Two assets, one blob, two
  provenances is the truth, and only an index can express it.
- **`review`, `rights` and `usage` are mutable by design.** §9 writes them. Writing mutable fields
  into an object served with `Cache-Control: immutable` guarantees a stale read.

Rules:

- `original` is the sanitised SVG or the source-resolution raster.
- `web` and `thumb` are always PNG (rendered at 2× the target and downsampled), so consumers never
  rasterise at request time. `preview_media` serves `thumb`.
- **The `sha256` is of what you stored**: the *sanitised* bytes for SVG, the *post-crop,
  post-EXIF-strip* bytes for raster. Hashing the source and then transforming identifies nothing.
- Strip EXIF from all rasters. Preserve colour profile; convert CMYK to sRGB **with an ICC profile**
  — the naive formula shifts colour visibly.
- `Cache-Control: public, max-age=31536000, immutable` on blobs only.
- Deletion is never performed by the pipeline. Retirement sets a flag in the index.

Rendering SVG → PNG needs a renderer in the image (`cairosvg` or `resvg`) — a third rendering
dependency alongside LibreOffice, folded into `Dockerfile.media`.

---

## 7. Metadata

### 7.1 What the media service returns

Per candidate, the media service computes and returns only what it can know without a database:

```jsonc
{
  "sha256": "…",
  "class": "shape_group",
  "mediaType": "image/svg+xml",
  "bytes": 41822,
  "dimensions": { "w": 1180, "h": 640, "unit": "pt" },
  "blobKeys": { "original": "media/blobs/ab/abc…/original.svg",
                "web": "…/web.png", "thumb": "…/thumb.png" },
  "locator": { "slide": 47, "shapeIds": ["12","13","14"] },
  "boundingBoxEmu": { "l": 685800, "t": 1143000, "w": 14986000, "h": 8128000 },
  "method": "render_crop_libreoffice",
  "authorAltText": "…",              // from p:cNvPr/@descr, null if absent
  "context": {
    "slideTitle": "Die vier Werkzeuge",
    "surroundingText": "…",          // placeholder text, truncated 2000 chars
    "speakerNotes": "…",             // truncated 2000 chars
    "layoutName": "Titel und Inhalt",
    "shapeName": "Gruppieren 21"
  },
  "technicalFlags": {
    "vectorConfidence": 0.7,
    "resolutionAdequacy": 1.0,       // §8.2
    "drawingElementCount": 41,
    "sanitisationRemoved": ["script", "onload"],
    "fontSubstituted": false,
    "containedSmartArt": false
  }
}
```

`context` is extracted, not inferred, and is the more valuable half — it records **what a figure was
used for**, not merely what it depicts.

### 7.2 What the portal adds

Descriptors are generated in the portal, which already has both clients.

- **`altText`, `description`, `tags`** — a vision model over the `thumb` plus the `context` block. A
  model given the slide title and speaker notes produces markedly better tags than one given pixels
  alone. `altTextSource` records `"author"` or `"generated"`; author alt text always wins.
- **Model.** `claude-opus-5` ($5 / $25 per MTok) for quality, `claude-haiku-4-5` ($1 / $5) for bulk.
  A 384 px thumbnail is ~150 image tokens; at ~800 input + 200 output per asset, 100 assets costs
  roughly **$0.18** on Haiku or **$0.90** on Opus. **Choose on output quality — the cost is a
  rounding error** next to the render path. Note the portal's configured model list is a generation
  behind (`claude-opus-4-6`, `claude-sonnet-4-6`); align it or don't, but knowingly.
- **Do not use the Batch API here.** It halves cost, but "most batches complete within 1 hour,
  maximum 24" is incompatible with §11.10's ten-minute criterion. Use the Messages API with bounded
  concurrency (8 in flight); 100 assets finish in well under a minute. Keep Batch for a *backfill*
  over the existing archive, where 24 hours is fine and 50% is worth having.
- **`altText` is required, and generation can fail.** On failure, fall back to
  `context.slideTitle` or the shape name and set `review.reasons += "alt_text_fallback"` — an asset
  must never be blocked from the index by a model call.
- **Descriptor output is untrusted.** `speakerNotes` and `surroundingText` are author-controlled
  text fed to a model whose output is shown to learners. Strip markup, cap length, never let it
  carry a URL.
- **`embedding`** — on `description + context.slideTitle + context.surroundingText`, not on tags.
  Semantic proximity beats keyword matching, which is the same argument the course makes about
  knowledge bases. **Reuse `src/embeddings.ts`** (`text-embedding-3-small`); a second embedding
  provider would put two incompatible vector spaces in one system, and the comparison block and
  media search could not share an index.

### 7.3 MongoDB collections (D4)

The portal's existing Atlas cluster, database `aire_wizard`.

```jsonc
// media_jobs
{ _id: "job_…", sourceSha256: "…", sourceDoc: "Modul_1.pptx", courseId: "…",
  state: "rendering", progress: { done: 47, total: 118 }, counts: {…},
  errors: [ { locator: {…}, message: "…" } ],
  leasedUntil: ISODate, attempts: 0, createdAt: ISODate }

// media_assets
{ _id: "ast_…", sha256: "…", class: "shape_group", mediaType: "image/svg+xml",
  bytes: 41822, dimensions: {…}, blobKeys: {…},
  provenance: { sourceDoc, sourceSha256, sourceType, locator, boundingBoxEmu,
                method, pipelineVersion, extractedAt },
  context: {…}, descriptors: { altText, altTextSource, description, tags, ocrText, embedding },
  rights:  { license: "hslu-own", source: null, confirmedBy: null, confirmedAt: null },
  review:  { state: "pending", vectorConfidence, resolutionAdequacy, reasons: [],
             reviewedBy: null, reviewedAt: null },
  usage:   { courseIds: [], tags: [] },
  retired: false }
```

Indexes:

| Collection | Index | For |
|---|---|---|
| `media_jobs` | `{ sourceSha256: 1 }` **unique** | §2 idempotency — duplicate key returns the prior job |
| `media_jobs` | `{ state: 1, createdAt: 1 }` | the atomic claim in §2.1 |
| `media_assets` | `{ sha256: 1 }` | blob reuse |
| `media_assets` | `{ "provenance.sourceSha256": 1 }` | re-ingest, review by document |
| `media_assets` | `{ retired: 1, "review.state": 1 }` | §10 default filter |
| `media_assets` | text index on `descriptors.tags`, `descriptors.altText`, `context.slideTitle` | §10 lexical half |

**Give the media pipeline its own Atlas database user.** The portal's connection string has full
access to users, progress and course content. The pipeline needs `readWrite` on two collections; an
Atlas custom role costs nothing and removes an unnecessary blast radius.

**No vector database.** At a corpus of thousands — not millions — brute-force cosine over an
in-memory matrix is entirely adequate for §10, and 1536-dimension embeddings at ~12 KB per document
are far inside the 16 MB BSON limit. Reach for Atlas Vector Search only if the corpus grows an order
of magnitude.

---

## 8. Rights and quality gates

### 8.1 Rights

Default `rights.license = "hslu-own"` for material extracted from first-party decks, with
`review.state = "pending"` until confirmed. Permitted values: `hslu-own`, `licensed-stock`,
`third-party-permitted`, `unreviewed`.

The gate is on **publication**, not ingest: `attach_media` must refuse to attach an asset to a
module belonging to a published, active course **unless `review.state === "approved"`**.

*(Gating on `rights.license === "unreviewed"` would never fire: ingest defaults to `hslu-own` and
nothing ever sets `unreviewed`, so every asset would be attachable immediately, labelled first-party
on no evidence. Gating on review state keeps the optimistic default while making the gate mean
something.)*

### 8.2 Resolution adequacy

Pasted screenshots arrive at clipboard resolution. An 800 px screenshot placed at half width on a
1920 pt slide looks fine in PowerPoint and is mush on a high-DPI portal.

```
resolutionAdequacy = pixelWidth / (displayWidthPt × (96/72) × targetDensity)
```

with `targetDensity = 2`. Flag below `0.75` with `review.reasons += "low_resolution"`.

*(The `96/72` is not optional: `pixelWidth` is device pixels and `displayWidthPt` is PostScript
points. Omitting it understates the requirement by 33% and passes images that are ~1.5× dense.)*

Upscaling does not help; the remedy is re-capture, which is a review decision.

### 8.3 Automatic review flags

The media service returns the first six; the portal appends the seventh.

- `vectorConfidence < 0.5`
- `resolutionAdequacy < 0.75`
- the candidate contained SmartArt
- fewer than 5 drawing elements
- sanitisation removed a `script` element or an `on*` attribute *(worth knowing about the source)*
- a font was substituted
- **(portal)** two candidates in the same document have cosine similarity > 0.95 — a probable
  duplicate that survived hash dedupe. This needs the embeddings, so it runs in `indexing`, not
  `deriving`. O(n²) over one document's assets: a few thousand pairs for 120 slides.

---

## 9. Review queue (portal)

Batch, post-run, not inline. It lives in the portal because that is where login and roles already
are; building a second auth surface for it would be pure duplication.

```
GET  /admin/media/review?state=pending&reason=&sourceDoc=&limit=
     → assets ordered by (reason severity desc, sourceDoc, slide)
POST /admin/media/review/{assetId}   { state, license?, source?, altText?, tags?, note }
POST /admin/media/review/bulk        { assetIds[], state, license? }
```

The UI needs three things and nothing else: the thumbnail, the source slide rendered whole with the
candidate outlined, and the flag reasons. Reviewing a 120-slide deck should be a twenty-minute pass,
which it is only if approval is one keystroke, the default is approve, and bulk approve exists — a
per-asset POST over 118 assets is not a twenty-minute pass.

**The whole-slide render is a real output**, not a by-product: `media/slides/<sourceSha256>/<n>.png`
(§6), produced during `prepare`. Without it the UI cannot show context, and re-rendering at review
time would put LibreOffice back in the interactive path.

**Severity order**, since §8.3 does not imply one:

```
vector_conversion_failed > sanitisation_removed_script > low_resolution >
smartart > font_substituted > probable_duplicate > alt_text_fallback
```

---

## 10. Read API (portal)

```
GET /admin/media/assets/{assetId}                  → metadata
GET /admin/media/assets?courseId=&q=&class=&limit= → search, metadata only
GET /admin/media/assets/{assetId}/thumb            → see §10.1
```

Search combines cosine similarity on `descriptors.embedding` with the text index on `tags`,
`altText` and `context.slideTitle`. Return `review.state` and `rights.license` on every hit so a
consumer can filter without a second call.

**Default to `retired: false`** and require an explicit `includeRetired=true`; unspecified, every
consumer forgets and retired assets resurface.

**`courseId` filtering needs `usage.courseIds`**, which only `attach_media` populates — out of scope
here. Until that ships, filter on `provenance.sourceDoc`.

### 10.1 How assets reach the browser (D5, open)

The bucket is private and read through the portal's `GET /documents/*` proxy. There is no CDN.

| Option | Gets you | Costs |
|---|---|---|
| **Portal proxy** (extend `GET /documents/*`) | Reuses existing auth; nothing new to build | Every image byte flows through a 2 GB service |
| **Presigned S3 URLs**, short TTL | No new infrastructure; bytes bypass the portal | No custom headers; no `immutable` caching; URLs expire, so they cannot be embedded in course content |
| **CloudFront + OAC** on the `media/` prefix | Proper caching, custom headers, a real asset origin | A distribution to configure and secure |

Given §5.1 — origin isolation does not protect the inline case anyway — presigned URLs are more
defensible than they first look, provided the sanitiser is solid and thumbnails render via `<img>`.
Start there; CloudFront is an optimisation, not a prerequisite.

---

## 11. Acceptance criteria

1. Ingesting a house deck yields at least one `shape_group` candidate per slide that visually
   contains a diagram, against a hand-labelled set of 20 slides. Recall ≥ 0.85, precision ≥ 0.80.
2. No logo, footer mark or page number appears as a registered asset in a 100-slide deck.
3. SVG output from the render path contains `<text>` elements, not paths, for every visible label.
4. A crafted PPTX containing an SVG with `<script>` and an `onload` attribute ingests successfully;
   the stored SVG contains neither, and `review.reasons` records the removal.
5. Re-ingesting an unchanged document creates zero new S3 blobs and returns the prior `jobId`.
6. Every registered asset has non-empty `altText` and a `rights.license` value.
7. A figure cropped on the slide via `srcRect` produces derivatives matching the on-slide crop.
8. A deck referencing a font absent from the host still produces assets; affected ones carry
   `font_substituted`. *(Not "the job fails" — that contradicts §1.)*
9. `thumb` exists for every asset, including SVG-sourced ones.
10. End-to-end over a 120-slide deck completes in under ten minutes — single ingest, no other load,
    cold start excluded, descriptors included.
11. No candidate region is emitted twice from one document. §3.1 frequency, §3.2 clustering and
    §4.4 fallback can each independently produce a candidate covering the same region.
12. A sanitised SVG still renders: a rasterisation of the sanitised output matches the original
    within a perceptual threshold. *(Criterion 4 proves the script is gone, not that the picture
    survived.)*

**Build the hand-labelled set first.** It is the only thing that makes §3.2's thresholds (0.25 in,
3 shapes, 8%, 85%) measured rather than guessed — an afternoon with a real deck, converting five
arbitrary constants into calibrated ones.

---

## 12. Known limitations to accept in v1

- SmartArt fidelity depends on the cached fallback drawing; flagged, not solved.
- Vector artwork inside PDFs converts less reliably than inside PPTX. Prefer the PPTX source where
  both exist; `provenance.sourceType` lets a later pass choose.
- Grouping heuristics will occasionally merge two adjacent diagrams into one candidate. Review
  catches it; splitting is v2.
- Chart redraw covers bar, column, line, pie and doughnut. Everything else falls to the render path.
- Animation and build steps are flattened — a diagram revealed in five clicks renders as its final
  state. Usually right, occasionally the wrong frame.
- Speaker notes may be empty across a whole deck, removing the better half of §7.2's context signal.
  Descriptor quality drops and the only signal for it is a weak proxy.
- Right-to-left and CJK text need fonts beyond the Verdana check and substitute silently.
- One document at a time. The portal's job loop is sequential; concurrent ingests queue.

---

## 13. What gets built where

| | Repository | New |
|---|---|---|
| Media service | `hslu-aire-doc-service` | `app_media/`, `Dockerfile.media`, `requirements-media.txt`, `buildspec.yml` builds both images |
| Orchestration, metadata, review, search | `hslu-aire-server` | `src/media/` — job loop, media-service client, descriptors, Mongo collections, admin routes |
| Infrastructure | — | one ECR repository, one App Runner service, one autoscaling configuration |

**No new repository and no new CodeBuild project.** One source zip, one build, two `docker build` /
`docker push` steps.

### 13.1 Media service sizing

Peak during a render: one 300 dpi page bitmap (~144 MB) + LibreOffice (~300–500 MB for a large
deck) + the Python process. That fits 4 GB **once**, not twice — so `MaxConcurrency: 1`, and the
instance is sized independently of both other services.

Suggested: **1 vCPU / 4 GB, MinSize 1, MaxSize 3, MaxConcurrency 1**. Cost is dominated by the
always-on floor — App Runner has no scale-to-zero, `MinSize` is at least 1:

| | Rate (Frankfurt) | Monthly |
|---|---|---|
| Provisioned memory, 4 GB | $0.007 / GB-h | **$20.44** |
| Active vCPU, ~20 decks × 10 min ≈ 3.3 h | $0.064 / vCPU-h | $0.21 |
| **Total** | | **≈ $21 / month** |

*(ECS Fargate per job would be ~$0.22/month at $0.04656/vCPU-h + $0.00511/GB-h, with no idle floor —
break-even is roughly 300 active hours per month. Rejected deliberately: the ~$20 difference buys no
ECS cluster, no task definition, no `RunTask` trigger, no per-task image pull, and a deploy path
identical to the services already running.)*

### 13.2 Image contents

`Dockerfile.media`: Python 3.12-slim, LibreOffice headless, fonts, `python-pptx`, PyMuPDF (or
`pypdfium2`), Pillow, `cairosvg`/`resvg`, `bleach`, `boto3`. **No Torch, no Docling** — roughly
1.5 GB against the doc service's 3.2 GB.

---

## 14. Open decisions

1. **D5 — how assets reach the browser** (§10.1). Recommendation: presigned URLs for v1.
2. **Font licensing.** Verdana and the MS core fonts are not redistributable in a container image
   without a licence. Either use metric-compatible substitutes (Liberation, DejaVu) and accept the
   §4.3 metric shift as a known limitation, or obtain the licence. The spec currently assumes fonts
   are simply present.
3. **PyMuPDF's AGPL licence** (§3.3).
4. **Whether inline SVG is needed at all**, or `<img>` suffices everywhere (§5.1). This decides how
   much the sanitiser has to carry.
5. **`<a>` and filter primitives** in the allowlist (§5.2) — keep or drop, but decide.
6. **OCR.** `descriptors.ocrText` is in the schema with nothing specified to produce it. Name a
   component (Tesseract, or Docling's OCR which is currently disabled) or mark the field reserved.
7. **Cross-document duplicate detection.** §8.3 covers duplicates within one document; across a
   course archive it is a different and larger problem.

---

## Appendix — inherited from the cells work

Three decisions from `delta-spec-doc-convert-cells.md` apply unchanged:

- **Streaming over materialising.** The reason `read_only=False` was rejected there (~120× file
  size) is the reason §3.3 renders one PDF page at a time.
- **Concurrency is a memory control, not a throughput knob.** `MaxConcurrency: 2` on the doc service
  exists because each request holds a document; `MaxConcurrency: 1` here exists for the same reason.
- **Measure before sizing.** The doc service stayed at 4 GB because measurement showed the 8 GB step
  was unnecessary. Do the same here: build the §11 hand-labelled set and render one real 120-slide
  deck before choosing an instance size.
