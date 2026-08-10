"""Crop a page-sized SVG down to one candidate — spec-media-extraction.md §4.3.

Steps 3 and 4 of the render ladder: set the `viewBox` to the candidate's box in
points and drop what falls outside it. Geometry is never rescaled — §3.2
establishes that the slide's point coordinates map 1:1 onto PDF user space, so
the bounding box *is* the crop rectangle. Rescaling here would silently
invalidate that.

Elements that straddle the boundary are kept and clipped, not dropped: half a
connector is better than a diagram missing its arrows.
"""
from __future__ import annotations

import re

from lxml import etree

from .sanitise import _local  # noqa: PLC2701 — same package, same parsing conventions
from .units import RectEmu, emu_to_pt

SVG_NS = "http://www.w3.org/2000/svg"

# Elements whose extent we can estimate cheaply. Anything else is kept: dropping
# an element we cannot measure would remove content on a guess.
_MEASURABLE = {"rect", "circle", "ellipse", "line", "image", "use", "text"}
_NUM = re.compile(r"-?\d+(?:\.\d+)?")


class CropError(RuntimeError):
    pass


def crop_svg(svg: bytes, box_emu: RectEmu, clip_id: str = "cand") -> bytes:
    """Return an SVG showing only `box_emu`, in points."""
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    try:
        root = etree.fromstring(svg, parser=parser)
    except etree.XMLSyntaxError as e:
        raise CropError(f"not parseable: {e}") from e
    if _local(root.tag) != "svg":
        raise CropError(f"root is <{_local(root.tag)}>, expected <svg>")

    l, t = emu_to_pt(box_emu.l), emu_to_pt(box_emu.t)
    w, h = emu_to_pt(box_emu.w), emu_to_pt(box_emu.h)
    if w <= 0 or h <= 0:
        raise CropError(f"degenerate crop box {w}x{h}")

    page = _page_box(root)
    _drop_outside(root, (l, t, l + w, t + h), page)

    # A clipPath makes the straddling case honest: the element stays in the tree
    # (so text remains selectable and correctable) but does not paint outside.
    defs = etree.SubElement(root, f"{{{SVG_NS}}}defs")
    clip = etree.SubElement(defs, f"{{{SVG_NS}}}clipPath")
    clip.set("id", clip_id)
    rect = etree.SubElement(clip, f"{{{SVG_NS}}}rect")
    for k, v in (("x", l), ("y", t), ("width", w), ("height", h)):
        rect.set(k, f"{v:.2f}")

    group = etree.Element(f"{{{SVG_NS}}}g")
    group.set("clip-path", f"url(#{clip_id})")
    for child in list(root):
        if child is defs:
            continue
        root.remove(child)
        group.append(child)
    root.append(group)

    root.set("viewBox", f"{l:.2f} {t:.2f} {w:.2f} {h:.2f}")
    root.set("width", f"{w:.2f}pt")
    root.set("height", f"{h:.2f}pt")
    return etree.tostring(root, xml_declaration=True, encoding="utf-8")


def _page_box(root) -> tuple[float, float, float, float]:
    vb = root.get("viewBox")
    if vb:
        parts = [float(x) for x in _NUM.findall(vb)][:4]
        if len(parts) == 4:
            return parts[0], parts[1], parts[0] + parts[2], parts[1] + parts[3]
    return 0.0, 0.0, float("inf"), float("inf")


def _drop_outside(root, box, page) -> None:
    """Remove elements entirely outside `box`. Keep anything unmeasurable."""
    bl, bt, br, bb = box
    for el in list(root.iter()):
        if not isinstance(el.tag, str):
            continue
        name = _local(el.tag)
        if name not in _MEASURABLE:
            continue
        extent = _extent(el)
        if extent is None:
            continue                       # cannot measure — keep it
        el_l, el_t, el_r, el_b = extent
        if el_r < bl or el_l > br or el_b < bt or el_t > bb:
            parent = el.getparent()
            if parent is not None:
                parent.remove(el)


def _f(el, attr, default=None):
    try:
        return float(el.get(attr))
    except (TypeError, ValueError):
        return default


def _extent(el) -> tuple[float, float, float, float] | None:
    name = _local(el.tag)
    if name in ("rect", "image", "use"):
        x, y = _f(el, "x", 0.0), _f(el, "y", 0.0)
        w, h = _f(el, "width"), _f(el, "height")
        if w is None or h is None:
            return None
        return x, y, x + w, y + h
    if name == "circle":
        cx, cy, r = _f(el, "cx", 0.0), _f(el, "cy", 0.0), _f(el, "r")
        return None if r is None else (cx - r, cy - r, cx + r, cy + r)
    if name == "ellipse":
        cx, cy = _f(el, "cx", 0.0), _f(el, "cy", 0.0)
        rx, ry = _f(el, "rx"), _f(el, "ry")
        if rx is None or ry is None:
            return None
        return cx - rx, cy - ry, cx + rx, cy + ry
    if name == "line":
        x1, y1 = _f(el, "x1", 0.0), _f(el, "y1", 0.0)
        x2, y2 = _f(el, "x2", 0.0), _f(el, "y2", 0.0)
        return min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)
    if name == "text":
        x, y = _f(el, "x"), _f(el, "y")
        if x is None or y is None:
            return None
        # A point, not a box — the glyph extent is unknown without font metrics.
        # Treated as a point so that only text well outside the box is dropped.
        return x, y, x, y
    return None
