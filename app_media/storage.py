"""Content-addressed blob storage — spec-media-extraction.md §6.

Blobs are keyed by the SHA-256 of **what was stored**, not of the source: the
sanitised bytes for an SVG, the post-crop post-EXIF-strip bytes for a raster.
Hashing the source and then transforming it produces a key that identifies
nothing you can serve.

Metadata deliberately does not live here. §6 explains why: the same figure in two
decks yields one hash, and a `meta.json` under that hash could hold only one
provenance. Two assets, one blob, two provenances is the truth, and only the
portal's index can express it.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass

import boto3
from botocore.exceptions import ClientError

BUCKET = os.environ.get("MEDIA_BUCKET", "")
PREFIX = os.environ.get("MEDIA_PREFIX", "media")
IMMUTABLE = "public, max-age=31536000, immutable"

_client = None


def s3():
    global _client
    if _client is None:
        _client = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "eu-central-1"))
    return _client


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def blob_key(digest: str, name: str) -> str:
    return f"{PREFIX}/blobs/{digest[:2]}/{digest}/{name}"


def slide_key(source_digest: str, slide: int) -> str:
    return f"{PREFIX}/slides/{source_digest}/{slide}.png"


def work_key(job_id: str, name: str) -> str:
    return f"{PREFIX}/work/{job_id}/{name}"


@dataclass
class PutResult:
    key: str
    sha256: str
    bytes: int
    existed: bool


def put_blob(data: bytes, name: str, content_type: str) -> PutResult:
    """Write once. An existing object with the same key is the same bytes by
    construction, so a re-ingest is a HEAD and nothing more (§11.5)."""
    digest = sha256_of(data)
    key = blob_key(digest, name)
    if _exists(key):
        return PutResult(key, digest, len(data), existed=True)
    s3().put_object(
        Bucket=BUCKET, Key=key, Body=data,
        ContentType=content_type, CacheControl=IMMUTABLE,
    )
    return PutResult(key, digest, len(data), existed=False)


def put_object(key: str, data: bytes, content_type: str, cache_control: str | None = None) -> str:
    """For non-blob objects — slide renders, job working files."""
    kwargs = {"Bucket": BUCKET, "Key": key, "Body": data, "ContentType": content_type}
    if cache_control:
        kwargs["CacheControl"] = cache_control
    s3().put_object(**kwargs)
    return key


def get_object(key: str) -> bytes:
    return s3().get_object(Bucket=BUCKET, Key=key)["Body"].read()


def delete_prefix(prefix: str) -> int:
    """Clean up `media/work/<jobId>/` when a job ends. Blobs are never deleted."""
    paginator = s3().get_paginator("list_objects_v2")
    removed = 0
    for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix):
        keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
        if keys:
            s3().delete_objects(Bucket=BUCKET, Delete={"Objects": keys})
            removed += len(keys)
    return removed


def _exists(key: str) -> bool:
    try:
        s3().head_object(Bucket=BUCKET, Key=key)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return False
        raise
