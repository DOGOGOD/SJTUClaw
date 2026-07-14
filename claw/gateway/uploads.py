"""Bounded streaming helpers shared by Gateway upload routes."""

from __future__ import annotations

from pathlib import Path

from fastapi import UploadFile


class UploadTooLargeError(ValueError):
    """Raised after an upload exceeds its route-specific byte limit."""


async def save_upload_limited(
    upload: UploadFile,
    destination: Path,
    *,
    max_bytes: int,
    chunk_bytes: int = 1024 * 1024,
) -> int:
    """Stream *upload* to disk and remove partial data on any failure."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    try:
        with destination.open("wb") as out:
            while True:
                chunk = await upload.read(chunk_bytes)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise UploadTooLargeError(
                        f"上传文件超过 {max_bytes} bytes 限制"
                    )
                out.write(chunk)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return total


__all__ = ["UploadTooLargeError", "save_upload_limited"]
