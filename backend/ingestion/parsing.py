"""Extract text from PDF, DOCX and plain-text files.

These functions run inside the sandboxed parser subprocess (see sandbox.py),
never in the API or worker process, because parsing libraries can crash, hang
or exhaust memory on hostile files.

Every failure is a ``ParseError`` with a stable ``code`` that is stored on the
document and shown to the user.
"""

import unicodedata
from dataclasses import dataclass
from pathlib import Path

from backend.ingestion.detect import FileKind, UnsafeArchiveError, check_zip_safety

# A PDF averaging fewer characters than this per page is almost certainly
# scanned images, which need OCR (not supported yet).
MIN_CHARS_PER_PAGE = 20


@dataclass(frozen=True)
class Page:
    number: int | None  # 1-based for PDFs; None for formats without pages
    text: str


class ParseError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def normalize_text(text: str) -> str:
    """Clean extracted text without changing what it says.

    NFKC turns ligatures and full-width forms into plain characters ("ﬁ" -> "fi"),
    control characters (including NUL, which PostgreSQL rejects) are removed,
    words hyphenated across a line break are rejoined, and runs of spaces and
    blank lines are collapsed.
    """
    text = unicodedata.normalize("NFKC", text).replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(ch for ch in text if ch in "\n\t" or unicodedata.category(ch)[0] != "C")
    lines: list[str] = []
    for raw in text.split("\n"):
        line = " ".join(raw.replace("\t", " ").split())
        if lines and lines[-1].endswith("-") and line[:1].islower() and lines[-1][-2:-1].isalpha():
            lines[-1] = lines[-1][:-1] + line  # "inter-\nnational" -> "international"
            continue
        lines.append(line)
    out: list[str] = []
    blank = 0
    for line in lines:
        blank = blank + 1 if not line else 0
        if blank <= 1:
            out.append(line)
    return "\n".join(out).strip()


def parse_file(path: Path, kind: FileKind, max_pdf_pages: int) -> list[Page]:
    if kind == "pdf":
        pages = _parse_pdf(path, max_pdf_pages)
    elif kind == "docx":
        pages = _parse_docx(path)
    else:
        pages = _parse_text(path)

    pages = [Page(p.number, normalize_text(p.text)) for p in pages]
    total = sum(len(p.text) for p in pages)
    if total == 0:
        raise ParseError("no_text", "The file contains no extractable text.")
    if kind == "pdf" and total < MIN_CHARS_PER_PAGE * len(pages):
        raise ParseError(
            "no_text",
            "Almost no text could be extracted; the PDF may be scanned images. "
            "Scanned documents are not supported yet.",
        )
    return pages


def _parse_pdf(path: Path, max_pages: int) -> list[Page]:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(path, strict=False)
        if reader.is_encrypted and not reader.decrypt(""):
            raise ParseError("encrypted", "The PDF is password-protected.")
        count = len(reader.pages)
        if count > max_pages:
            raise ParseError(
                "too_many_pages", f"The PDF has {count} pages; the limit is {max_pages}."
            )
        return [Page(i + 1, page.extract_text() or "") for i, page in enumerate(reader.pages)]
    except ParseError:
        raise
    except (PdfReadError, ValueError, KeyError, TypeError, AttributeError, IndexError) as exc:
        raise ParseError("corrupt", f"The PDF could not be read ({type(exc).__name__}).") from exc
    except RecursionError as exc:
        raise ParseError("corrupt", "The PDF's structure is too deeply nested.") from exc


def _parse_docx(path: Path) -> list[Page]:
    try:
        check_zip_safety(path)
    except UnsafeArchiveError as exc:
        raise ParseError("unsafe_archive", str(exc)) from exc

    import docx
    from docx.opc.exceptions import PackageNotFoundError

    try:
        document = docx.Document(str(path))
    except (PackageNotFoundError, KeyError, ValueError) as exc:
        raise ParseError("corrupt", f"The DOCX could not be read ({type(exc).__name__}).") from exc

    parts = [paragraph.text for paragraph in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            parts.append(" | ".join(cell.text for cell in row.cells))
    return [Page(None, "\n\n".join(part for part in parts if part.strip()))]


def _parse_text(path: Path) -> list[Page]:
    try:
        text = path.read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ParseError("not_utf8", "Text files must be UTF-8 encoded.") from exc
    return [Page(None, text)]
