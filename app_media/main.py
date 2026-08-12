"""hslu-aire-media — stateless extraction service.

Request in, data out. No database, no auth beyond the shared service token, no
LLM, nothing remembered between calls. The portal owns the job, the metadata and
the orchestration; this service owns parsing, rendering and sanitisation.

See docs/spec-media-extraction.md §0 for why the boundary sits here.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile

from . import libreoffice, source_cache, storage
from .pdf_scan import scan_pdf
from .pptx_scan import deck_fonts, scan_pptx
from .render_pipeline import render_candidate

app = FastAPI(title="hslu-aire-media", version="0.1.0")

SERVICE_TOKEN = os.environ.get("SERVICE_TOKEN", "").strip()
MAX_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(500 * 1024 * 1024)))


def _auth(authorization: str) -> None:
    if SERVICE_TOKEN and authorization != f"Bearer {SERVICE_TOKEN}":
        raise HTTPException(status_code=401, detail="unauthorized")


@app.get("/health")
def health():
    return {"ok": True, "service": "hslu-aire-media"}


@app.post("/v1/media/hash")
async def hash_source(
    sourceKey: str = Form(...),
    authorization: str = Header(default=""),
):
    """SHA-256 of an object in S3, without the caller ever reading it.

    §2's idempotency key is the source document's hash, and the portal used to
    have it for free because the upload passed through it. Once the browser
    uploads straight to S3 (§2.1) the portal holds only a key, and something has
    to turn that into a hash. Doing it here is not a workaround: the hash of what
    the extractor actually read is a stronger claim than the hash of what a
    client said it sent, and this is also the only process that can produce it
    without moving 285 MB somewhere it is not needed.

    Cheap in practice — it warms the cache the very next `prepare` call reads.
    """
    _auth(authorization)
    try:
        _, digest, size = source_cache.fetch(sourceKey, MAX_BYTES)
    except source_cache.SourceTooLarge as e:
        raise HTTPException(status_code=413, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001 — a missing key is the common case
        raise HTTPException(status_code=404, detail=f"cannot read {sourceKey}: {e}") from e
    return {"sourceSha256": digest, "bytes": size}



async def _materialise(
    tmp: str,
    file: UploadFile | None,
    source_key: str | None,
    fallback_name: str,
) -> tuple[Path, str]:
    """Put the source document on disk and return (path, sha256).

    Prefers `sourceKey`: the orchestrator already stored the upload in S3, and
    forwarding the bytes again means the file crosses the wire once per call —
    35 calls over a 285 MB deck is ten gigabytes moved to convey nothing new.
    Streaming it from S3 also keeps it off the Python heap, and the cache keeps
    the repeat calls of one job from re-downloading it at all.

    A direct upload still works, for local runs and curl.
    """
    if source_key:
        try:
            # NB: cached — outside `tmp`, and not the caller's to delete.
            path, digest, _ = source_cache.fetch(source_key, MAX_BYTES)
        except source_cache.SourceTooLarge as e:
            raise HTTPException(status_code=413, detail=str(e)) from e
        return path, digest

    if file is None:
        raise HTTPException(status_code=400, detail="either file or sourceKey is required")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty file")
    if len(data) > MAX_BYTES:
        raise HTTPException(status_code=413, detail="file too large")
    path = Path(tmp) / (file.filename or fallback_name)
    path.write_bytes(data)
    return path, storage.sha256_of(data)


@app.post("/v1/media/prepare")
async def prepare(
    jobId: str = Form(...),
    file: UploadFile | None = File(default=None),
    sourceKey: str | None = Form(default=None),
    authorization: str = Header(default=""),
):
    """Deck → PDF + per-slide PNGs in S3, once per document (§4.3, §9).

    The slide renders are not a by-product: §9's review UI needs the whole slide
    with the candidate outlined, and re-rendering at review time would put
    LibreOffice back in the interactive path.
    """
    _auth(authorization)
    with tempfile.TemporaryDirectory() as tmp:
        deck, source_sha = await _materialise(tmp, file, sourceKey, "deck.pptx")
        name = deck.name
        if name.lower().endswith(".pdf"):
            pdf = deck            # already a PDF — LibreOffice has nothing to do
        else:
            try:
                pdf = libreoffice.deck_to_pdf(deck, Path(tmp) / "out")
            except libreoffice.LibreOfficeError as e:
                raise HTTPException(status_code=502, detail=str(e))

        pdf_key = storage.work_key(jobId, "deck.pdf")
        storage.put_object(pdf_key, pdf.read_bytes(), "application/pdf")
        slides = _render_slides(pdf, source_sha)

    return {
        "sourceSha256": source_sha,
        "pdfKey": pdf_key,
        "slideCount": len(slides),
        "slideKeys": slides,
    }


def _render_slides(pdf: Path, source_sha: str) -> list[str]:
    """One page at a time, released immediately.

    §3.3: a 1920x1080 pt page at 300 dpi is ~144 MB as RGBA. Holding a whole
    document, or two pages at once, is how this OOMs a 4 GB instance.
    """
    import fitz

    keys: list[str] = []
    doc = fitz.open(pdf)
    try:
        for number in range(doc.page_count):
            page = doc.load_page(number)
            pix = page.get_pixmap(dpi=110)          # review context, not extraction fidelity
            keys.append(storage.put_object(
                storage.slide_key(source_sha, number + 1), pix.tobytes("png"), "image/png",
                cache_control=storage.IMMUTABLE,
            ))
            del pix
    finally:
        doc.close()
    return keys


@app.post("/v1/media/candidates")
async def candidates(
    file: UploadFile | None = File(default=None),
    sourceKey: str | None = Form(default=None),
    authorization: str = Header(default=""),
):
    """Enumerate figure candidates (§3). Pure XML work — fast, no rendering."""
    _auth(authorization)
    with tempfile.TemporaryDirectory() as tmp:
        path, source_sha = await _materialise(tmp, file, sourceKey, "deck.pptx")
        name = path.name
        is_pdf = name.lower().endswith(".pdf")
        try:
            if is_pdf:
                scans, page = scan_pdf(str(path))
                rejections: list[dict] = []
            else:
                scans, page, rejections = scan_pptx(str(path))
        except Exception as e:  # noqa: BLE001 — surface a clean message
            raise HTTPException(status_code=422, detail=f"cannot read document: {e}")

    if is_pdf:
        slides = [
            {
                "slide": s.number,
                # §7.1's context block. A PDF has no speaker notes and no layout
                # name; nearestHeading is the closest analogue and is per-candidate.
                "context": {"slideTitle": None, "surroundingText": s.text,
                            "speakerNotes": "", "layoutName": None},
                "candidates": [c.as_dict() for c in s.candidates],
            }
            for s in scans
        ]
    else:
        slides = [
            {
                "slide": s.number,
                # Hidden slides are skipped, so this is not s.number once a deck
                # has any: LibreOffice leaves them out of the PDF (§3.1).
                "pdfPage": s.pdf_page if s.pdf_page is not None else s.number,
                "context": {
                    "slideTitle": s.title,
                    "surroundingText": s.surrounding_text,
                    "speakerNotes": s.speaker_notes,
                    "layoutName": s.layout_name,
                },
                "candidates": [c.as_dict() for c in s.candidates],
            }
            for s in scans
        ]

    return {
        "sourceSha256": source_sha,
        "sourceType": "pdf" if is_pdf else "pptx",
        "slideSizeEmu": page.as_dict(),
        "slides": slides,
        "frequencyRejections": rejections,
    }


@app.post("/v1/media/render")
async def render(
    spec: str = Form(...),
    file: UploadFile | None = File(default=None),
    sourceKey: str | None = Form(default=None),
    authorization: str = Header(default=""),
):
    """Render every candidate on one slide and return what the portal needs to
    index them.

    Batched per slide rather than per candidate: §2.1 puts the loop in the portal,
    and one round trip per figure over a 120-slide deck is hundreds of requests
    for no benefit. The request names the source rather than carrying it: the
    portal wrote it to S3 at ingest, this service reads it from there (and keeps
    it on disk between the slides of one job), so extraction stays on this side
    of the wire without the deck crossing it once per slide.

    Returns no assetId and writes no metadata: that boundary is §0.
    """
    _auth(authorization)
    try:
        parsed = json.loads(spec)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"spec is not JSON: {e}")

    candidates = parsed.get("candidates") or []
    if not candidates:
        raise HTTPException(status_code=400, detail="no candidates")

    with tempfile.TemporaryDirectory() as tmp:
        source, _ = await _materialise(tmp, file, sourceKey, "source.pptx")
        name = source.name
        source_type = "pptx" if name.lower().endswith((".pptx", ".pptm", ".ppt")) else "pdf"
        needs_pdf = any(c.get("class") in ("shape_group", "chart") for c in candidates) or \
            (source_type == "pdf")

        pdf: Path | None = None
        if needs_pdf:
            pdf_key = parsed.get("pdfKey")
            if pdf_key:
                pdf = Path(tmp) / "deck.pdf"
                pdf.write_bytes(storage.get_object(pdf_key))
            elif source_type == "pdf":
                pdf = source
            else:
                raise HTTPException(
                    status_code=400,
                    detail="pdfKey is required for shape_group/chart candidates — run prepare first",
                )

        # Enumerate from the deck rather than from the request: the caller cannot
        # know which typefaces are inside a file it only forwards.
        required = set(parsed.get("requiredFonts") or [])
        if source_type == "pptx" and not required:
            try:
                required = deck_fonts(str(source))
            except Exception:       # a malformed part must not fail the render
                required = set()
        missing = libreoffice.check_fonts(required)
        results = [
            render_candidate(source, c, pdf, source_type, missing).as_dict()
            for c in candidates
        ]

    return {"slide": parsed.get("slide"), "results": results,
            "missingFonts": sorted(missing)}


@app.post("/v1/media/cleanup")
async def cleanup(jobId: str = Form(...), authorization: str = Header(default="")):
    """Remove `media/work/<jobId>/`. Blobs are never deleted (§1, §6).

    A form field, like every other endpoint on this service. It used to take a
    JSON body while the portal sent multipart, so every call answered 422 and no
    job ever cleaned up after itself. Silently: the caller treats cleanup as
    best-effort and only warns, which is the right call for a few stale megabytes
    and the reason nobody noticed 459 MB of them.

    Mixing `Form` and `Body` on one endpoint is not an option — FastAPI parses a
    request body one way or the other — and matching the caller matters more than
    keeping the JSON shape nothing was using.
    """
    _auth(authorization)
    return {"removed": storage.delete_prefix(storage.work_key(jobId, ""))}
