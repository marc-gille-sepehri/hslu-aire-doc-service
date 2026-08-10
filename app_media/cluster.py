"""Bounding-box clustering — spec-media-extraction.md §3.2.

Shared by the PPTX and PDF paths: both reduce a page to a list of placed boxes
and then ask the same question, "which of these are one illustration?".

Determinism is a correctness requirement, not a nicety. §3.2 step 2 merges "into
an existing cluster", which makes the result depend on visit order; §2 promises
idempotency on the source hash. Both hold only if the input is sorted and the
merge runs to a fixed point, which is what `cluster_boxes` does.

The five thresholds below are the spec's, and they are guesses until the §11
hand-labelled set exists. They are module constants so that calibration is a
diff to one place.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .units import EMU_PER_INCH, RectEmu

GAP_EMU = round(0.25 * EMU_PER_INCH)   # §3.2 step 2
MIN_SHAPES = 3                          # §3.2 step 3
MIN_AREA_FRACTION = 0.08                # §3.2 step 3
FULL_SLIDE_FRACTION = 0.85              # §3.2 step 5


@dataclass
class Box:
    """One placed thing on a page."""

    key: str                 # shape id, or a synthetic id for PDF drawings
    rect: RectEmu
    has_fill: bool = False   # §3.2 step 4 — something is actually drawn
    is_connector: bool = False
    is_text: bool = False
    is_image: bool = False
    preformed_group: bool = False


@dataclass
class Cluster:
    boxes: list[Box] = field(default_factory=list)

    @property
    def rect(self) -> RectEmu:
        r = self.boxes[0].rect
        for b in self.boxes[1:]:
            r = r.union(b.rect)
        return r

    @property
    def keys(self) -> list[str]:
        return [b.key for b in self.boxes]

    @property
    def draws_something(self) -> bool:
        """§3.2 step 4: a cluster with no fill and no connector is a bullet layout."""
        return any(b.has_fill or b.is_connector or b.is_image for b in self.boxes)

    @property
    def image_area(self) -> int:
        return sum(b.rect.area for b in self.boxes if b.is_image)


def cluster_boxes(boxes: list[Box], gap_emu: int = GAP_EMU) -> list[Cluster]:
    """Merge boxes within `gap_emu` of each other, deterministically.

    Sorted by (top, left, key) so the traversal order is a property of the page
    rather than of dict ordering, then iterated to a fixed point so that a late
    box bridging two earlier clusters merges them rather than joining one
    arbitrarily.
    """
    ordered = sorted(boxes, key=lambda b: (b.rect.t, b.rect.l, b.key))
    clusters: list[Cluster] = []

    for box in ordered:
        if box.preformed_group:
            clusters.append(Cluster([box]))       # §3.2 step 1 — groups are given
            continue
        hit = next(
            (c for c in clusters
             if not c.boxes[0].preformed_group
             and any(box.rect.gap_to(m.rect) <= gap_emu for m in c.boxes)),
            None,
        )
        if hit is None:
            clusters.append(Cluster([box]))
        else:
            hit.boxes.append(box)

    # Fixed point: merging can bring two clusters within the gap of each other.
    changed = True
    while changed:
        changed = False
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                a, b = clusters[i], clusters[j]
                if a.boxes[0].preformed_group or b.boxes[0].preformed_group:
                    continue
                if any(x.rect.gap_to(y.rect) <= gap_emu for x in a.boxes for y in b.boxes):
                    a.boxes.extend(b.boxes)
                    a.boxes.sort(key=lambda x: (x.rect.t, x.rect.l, x.key))
                    del clusters[j]
                    changed = True
                    break
            if changed:
                break

    return clusters


def filter_clusters(clusters: list[Cluster], page: RectEmu) -> list[Cluster]:
    """§3.2 steps 3–5. Returns the surviving candidates, largest area first."""
    kept: list[Cluster] = []
    for c in clusters:
        if len(c.boxes) < MIN_SHAPES and not c.boxes[0].preformed_group:
            continue
        if c.rect.area < page.area * MIN_AREA_FRACTION:
            continue
        if not c.draws_something:
            continue                              # step 4: a bullet layout, not a diagram
        if c.rect.area > page.area * FULL_SLIDE_FRACTION:
            # Step 5 promotes the cluster to the whole content area — but only
            # because step 4 already passed. Without that guard a full-bleed
            # background photo plus a title becomes a whole-slide candidate.
            c = Cluster(list(c.boxes))
        kept.append(c)
    kept.sort(key=lambda c: (-c.rect.area, c.rect.t, c.rect.l))
    return kept
