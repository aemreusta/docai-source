from __future__ import annotations

import os
import posixpath
from pathlib import Path
from typing import Protocol


class Storage(Protocol):
    def get(self, key: str) -> bytes: ...
    def put(self, key: str, data: bytes, content_type: str) -> None: ...


def sibling(key: str, name: str) -> str:
    return posixpath.join(posixpath.dirname(key), name)


class LocalStorage:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def _path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if self.root not in p.parents:
            raise ValueError("key escapes storage root")
        return p

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def put(self, key: str, data: bytes, content_type: str) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


class S3Storage:
    def __init__(self) -> None:
        import boto3

        self.bucket = os.environ["S3_BUCKET"]
        self.s3 = boto3.client(
            "s3",
            endpoint_url=os.environ.get("S3_ENDPOINT") or None,
            region_name=os.environ.get("S3_REGION", "auto"),
            aws_access_key_id=os.environ.get("S3_ACCESS_KEY_ID"),
            aws_secret_access_key=os.environ.get("S3_SECRET_ACCESS_KEY"),
        )

    def get(self, key: str) -> bytes:
        return self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)


def from_env() -> Storage:
    if os.environ.get("S3_BUCKET"):
        return S3Storage()
    return LocalStorage(os.environ.get("DOCAI_LOCAL_ROOT", "/tmp/memoris-docai"))
