"""EMU conversions and the rectangle type the whole pipeline passes around.

OOXML measures everything in English Metric Units. The two constants below are
exact, not approximations: 914400 EMU = 1 inch and 914400 / 72 = 12700 EMU = 1 pt.
For the house canvas (26.67 x 15 in) the slide is 1920 x 1080 pt, which maps 1:1
onto PDF user space — so a candidate's bounding box in points is directly the
crop rectangle, with no scale factor. See spec-media-extraction.md §3.2.
"""
from dataclasses import dataclass

EMU_PER_INCH = 914400
EMU_PER_POINT = 12700
POINTS_PER_INCH = 72
# CSS reference pixel density. Needed for §8.2 — pixelWidth is device pixels and
# displayWidth is points; omitting this understates the requirement by 33%.
CSS_PX_PER_POINT = 96 / 72


def emu_to_pt(emu: int) -> float:
    return emu / EMU_PER_POINT


def emu_to_in(emu: int) -> float:
    return emu / EMU_PER_INCH


def in_to_emu(inches: float) -> int:
    return round(inches * EMU_PER_INCH)


@dataclass(frozen=True)
class RectEmu:
    """An axis-aligned rectangle in EMU. Immutable so clusters can be dict keys."""

    l: int
    t: int
    w: int
    h: int

    @property
    def r(self) -> int:
        return self.l + self.w

    @property
    def b(self) -> int:
        return self.t + self.h

    @property
    def area(self) -> int:
        return self.w * self.h

    def union(self, other: "RectEmu") -> "RectEmu":
        l, t = min(self.l, other.l), min(self.t, other.t)
        return RectEmu(l, t, max(self.r, other.r) - l, max(self.b, other.b) - t)

    def gap_to(self, other: "RectEmu") -> int:
        """Shortest edge-to-edge distance; 0 when the rectangles touch or overlap."""
        dx = max(0, max(self.l - other.r, other.l - self.r))
        dy = max(0, max(self.t - other.b, other.t - self.b))
        if dx == 0 and dy == 0:
            return 0
        if dx == 0:
            return dy
        if dy == 0:
            return dx
        return round((dx * dx + dy * dy) ** 0.5)

    def as_points(self) -> dict:
        return {
            "l": round(emu_to_pt(self.l), 2),
            "t": round(emu_to_pt(self.t), 2),
            "w": round(emu_to_pt(self.w), 2),
            "h": round(emu_to_pt(self.h), 2),
        }

    def as_dict(self) -> dict:
        return {"l": self.l, "t": self.t, "w": self.w, "h": self.h}


def resolution_adequacy(pixel_width: int, display_width_pt: float, target_density: float = 2.0) -> float:
    """§8.2. Returns 0.0 rather than dividing by zero on a degenerate placement."""
    denominator = display_width_pt * CSS_PX_PER_POINT * target_density
    if denominator <= 0:
        return 0.0
    return pixel_width / denominator
