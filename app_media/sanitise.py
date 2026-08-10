"""SVG sanitisation — spec-media-extraction.md §5.

This operates on a parsed tree, never on strings. That is not a stylistic
preference: §5.1 establishes that when an SVG is inlined into the portal's DOM,
response headers on the asset origin do not apply and this sanitiser is the only
thing standing between a crafted deck and stored XSS. A regex-based cleaner is
not adequate for that job.

Written against lxml rather than bleach: bleach's allowlist is HTML-shaped and
would need an SVG-specific configuration anyway, and a dependency that carries
the security weight of the pipeline should be one whose behaviour is visible here.
"""
from __future__ import annotations

import re

from lxml import etree

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"

# §5 element allowlist. Anything not named here is unwrapped (children kept) or
# dropped entirely — see STRIP_WITH_CHILDREN.
ALLOWED_ELEMENTS = {
    "svg", "g", "defs", "title", "desc", "path", "rect", "circle", "ellipse",
    "line", "polyline", "polygon", "text", "tspan", "textPath", "image", "use",
    "symbol", "marker", "linearGradient", "radialGradient", "stop", "clipPath",
    "mask", "pattern", "style",
}

# Removed with their subtree — keeping the children of a <script> would keep the
# script body as text.
STRIP_WITH_CHILDREN = {"script", "foreignObject", "handler", "set"}

# Elements counted for the §4.4 "fewer than 5 drawing elements" heuristic.
DRAWING_ELEMENTS = {
    "path", "rect", "circle", "ellipse", "line", "polyline", "polygon", "text", "image",
}

_ANIMATE = re.compile(r"^animate")
_DATA_OK = re.compile(r"^data:image/(png|jpeg);", re.I)
_URL_IN_CSS = re.compile(r"url\(\s*['\"]?([^'\")]+)", re.I)
_CSS_BANNED = re.compile(r"@import|behavior\s*:|expression\s*\(", re.I)

MAX_SVG_BYTES = 2 * 1024 * 1024
MAX_USE_DEPTH = 3
MAX_ELEMENTS_AFTER_EXPANSION = 50_000


class SanitisationError(Exception):
    pass


def _local(tag) -> str:
    if not isinstance(tag, str):
        return ""  # comment / PI
    return tag.rsplit("}", 1)[-1]


def _is_safe_href(value: str) -> bool:
    v = (value or "").strip()
    if v.startswith("#"):
        return True                      # same-document fragment — the only allowed reference
    return bool(_DATA_OK.match(v))       # data:image/png and data:image/jpeg only


def _clean_css(text: str) -> str:
    """Strip @import, off-document url() and behavior from a CSS string."""
    if not text:
        return text
    out = _CSS_BANNED.sub("/* removed */", text)

    def _url(match: re.Match) -> str:
        target = match.group(1).strip()
        return match.group(0) if _is_safe_href(target) else "url(#removed"

    return _URL_IN_CSS.sub(_url, out)


def sanitise_svg(raw: bytes, id_prefix: str) -> tuple[bytes, dict]:
    """Return (clean_svg_bytes, report).

    `report` carries what was removed (§8.3 feeds `sanitisationRemoved` into the
    review flags — a deck that ships a `<script>` is worth knowing about) and the
    drawing-element count §4.4 uses to detect a failed render.
    """
    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)
    try:
        root = etree.fromstring(raw, parser=parser)
    except etree.XMLSyntaxError as e:
        raise SanitisationError(f"not parseable as XML: {e}") from e

    if _local(root.tag) != "svg":
        raise SanitisationError(f"root element is <{_local(root.tag)}>, expected <svg>")

    removed: set[str] = set()

    # 1. Elements. Walk a materialised list — the tree is mutated during iteration.
    for el in list(root.iter()):
        name = _local(el.tag)
        if not isinstance(el.tag, str):          # comments, processing instructions
            el.getparent().remove(el)
            continue
        if name in STRIP_WITH_CHILDREN or _ANIMATE.match(name):
            removed.add(name)
            parent = el.getparent()
            if parent is not None:
                parent.remove(el)
            continue
        if name not in ALLOWED_ELEMENTS:
            removed.add(name)
            parent = el.getparent()
            if parent is not None:
                # Unwrap: an unknown container should not take its children with it.
                index = list(parent).index(el)
                for child in reversed(list(el)):
                    parent.insert(index, child)
                parent.remove(el)

    # 2. Attributes.
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        for attr in list(el.attrib):
            local = _local(attr)
            if local.startswith("on"):
                removed.add(attr)
                del el.attrib[attr]
            elif local == "href":
                if not _is_safe_href(el.attrib[attr]):
                    removed.add("href")
                    del el.attrib[attr]
            elif local == "style":
                el.attrib[attr] = _clean_css(el.attrib[attr])

    # 3. <style> bodies.
    for el in root.iter():
        if isinstance(el.tag, str) and _local(el.tag) == "style" and el.text:
            el.text = _clean_css(el.text)

    # 4. ID namespacing, so two inline SVGs on one page cannot collide (§5).
    _prefix_ids(root, id_prefix)

    # 5. <use> amplification guard (§5.2).
    _check_use_depth(root)

    total = sum(1 for _ in root.iter())
    if total > MAX_ELEMENTS_AFTER_EXPANSION:
        raise SanitisationError(f"{total} elements exceeds the expansion cap")

    clean = etree.tostring(root, xml_declaration=True, encoding="utf-8")
    if len(clean) > MAX_SVG_BYTES:
        raise SanitisationError(f"{len(clean)} bytes after sanitisation exceeds 2 MB")

    drawing_count = sum(1 for el in root.iter() if isinstance(el.tag, str) and _local(el.tag) in DRAWING_ELEMENTS)
    return clean, {"removed": sorted(removed), "drawingElementCount": drawing_count}


def _prefix_ids(root, prefix: str) -> None:
    mapping = {}
    for el in root.iter():
        if isinstance(el.tag, str) and "id" in el.attrib:
            old = el.attrib["id"]
            new = f"{prefix}-{old}"
            mapping[old] = new
            el.attrib["id"] = new
    if not mapping:
        return
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        for attr in list(el.attrib):
            value = el.attrib[attr]
            if value.startswith("#") and value[1:] in mapping:
                el.attrib[attr] = f"#{mapping[value[1:]]}"
            elif "url(#" in value:
                for old, new in mapping.items():
                    value = value.replace(f"url(#{old})", f"url(#{new})")
                el.attrib[attr] = value


def _check_use_depth(root) -> None:
    """A <use> chain into a <symbol> containing <use> expands exponentially."""
    targets = {}
    for el in root.iter():
        if isinstance(el.tag, str) and "id" in el.attrib:
            targets[el.attrib["id"]] = el

    def depth(el, seen: frozenset) -> int:
        best = 0
        for child in el.iter():
            if not isinstance(child.tag, str) or _local(child.tag) != "use":
                continue
            ref = child.get("href") or child.get(f"{{{XLINK_NS}}}href") or ""
            if not ref.startswith("#"):
                continue
            key = ref[1:]
            if key in seen:
                raise SanitisationError("cyclic <use> reference")
            target = targets.get(key)
            if target is not None:
                best = max(best, 1 + depth(target, seen | {key}))
        return best

    if depth(root, frozenset()) > MAX_USE_DEPTH:
        raise SanitisationError(f"<use> nesting deeper than {MAX_USE_DEPTH}")
