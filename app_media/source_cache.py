"""Per-instance disk cache for source documents.

The orchestrator calls `/render` once per slide (spec §2.1). Each call needs the
deck, and each call would otherwise pull it out of S3 again: over a 120-slide
deck of 285 MB that is thirty gigabytes of transfer and several seconds of dead
time per slide to convey a file that cannot have changed — the key is written
once, and the job holds it for its whole run.

So keep it on disk between calls. The cache is deliberately small and dumb:

* keyed by the S3 key **and** its ETag, so a re-uploaded key is a miss rather
  than a stale hit;
* bounded by entry count, evicting least-recently-used, because the failure mode
  that matters is filling the instance's ephemeral disk, not a low hit rate;
* written to a staging name and moved with `os.replace`, so a second request
  arriving mid-download either waits on the lock or sees a complete file, never
  a partial one.

The SHA-256 is cached next to the file for the same reason: hashing 285 MB per
call is a second of CPU spent re-deriving a constant.

It is a cache, not storage — losing it costs one download.
"""
from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path

from . import storage

CACHE_DIR = Path(os.environ.get("MEDIA_SOURCE_CACHE", "/tmp/media-src"))
MAX_ENTRIES = int(os.environ.get("MEDIA_SOURCE_CACHE_ENTRIES", "2"))


class SourceTooLarge(Exception):
    def __init__(self, size: int, limit: int):
        super().__init__(f"source is {size} bytes, over the {limit} byte limit")
        self.size = size
        self.limit = limit


# One lock per cache entry: two concurrent renders of different slides of the
# same deck must not both download it. A single global lock would serialise
# unrelated jobs, so the map is keyed the way the cache is.
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(name: str) -> threading.Lock:
    with _locks_guard:
        lock = _locks.get(name)
        if lock is None:
            lock = threading.Lock()
            _locks[name] = lock
        return lock


def _entry_name(key: str, etag: str) -> str:
    # Including the key as well as the ETag is not needed for correctness — two
    # keys with one ETag are the same bytes — but it keeps the file recognisable
    # when someone shells into the instance.
    tag = etag.replace('"', "")
    safe = key.replace("/", "_")[-80:]
    return f"{tag}-{safe}"


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _evict(keep: Path) -> None:
    """Keep at most MAX_ENTRIES files, dropping the least recently used."""
    try:
        entries = sorted(
            (p for p in CACHE_DIR.iterdir() if p.is_file() and p.suffix != ".sha256"),
            key=lambda p: p.stat().st_atime,
        )
    except OSError:
        return
    for old in entries[: max(0, len(entries) - MAX_ENTRIES)]:
        if old == keep:
            continue
        for victim in (old, old.with_suffix(old.suffix + ".sha256")):
            try:
                victim.unlink()
            except OSError:
                pass  # a concurrent request may have removed it already


def fetch(key: str, max_bytes: int) -> tuple[Path, str, int]:
    """Return (local path, sha256, size) for the object at `key`.

    The path is owned by the cache: read it, do not delete or modify it. Raises
    SourceTooLarge before transferring anything if the object exceeds the limit.
    """
    head = storage.s3().head_object(Bucket=storage.BUCKET, Key=key)
    size = int(head["ContentLength"])
    if size > max_bytes:
        raise SourceTooLarge(size, max_bytes)

    path = CACHE_DIR / _entry_name(key, head["ETag"])
    sha_path = path.with_suffix(path.suffix + ".sha256")

    with _lock_for(path.name):
        if path.exists() and path.stat().st_size == size and sha_path.exists():
            os.utime(path)  # mark recently used, for the LRU above
            return path, sha_path.read_text().strip(), size

        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        staging = path.with_suffix(path.suffix + f".part-{os.getpid()}")
        try:
            storage.download_to(key, staging)
            digest = _sha256_of(staging)
            os.replace(staging, path)
            sha_path.write_text(digest)
        finally:
            staging.unlink(missing_ok=True)
        _evict(keep=path)
    return path, digest, size
