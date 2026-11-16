"""Where uploaded files live on disk.

Files are stored as ``<root>/<org_id>/<document_id>``. The user's filename is
never part of the path, which rules out path traversal (``../../etc/passwd``)
and name collisions by construction.
"""

import asyncio
import hashlib
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path


class FileTooLargeError(Exception):
    pass


@dataclass(frozen=True)
class StoredUpload:
    path: Path
    size_bytes: int
    sha256: str


class FileStorage:
    def __init__(self, root: Path) -> None:
        self.root = root

    def path_for(self, org_id: uuid.UUID, document_id: uuid.UUID) -> Path:
        return self.root / str(org_id) / str(document_id)

    async def save_stream(
        self, chunks: AsyncIterator[bytes], org_id: uuid.UUID, max_bytes: int
    ) -> StoredUpload:
        """Write an upload to a temporary file, hashing it and enforcing the size limit."""
        directory = self.root / str(org_id)
        await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
        temporary = directory / f".upload-{uuid.uuid4().hex}"
        digest, size = hashlib.sha256(), 0
        try:
            with temporary.open("xb") as handle:
                async for chunk in chunks:
                    size += len(chunk)
                    if size > max_bytes:
                        raise FileTooLargeError
                    digest.update(chunk)
                    await asyncio.to_thread(handle.write, chunk)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return StoredUpload(temporary, size, digest.hexdigest())

    async def commit(self, upload: StoredUpload, org_id: uuid.UUID, document_id: uuid.UUID) -> Path:
        """Move a saved upload to its final, ID-based location."""
        final = self.path_for(org_id, document_id)
        await asyncio.to_thread(os.replace, upload.path, final)
        return final

    async def delete(self, path: Path) -> None:
        await asyncio.to_thread(path.unlink, missing_ok=True)
