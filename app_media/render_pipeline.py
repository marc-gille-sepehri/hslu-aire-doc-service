"""The §4 conversion ladder, one candidate at a time.

Everything here runs inside the media service and returns only what the portal
needs to index the result: a content hash, blob keys, geometry and technical
flags. No assetId, no metadata, nothing written to a database — that boundary is
§0 and it is the reason this service can stay a plain request/response App
Runner service.

`provenance.method` records which rung of the ladder produced the output, so a
later pass can tell a faithful pass-through from a render that merely didn't
fail.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path

from . import libreoffice, pdf_scan, pptx_scan, storage
from .derivatives import (
    DerivativeError,
    crop_src_rect,
    normalise,
    raster_derivatives,
    svg_to_png,
)
from .sanitise import DRAWING_ELEMENTS, SanitisationError, sanitise_svg
from .svg_crop import CropError, crop_svg
from .units import RectEmu, emu_to_pt, resolution_adequacy

MIN_DRAWING_ELEMENTS = 5      # §4.4 — the symptom of a failed render


@dataclass
class RenderResult:
    ok: bool
    sha256: str | None = None
    media_type: str | None = None
    bytes: int = 0
    blob_keys: dict = field(default_factory=dict)
    dimensions: dict | None = None
    method: str = ""
    flags: dict = field(default_factory=dict)
    existed: bool = False
    error: str | None = None

    def as_dict(self) -> dict:
        if not self.ok:
            return {"ok": False, "error": self.error}
        return {
            "ok": True,
            "sha256": self.sha256,
            "mediaType": self.media_type,
            "bytes": self.bytes,
            "blobKeys": self.blob_keys,
            "dimensions": self.dimensions,
            "method": self.method,
            "existed": self.existed,
            "technicalFlags": self.flags,
        }


def _rect(d: dict) -> RectEmu:
    return RectEmu(int(d["l"]), int(d["t"]), int(d["w"]), int(d["h"]))


def _store_svg(clean: bytes, flags: dict) -> tuple[storage.PutResult, dict]:
    original = storage.put_blob(clean, "original.svg", "image/svg+xml")
    keys = {"original": original.key}
    try:
        keys["web"] = storage.put_blob(svg_to_png(clean, 1600), "web.png", "image/png").key
        keys["thumb"] = storage.put_blob(svg_to_png(clean, 384), "thumb.png", "image/png").key
    except DerivativeError as e:
        # §11.9 wants a thumb for every asset, but losing a good SVG because the
        # rasteriser is missing would be the worse trade. Flag and continue.
        flags["thumbnailFailed"] = str(e)
    return original, keys


def _store_raster(image, flags: dict) -> tuple[storage.PutResult, dict, dict]:
    buf = io.BytesIO()
    image.save(buf, format="PNG", optimize=True)
    original = storage.put_blob(buf.getvalue(), "original.png", "image/png")
    web, thumb = raster_derivatives(image)
    keys = {
        "original": original.key,
        "web": storage.put_blob(web, "web.png", "image/png").key,
        "thumb": storage.put_blob(thumb, "thumb.png", "image/png").key,
    }
    return original, keys, {"w": image.size[0], "h": image.size[1], "unit": "px"}


def _svg_result(clean: bytes, report: dict, method: str, confidence: float,
                box: RectEmu | None) -> RenderResult:
    flags = {
        "vectorConfidence": confidence,
        "drawingElementCount": report["drawingElementCount"],
        "sanitisationRemoved": report["removed"],
    }
    original, keys = _store_svg(clean, flags)
    return RenderResult(
        ok=True, sha256=original.sha256, media_type="image/svg+xml",
        bytes=original.bytes, blob_keys=keys, method=method, flags=flags,
        existed=original.existed,
        dimensions=(box.as_points() | {"unit": "pt"}) if box else None,
    )


def render_candidate(
    source: Path,
    candidate: dict,
    pdf: Path | None,
    source_type: str,
    missing_fonts: set[str] | None = None,
) -> RenderResult:
    """Dispatch one candidate down the §4 ladder."""
    cls = candidate.get("class")
    box = _rect(candidate["boundingBoxEmu"]) if candidate.get("boundingBoxEmu") else None
    slide = int(candidate.get("slide", 1))
    # Addressing a shape inside the PPTX and addressing a page in the exported
    # PDF are two different numbers whenever the deck hides slides — LibreOffice
    # omits those from the export. Defaults to `slide` for PDF sources and for
    # any caller predating the field.
    pdf_page = int(candidate.get("pdfPage") or slide)
    shape_ids = candidate.get("shapeIds") or []

    try:
        if cls == "svg_native" and source_type == "pptx":
            raw, _ = pptx_scan.extract_media(str(source), slide, shape_ids[0])
            clean, report = sanitise_svg(raw, candidate.get("idPrefix", "a"))
            return _svg_result(clean, report, "passthrough_svg", 1.0, box)   # §4.1

        if cls == "vector_import" and source_type == "pptx":
            return _render_vector_import(source, slide, pdf_page, shape_ids, candidate, box, pdf)

        if cls == "raster":
            return _render_raster(source, slide, pdf_page, shape_ids, candidate, box,
                                  source_type, pdf)

        # chart and shape_group both take the render path in v1: §12 scopes chart
        # redraw to a later iteration, and §4.2 says an unsupported chart type
        # falls back here and is marked.
        return _render_and_crop(pdf, pdf_page, box, candidate, cls, missing_fonts)

    except (SanitisationError, CropError, DerivativeError, KeyError, ValueError) as e:
        return RenderResult(ok=False, error=f"{type(e).__name__}: {e}")


def _render_vector_import(source, slide, pdf_page, shape_ids, candidate, box, pdf) -> RenderResult:
    """§4.2 — EMF/WMF through LibreOffice."""
    raw, ext = pptx_scan.extract_media(str(source), slide, shape_ids[0])
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / f"art.{ext}"
        src.write_bytes(raw)
        try:
            produced = libreoffice.convert(src, Path(tmp) / "out", "svg")
        except libreoffice.LibreOfficeError:
            return _render_and_crop(pdf, pdf_page, box, candidate, "vector_import", None)
        clean, report = sanitise_svg(produced.read_bytes(), candidate.get("idPrefix", "a"))
    return _svg_result(clean, report, "convert_libreoffice", 0.9, box)


def _render_raster(source, slide, pdf_page, shape_ids, candidate, box, source_type, pdf) -> RenderResult:
    if source_type == "pptx":
        raw, _ = pptx_scan.extract_media(str(source), slide, shape_ids[0])
        image = crop_src_rect(normalise(raw), candidate.get("srcRect"))   # §3.1
    else:
        if pdf is None:
            raise ValueError("PDF raster candidate needs the prepared PDF")
        image = normalise(pdf_scan.render_region(str(pdf), pdf_page, box))

    flags = {"vectorConfidence": 0.0}
    if box:
        flags["resolutionAdequacy"] = round(
            resolution_adequacy(image.size[0], emu_to_pt(box.w)), 3
        )
    original, keys, dims = _store_raster(image, flags)
    return RenderResult(
        ok=True, sha256=original.sha256, media_type="image/png", bytes=original.bytes,
        blob_keys=keys, dimensions=dims, method="extract_raster", flags=flags,
        existed=original.existed,
    )


def _render_and_crop(pdf, pdf_page, box, candidate, cls, missing_fonts) -> RenderResult:
    """§4.3 — page SVG, cropped to the candidate; §4.4 raster fallback.

    Takes a PDF page, not a slide number: the two differ once a deck hides
    slides, and this is the path where the difference silently renders the wrong
    picture instead of raising.
    """
    if pdf is None or box is None:
        raise ValueError("shape_group needs the prepared PDF and a bounding box")

    confidence = 0.4 if candidate.get("containedSmartArt") else 0.7   # §4.3
    flags: dict = {}
    if missing_fonts:
        flags["fontSubstituted"] = sorted(missing_fonts)

    try:
        page_svg = pdf_scan.page_to_svg(str(pdf), pdf_page)
        cropped = crop_svg(page_svg, box, clip_id=candidate.get("idPrefix", "cand"))
        clean, report = sanitise_svg(cropped, candidate.get("idPrefix", "a"))
        if report["drawingElementCount"] >= MIN_DRAWING_ELEMENTS:
            result = _svg_result(clean, report, "render_crop_libreoffice", confidence, box)
            result.flags.update(flags)
            if cls == "chart":
                # §4.2: the redraw is not in v1, so every chart arrives here.
                result.flags["chartRedrawUnavailable"] = True
            return result
        reason = f"only {report['drawingElementCount']} drawing elements"
    except (CropError, SanitisationError) as e:
        reason = str(e)

    # §4.4 — still write something usable, and say why it is a raster.
    image = normalise(pdf_scan.render_region(str(pdf), pdf_page, box))
    flags |= {"vectorConfidence": 0.0, "vectorConversionFailed": reason,
              "resolutionAdequacy": round(resolution_adequacy(image.size[0], emu_to_pt(box.w)), 3)}
    original, keys, dims = _store_raster(image, flags)
    return RenderResult(
        ok=True, sha256=original.sha256, media_type="image/png", bytes=original.bytes,
        blob_keys=keys, dimensions=dims, method="render_crop_raster_fallback",
        flags=flags, existed=original.existed,
    )
