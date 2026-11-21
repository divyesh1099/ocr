"""S3 streaming helpers: random-access reads (for huge zips) and multipart streaming writes."""
from __future__ import annotations

import io
import os
from collections.abc import Iterator

from . import aws


class S3SeekableReader(io.RawIOBase):
    """A read-only, seekable file object backed by S3 ranged GETs.

    Lets Python's zipfile read the central directory and individual members of a
    multi-gigabyte archive without downloading the whole thing.
    """

    def __init__(self, bucket: str, key: str, size: int | None = None, block: int = 8 * 1024 * 1024):
        super().__init__()
        self.bucket, self.key = bucket, key
        self.s3 = aws.client("s3")
        self.size = size if size is not None else self.s3.head_object(Bucket=bucket, Key=key)["ContentLength"]
        self.pos = 0
        self.block = block
        self._buf_start = -1
        self._buf = b""

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            self.pos = offset
        elif whence == io.SEEK_CUR:
            self.pos += offset
        elif whence == io.SEEK_END:
            self.pos = self.size + offset
        self.pos = max(0, min(self.pos, self.size))
        return self.pos

    def _fetch(self, start: int, length: int) -> bytes:
        end = min(start + length, self.size) - 1
        if end < start:
            return b""
        resp = self.s3.get_object(Bucket=self.bucket, Key=self.key, Range=f"bytes={start}-{end}")
        return resp["Body"].read()

    def read(self, n: int = -1) -> bytes:
        if self.pos >= self.size:
            return b""
        if n is None or n < 0:
            n = self.size - self.pos
        n = min(n, self.size - self.pos)
        # Serve from buffer when possible
        if self._buf_start <= self.pos and self.pos + n <= self._buf_start + len(self._buf):
            off = self.pos - self._buf_start
            data = self._buf[off: off + n]
        elif n >= self.block:
            data = self._fetch(self.pos, n)
        else:
            self._buf_start = self.pos
            self._buf = self._fetch(self.pos, self.block)
            data = self._buf[:n]
        self.pos += len(data)
        return data

    def readinto(self, b) -> int:  # type: ignore[override]
        data = self.read(len(b))
        b[: len(data)] = data
        return len(data)


class S3MultipartWriter(io.RawIOBase):
    """Write-only stream that uploads to S3 in parts (for zips and merged outputs of any size)."""

    def __init__(self, bucket: str, key: str, content_type: str = "application/octet-stream",
                 part_size: int = 64 * 1024 * 1024):
        super().__init__()
        self.bucket, self.key, self.part_size = bucket, key, part_size
        self.s3 = aws.client("s3")
        self.upload_id = self.s3.create_multipart_upload(Bucket=bucket, Key=key, ContentType=content_type)["UploadId"]
        self.parts: list[dict] = []
        self.buf = bytearray()
        self.written = 0
        self._done = False

    def writable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.written

    def write(self, b) -> int:  # type: ignore[override]
        self.buf.extend(b)
        self.written += len(b)
        while len(self.buf) >= self.part_size:
            self._upload(bytes(self.buf[: self.part_size]))
            del self.buf[: self.part_size]
        return len(b)

    def _upload(self, data: bytes) -> None:
        n = len(self.parts) + 1
        resp = self.s3.upload_part(Bucket=self.bucket, Key=self.key, UploadId=self.upload_id, PartNumber=n, Body=data)
        self.parts.append({"PartNumber": n, "ETag": resp["ETag"]})

    def close(self) -> None:
        if self._done:
            return
        self._done = True
        try:
            if self.buf or not self.parts:
                self._upload(bytes(self.buf))
                self.buf.clear()
            self.s3.complete_multipart_upload(
                Bucket=self.bucket, Key=self.key, UploadId=self.upload_id, MultipartUpload={"Parts": self.parts}
            )
        except Exception:
            self.abort()
            raise
        finally:
            super().close()

    def abort(self) -> None:
        try:
            self.s3.abort_multipart_upload(Bucket=self.bucket, Key=self.key, UploadId=self.upload_id)
        except Exception:  # noqa: BLE001
            pass


def list_keys(bucket: str, prefix: str, token: str | None = None,
              max_keys: int = 1000) -> tuple[list[dict], str | None]:
    kwargs = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": max_keys}
    if token:
        kwargs["ContinuationToken"] = token
    resp = aws.client("s3").list_objects_v2(**kwargs)
    return resp.get("Contents", []), resp.get("NextContinuationToken")


def iter_keys(bucket: str, prefix: str) -> Iterator[dict]:
    token = None
    while True:
        items, token = list_keys(bucket, prefix, token)
        yield from items
        if not token:
            return


def download(bucket: str, key: str, path: str) -> str:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    aws.client("s3").download_file(bucket, key, path)
    return path


def upload(bucket: str, key: str, path: str, content_type: str | None = None) -> None:
    extra = {"ContentType": content_type} if content_type else None
    aws.client("s3").upload_file(path, bucket, key, ExtraArgs=extra)


def put_bytes(bucket: str, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
    aws.client("s3").put_object(Bucket=bucket, Key=key, Body=data, ContentType=content_type)


def get_bytes(bucket: str, key: str) -> bytes:
    return aws.client("s3").get_object(Bucket=bucket, Key=key)["Body"].read()


def exists(bucket: str, key: str) -> bool:
    from botocore.exceptions import ClientError

    try:
        aws.client("s3").head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return False
        raise
