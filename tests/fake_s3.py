"""Minimal in-memory S3 client fake for strategy-config tests (no network)."""

from __future__ import annotations

import io


class FakeNoSuchKey(Exception):
    """Mimics botocore ClientError for a missing key (has ``.response``)."""

    def __init__(self, key: str, code: str = "NoSuchKey") -> None:
        super().__init__(f"{code}: {key}")
        self.response = {"Error": {"Code": code}}


class FakeS3:
    """Implements the client surface :mod:`ki_ops.strategies` uses.

    ``objects`` maps ``(bucket, key)`` → bytes. ``page_size`` forces
    ``list_objects_v2`` pagination so the continuation loop is exercised.
    """

    def __init__(self, objects=None, *, page_size: int = 1000) -> None:
        self.objects: dict[tuple[str, str], bytes] = dict(objects or {})
        self.page_size = page_size
        self.put_calls: list[tuple[str, str]] = []
        self.copy_calls: list[tuple[str, str]] = []

    def get_object(self, *, Bucket: str, Key: str):
        try:
            body = self.objects[(Bucket, Key)]
        except KeyError:
            raise FakeNoSuchKey(Key) from None
        return {"Body": io.BytesIO(body)}

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, **_kwargs):
        self.objects[(Bucket, Key)] = bytes(Body)
        self.put_calls.append((Bucket, Key))
        return {}

    def copy_object(self, *, Bucket: str, Key: str, CopySource: dict):
        src = (CopySource["Bucket"], CopySource["Key"])
        try:
            self.objects[(Bucket, Key)] = self.objects[src]
        except KeyError:
            raise FakeNoSuchKey(CopySource["Key"]) from None
        self.copy_calls.append((Bucket, Key))
        return {}

    def list_objects_v2(self, *, Bucket: str, Prefix: str, ContinuationToken=None):
        keys = sorted(k for b, k in self.objects if b == Bucket and k.startswith(Prefix))
        start = int(ContinuationToken) if ContinuationToken else 0
        page = keys[start : start + self.page_size]
        truncated = start + self.page_size < len(keys)
        resp = {
            "Contents": [{"Key": k} for k in page],
            "IsTruncated": truncated,
        }
        if truncated:
            resp["NextContinuationToken"] = str(start + self.page_size)
        return resp
