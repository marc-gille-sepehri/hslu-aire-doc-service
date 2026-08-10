"""PDF enumeration — spec-media-extraction.md §3.3.

XObject extraction is not enough: many producers emit one visual image as dozens
of horizontal strips, alpha arrives as separate SMask objects, and vector artwork
is not an image at all. So this works from the content stream — drawing
operations and image placements with their rectangles — and reuses the same
clustering the PPTX path uses.

Memory: one page at a time, released immediately. A 1920x1080 pt page at 300 dpi
is ~144 MB as RGBA; holding a document, or two pages at once, is how this OOMs a
4 GB instance.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import fitz

from .cluster import Box, cluster_boxes, filter_clusters
from .units import EMU_PER_POINT, RectEmu

HEADER_FOOTER_BAND = 0.08     # §3.3 — top and bottom 8%
WIDE_ENOUGH = 0.20            # unless wider than 20% of the page
RASTER_COVERAGE = 0.90        # §3.3 step 3
EXTRACT_DPI = 300             # §3.3 step 4


def _pt_to_emu(v: float) -> int:
    return round(v * EMU_PER_POINT)


def _rect(r) -> RectEmu:
    return RectEmu(_pt_to_emu(r.x0), _pt_to_emu(r.y0),
                   _pt_to_emu(r.width), _pt_to_emu(r.height))


@dataclass
class PdfCandidate:
    page: int
    cls: str
    rect: RectEmu
    nearest_heading: str | None = None

    def as_dict(self) -> dict:
        return {
            "slide": self.page,          # same key as PPTX so the portal has one shape
            "class": self.cls,
            "boundingBoxEmu": self.rect.as_dict(),
            "boundingBoxPt": self.rect.as_points(),
            "shapeIds": [],
            "authorAltText": None,
            "shapeName": None,
            "srcRect": None,
            "containedSmartArt": False,
            "nearestHeading": self.nearest_heading,
        }


@dataclass
class PageScan:
    number: int
    text: str
    candidates: list[PdfCandidate] = field(default_factory=list)


def scan_pdf(path: str) -> tuple[list[PageScan], RectEmu]:
    doc = fitz.open(path)
    scans: list[PageScan] = []
    page_rect = RectEmu(0, 0, 0, 0)
    try:
        # Repeated placements across pages are furniture, not figures — collect
        # first, filter second (§3.3's "not repeated across pages").
        seen_positions: dict[tuple[int, int, int, int], int] = {}
        raw: list[tuple[int, list, RectEmu, str]] = []

        for number in range(doc.page_count):
            page = doc.load_page(number)
            page_rect = _rect(page.rect)
            boxes: list[Box] = []

            for i, drawing in enumerate(page.get_drawings()):
                r = drawing.get("rect")
                if r is None or r.is_empty:
                    continue
                filled = bool(drawing.get("fill")) or bool(drawing.get("color"))
                boxes.append(Box(key=f"d{i}", rect=_rect(r), has_fill=filled,
                                 is_connector=drawing.get("type") == "s"))

            for i, info in enumerate(page.get_images(full=True)):
                for r in page.get_image_rects(info[0]):
                    if r.is_empty:
                        continue
                    boxes.append(Box(key=f"i{i}", rect=_rect(r), has_fill=True, is_image=True))

            for cluster in filter_clusters(cluster_boxes(boxes), page_rect):
                key = (cluster.rect.l // EMU_PER_POINT, cluster.rect.t // EMU_PER_POINT,
                       cluster.rect.w // EMU_PER_POINT, cluster.rect.h // EMU_PER_POINT)
                seen_positions[key] = seen_positions.get(key, 0) + 1
                cls = ("raster"
                       if cluster.image_area >= cluster.rect.area * RASTER_COVERAGE
                       else "shape_group")
                raw.append((number + 1, [key], cluster.rect, cls))

            scans.append(PageScan(number=number + 1, text=page.get_text().strip()[:2000]))
            del page

        for page_no, keys, rect, cls in raw:
            if _in_band(rect, page_rect) and seen_positions.get(keys[0], 0) > 1:
                continue
            scans[page_no - 1].candidates.append(PdfCandidate(page_no, cls, rect))
    finally:
        doc.close()
    return scans, page_rect


def _in_band(rect: RectEmu, page: RectEmu) -> bool:
    """§3.3 — header/footer band, unless the cluster is wide enough to be content."""
    if rect.w >= page.w * WIDE_ENOUGH:
        return False
    top_band = page.t + page.h * HEADER_FOOTER_BAND
    bottom_band = page.b - page.h * HEADER_FOOTER_BAND
    return rect.b <= top_band or rect.t >= bottom_band


def render_region(pdf_path: str, page_number: int, rect: RectEmu, dpi: int = EXTRACT_DPI) -> bytes:
    """Crop `rect` out of one page at `dpi`, as PNG. One page, then released."""
    doc = fitz.open(pdf_path)
    try:
        page = doc.load_page(page_number - 1)
        clip = fitz.Rect(rect.l / EMU_PER_POINT, rect.t / EMU_PER_POINT,
                         rect.r / EMU_PER_POINT, rect.b / EMU_PER_POINT)
        pix = page.get_pixmap(dpi=dpi, clip=clip)
        try:
            return pix.tobytes("png")
        finally:
            del pix
    finally:
        doc.close()


def page_to_svg(pdf_path: str, page_number: int) -> bytes:
    """One PDF page as SVG, with real `<text>` elements.

    `text_as_path=False` is load-bearing, not a tuning flag. PyMuPDF's **default
    converts every glyph to paths** — measured on a one-line page: 16 `<path>`
    and zero `<text>` by default, against 1 `<text>` and 2 `<path>` with the flag.
    That default is precisely the failure §4.3 warns about for `pdftocairo` and
    §11.3 forbids: output that is not searchable, not correctable and worthless
    to a screen reader.

    Deviation from §4.3's letter, kept within its intent: the spec names
    LibreOffice's SVG exporter, *because* it preserves text. Going deck → PDF
    (LibreOffice, once) → page SVG (here) satisfies that reason and avoids
    LibreOffice's per-page SVG export, which only emits the first slide of a
    deck. See §11.3 for the test that holds this honest.
    """
    doc = fitz.open(pdf_path)
    try:
        svg = doc.load_page(page_number - 1).get_svg_image(text_as_path=False)
        return svg.encode("utf-8")
    finally:
        doc.close()
