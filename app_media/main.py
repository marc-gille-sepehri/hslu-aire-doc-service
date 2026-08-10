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

from fastapi import Body, FastAPI, File, Form, Header, HTTPException, UploadFile

from . import libreoffice, storage
from .pdf_scan import scan_pdf
from .pptx_scan import scan_pptx
from .render_pipeline import render_candidate

app = FastAPI(title="hslu-aire-media", version="0.1.0")

SERVICE_TOKEN = os.environ.get("SERVICE_TOKEN", "").strip()
MAX_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(60 * 1024 * 1024)))


def _auth(authorization: str) -> None:
    if SERVICE_TOKEN and authorization != f"Bearer {SERVICE_TOKEN}":
        raise HTTPException(status_code=401, detail="unauthorized")


@app.get("/health")
def health():
    return {"ok": True, "service": "hslu-aire-media"}


@app.post("/v1/media/prepare")
async def prepare(
    file: UploadFile = File(...),
    jobId: str = Form(...),
    authorization: str = Header(default=""),
):
    """Deck → PDF + per-slide PNGs in S3, once per document (§4.3, §9).

    The slide renders are not a by-product: §9's review UI needs the whole slide
    with the candidate outlined, and re-rendering at review time would put
    LibreOffice back in the interactive path.
    """
    _auth(authorization)
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty file")
    if len(data) > MAX_BYTES:
        raise HTTPException(status_code=413, detail="file too large")

    source_sha = storage.sha256_of(data)
    name = file.filename or "deck.pptx"
    with tempfile.TemporaryDirectory() as tmp:
        deck = Path(tmp) / name
        deck.write_bytes(data)
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
    file: UploadFile = File(...),
    authorization: str = Header(default=""),
):
    """Enumerate figure candidates (§3). Pure XML work — fast, no rendering."""
    _auth(authorization)
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty file")

    name = file.filename or "deck.pptx"
    is_pdf = name.lower().endswith(".pdf")

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / name
        path.write_bytes(data)
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
        "sourceSha256": storage.sha256_of(data),
        "sourceType": "pdf" if is_pdf else "pptx",
        "slideSizeEmu": page.as_dict(),
        "slides": slides,
        "frequencyRejections": rejections,
    }


@app.post("/v1/media/render")
async def render(
    file: UploadFile = File(...),
    spec: str = Form(...),
    authorization: str = Header(default=""),
):
    """Render every candidate on one slide and return what the portal needs to
    index them.

    Batched per slide rather than per candidate: §2.1 puts the loop in the portal,
    and one round trip per figure over a 120-slide deck is hundreds of requests
    for no benefit. The source document is uploaded with the request instead of
    the portal shipping extracted bytes back and forth — the media service is the
    one that can read a PPTX part, so extraction stays on this side of the wire.

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

    data = await file.read()
    if len(data) > MAX_BYTES:
        raise HTTPException(status_code=413, detail="file too large")

    name = file.filename or "source"
    source_type = "pptx" if name.lower().endswith((".pptx", ".pptm", ".ppt")) else "pdf"
    needs_pdf = any(c.get("class") in ("shape_group", "chart") for c in candidates) or \
        (source_type == "pdf")

    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / name
        source.write_bytes(data)

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

        missing = libreoffice.check_fonts(set(parsed.get("requiredFonts") or []))
        results = [
            render_candidate(source, c, pdf, source_type, missing).as_dict()
            for c in candidates
        ]

    return {"slide": parsed.get("slide"), "results": results,
            "missingFonts": sorted(missing)}


@app.post("/v1/media/cleanup")
async def cleanup(payload: dict = Body(...), authorization: str = Header(default="")):
    """Remove `media/work/<jobId>/`. Blobs are never deleted (§1, §6)."""
    _auth(authorization)
    job_id = payload.get("jobId")
    if not job_id:
        raise HTTPException(status_code=400, detail="jobId required")
    return {"removed": storage.delete_prefix(storage.work_key(job_id, ""))}
