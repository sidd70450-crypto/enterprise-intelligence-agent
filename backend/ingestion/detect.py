"""Decide what kind of file an upload is from its content, never its name.

A file called ``report.pdf`` that is really an executable is rejected, and a
real PDF called ``notes.txt`` is treated as a PDF.
"""

import codecs
import zipfile
from pathlib import Path
from typing import Literal

FileKind = Literal["pdf", "docx", "text"]

_SNIFF_BYTES = 8192
# Limits for DOCX files, which are zip archives. A "zip bomb" is a small archive
# that expands to gigabytes; these checks read only the archive's directory.
MAX_ZIP_ENTRIES = 2000
MAX_ZIP_UNCOMPRESSED_BYTES = 200 * 1024 * 1024
MAX_ZIP_RATIO = 200


class UnsafeArchiveError(Exception):
    pass


def detect_kind(path: Path) -> FileKind | None:
    """The file's kind, or None if it is not a supported format."""
    with path.open("rb") as handle:
        head = handle.read(_SNIFF_BYTES)
    if not head:
        return None
    # The PDF spec allows the header anywhere in the first 1024 bytes.
    if b"%PDF-" in head[:1024]:
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        return "docx" if _is_docx(path) else None
    if _looks_like_utf8_text(head):
        return "text"
    return None


def _is_docx(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as archive:
            return "word/document.xml" in archive.namelist()
    except (zipfile.BadZipFile, OSError):
        return False


def _looks_like_utf8_text(head: bytes) -> bool:
    if b"\x00" in head:
        return False
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        # final=False: the sample may end in the middle of a multi-byte character.
        decoder.decode(head, final=False)
    except UnicodeDecodeError:
        return False
    return True


def check_zip_safety(path: Path) -> None:
    """Raise UnsafeArchiveError if the archive could expand to an unsafe size."""
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
    except zipfile.BadZipFile as exc:
        raise UnsafeArchiveError("The file is not a valid DOCX archive.") from exc
    if len(entries) > MAX_ZIP_ENTRIES:
        raise UnsafeArchiveError(f"The archive has more than {MAX_ZIP_ENTRIES} entries.")
    total = sum(entry.file_size for entry in entries)
    if total > MAX_ZIP_UNCOMPRESSED_BYTES:
        raise UnsafeArchiveError("The archive expands to more than 200 MB.")
    for entry in entries:
        if entry.compress_size and entry.file_size / entry.compress_size > MAX_ZIP_RATIO:
            raise UnsafeArchiveError("The archive's compression ratio is unsafe.")
