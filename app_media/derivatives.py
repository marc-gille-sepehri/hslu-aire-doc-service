"""Raster derivatives — spec-media-extraction.md §6.

`web` and `thumb` are always PNG, rendered at 2x the target edge and downsampled,
so no consumer ever rasterises at request time. EXIF is stripped; CMYK is
converted to sRGB through an ICC profile where one is embedded, because the naive
channel formula shifts colour visibly.
"""
from __future__ import annotations

import io

from PIL import Image, ImageCms

WEB_EDGE = 1600
THUMB_EDGE = 384
SUPERSAMPLE = 2
# §5 set this at "20 MB decoded", which works out to 5.2 megapixels — below an
# ordinary high-DPI screenshot. Measured on real course material: a 3713x2475
# screenshot (9.2 MP) was rejected, and with it the whole figure. The guard is
# meant to stop a decompression bomb, not content, so it is expressed in pixels
# and set far above anything a slide legitimately carries: 50 MP is ~200 MB
# decoded, which one render at MaxConcurrency 1 absorbs, while a real bomb
# (50000x50000 = 2500 MP) is still refused before it is decoded.
MAX_DECODED_PIXELS = 50_000_000


class DerivativeError(RuntimeError):
    pass


def _open_bounded(data: bytes) -> Image.Image:
    """Reject a decompression bomb from the header, before decoding it."""
    image = Image.open(io.BytesIO(data))
    w, h = image.size
    if w * h > MAX_DECODED_PIXELS:
        raise DerivativeError(
            f"{w}x{h} = {w * h / 1e6:.1f} MP exceeds the {MAX_DECODED_PIXELS / 1e6:.0f} MP decode limit"
        )
    image.load()
    return image


def normalise(data: bytes) -> Image.Image:
    """Strip EXIF, convert CMYK to sRGB, return an RGB/RGBA image."""
    image = _open_bounded(data)
    if image.mode == "CMYK":
        profile = image.info.get("icc_profile")
        if profile:
            src = ImageCms.ImageCmsProfile(io.BytesIO(profile))
            image = ImageCms.profileToProfile(image, src, ImageCms.createProfile("sRGB"),
                                              outputMode="RGB")
        else:
            # No embedded profile: the naive conversion is the only option, and it
            # is a known colour shift rather than a silent one.
            image = image.convert("RGB")
    elif image.mode not in ("RGB", "RGBA"):
        image = image.convert("RGBA" if "A" in image.getbands() else "RGB")

    clean = Image.new(image.mode, image.size)
    clean.putdata(list(image.getdata()))     # drops EXIF and every other chunk
    return clean


def crop_src_rect(image: Image.Image, src_rect: dict | None) -> Image.Image:
    """§3.1 — apply the on-slide crop. `srcRect` insets are in 1/100000 of the edge."""
    if not src_rect:
        return image
    w, h = image.size
    left = int(w * src_rect.get("l", 0) / 100000)
    top = int(h * src_rect.get("t", 0) / 100000)
    right = w - int(w * src_rect.get("r", 0) / 100000)
    bottom = h - int(h * src_rect.get("b", 0) / 100000)
    if right <= left or bottom <= top:
        return image
    return image.crop((left, top, right, bottom))


def _resize(image: Image.Image, edge: int) -> Image.Image:
    w, h = image.size
    if max(w, h) <= edge:
        return image.copy()
    scale = edge / max(w, h)
    over = image.resize((max(1, int(w * scale * SUPERSAMPLE)),
                         max(1, int(h * scale * SUPERSAMPLE))), Image.LANCZOS)
    return over.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)


def _png(image: Image.Image) -> bytes:
    buf = io.BytesIO()
    image.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def raster_derivatives(image: Image.Image) -> tuple[bytes, bytes]:
    return _png(_resize(image, WEB_EDGE)), _png(_resize(image, THUMB_EDGE))


def svg_to_png(svg: bytes, edge: int) -> bytes:
    """§6 — every asset needs a thumb, including SVG-sourced ones (§11.9).

    Imported lazily so the module is usable without the renderer installed; the
    dependency is real in `Dockerfile.media` but absent from a light dev setup.
    """
    try:
        import cairosvg
    except ImportError as e:
        raise DerivativeError(
            "cairosvg is not installed — SVG assets cannot get a thumbnail"
        ) from e
    return cairosvg.svg2png(bytestring=svg, output_width=edge * SUPERSAMPLE)
