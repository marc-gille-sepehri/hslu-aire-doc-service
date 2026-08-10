"""LibreOffice headless orchestration — spec-media-extraction.md §4.3.

Two things here are load-bearing and easy to get wrong:

1. **A per-invocation profile directory.** Two concurrent `soffice` processes
   sharing the default `~/.config/libreoffice` deadlock or silently reuse each
   other's state. `-env:UserInstallation` gives each call its own.
2. **A hard timeout.** LibreOffice hangs rather than exits on malformed input
   often enough that a bare `subprocess.run` without a timeout will eventually
   wedge an instance until App Runner's health check kills it.

Deck → PDF runs **once per document**, in `prepare`. Reading §4.3 step 1 as once
per candidate is the difference between ten minutes and several hours.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

SOFFICE = os.environ.get("SOFFICE_BIN", "soffice")
CONVERT_TIMEOUT_S = int(os.environ.get("LO_CONVERT_TIMEOUT_S", "600"))


class LibreOfficeError(RuntimeError):
    pass


def _run(args: list[str], timeout: int) -> subprocess.CompletedProcess:
    profile = Path(tempfile.gettempdir()) / f"lo-{uuid.uuid4().hex}"
    cmd = [
        SOFFICE,
        "--headless", "--norestore", "--invisible", "--nolockcheck", "--nodefault",
        f"-env:UserInstallation=file://{profile}",
        *args,
    ]
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as e:
        raise LibreOfficeError(f"soffice timed out after {timeout}s") from e
    except FileNotFoundError as e:
        raise LibreOfficeError(
            f"{SOFFICE} not found — LibreOffice is not installed in this image"
        ) from e
    finally:
        shutil.rmtree(profile, ignore_errors=True)


def convert(source: Path, out_dir: Path, target: str, timeout: int = CONVERT_TIMEOUT_S) -> Path:
    """Convert `source` to `target` (e.g. "pdf", "svg") inside `out_dir`."""
    out_dir.mkdir(parents=True, exist_ok=True)
    result = _run(
        ["--convert-to", target, "--outdir", str(out_dir), str(source)],
        timeout=timeout,
    )
    produced = out_dir / f"{source.stem}.{target.split(':')[0]}"
    if not produced.exists():
        stderr = result.stderr.decode("utf-8", "replace")[:500]
        raise LibreOfficeError(
            f"conversion to {target} produced no output (exit {result.returncode}): {stderr}"
        )
    return produced


def deck_to_pdf(deck: Path, out_dir: Path) -> Path:
    return convert(deck, out_dir, "pdf")


def check_fonts(required: set[str]) -> set[str]:
    """Return the subset of `required` that the host cannot supply.

    §4.3 wants this before rendering, because a substituted font shifts metrics
    and overflows labels — silently. The result feeds the per-candidate
    `font_substituted` flag rather than failing the job: failing outright would
    yield zero assets from a 120-slide deck over one exotic typeface, which
    contradicts §1.
    """
    try:
        out = subprocess.run(["fc-list", "--format", "%{family}\\n"],
                             capture_output=True, timeout=30, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return set()          # cannot tell — do not claim fonts are missing
    available = set()
    for line in out.stdout.decode("utf-8", "replace").splitlines():
        for family in line.split(","):
            available.add(family.strip().casefold())
    return {f for f in required if f.casefold() not in available}
