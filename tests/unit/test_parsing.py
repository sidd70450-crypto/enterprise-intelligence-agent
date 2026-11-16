from pathlib import Path

import pytest

from backend.ingestion.detect import detect_kind
from backend.ingestion.parsing import ParseError, normalize_text, parse_file
from backend.ingestion.sandbox import parse_in_sandbox
from tests.files import make_docx, make_pdf, make_zip_bomb


def test_normalize_text() -> None:
    raw = "The ﬁnal  report\r\ncovers inter-\nnational\x00 sales.\n\n\n\nNext   section"
    assert normalize_text(raw) == "The final report\ncovers international sales.\n\nNext section"


def test_normalize_keeps_real_hyphens() -> None:
    assert normalize_text("A well-\nKnown case") == "A well-\nKnown case"


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (b"%PDF-1.7\n...", "pdf"),
        (b"# Heading\n\nSome *markdown* text.", "text"),
        ("Résumé, naïve, 日本語".encode(), "text"),
        (b"MZ\x90\x00\x03\x00\x00\x00", None),  # a Windows executable
        (b"\xff\xfe\x00\x00binary", None),
        (b"", None),
        (b"PK\x03\x04not a real zip", None),
    ],
)
def test_detect_kind_uses_content(tmp_path: Path, content: bytes, expected: str | None) -> None:
    path = tmp_path / "upload.pdf"  # the extension is deliberately misleading
    path.write_bytes(content)
    assert detect_kind(path) == expected


def test_detects_docx(tmp_path: Path) -> None:
    assert detect_kind(make_docx(tmp_path / "notes.txt", ["hello"])) == "docx"


def test_pdf_pages_are_numbered(tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path / "a.pdf", ["First page about revenue.", "Second page about costs."])
    pages = parse_file(pdf, "pdf", max_pdf_pages=10)
    assert [p.number for p in pages] == [1, 2]
    assert "revenue" in pages[0].text and "costs" in pages[1].text


def test_pdf_page_limit(tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path / "a.pdf", ["page"] * 5)
    with pytest.raises(ParseError) as error:
        parse_file(pdf, "pdf", max_pdf_pages=3)
    assert error.value.code == "too_many_pages"


def test_corrupt_pdf_fails_with_a_reason(tmp_path: Path) -> None:
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"%PDF-1.7\n1 0 obj << /Type /Catalog /Pages 2 0 R >> garbage")
    with pytest.raises(ParseError) as error:
        parse_file(path, "pdf", max_pdf_pages=10)
    assert error.value.code in {"corrupt", "no_text"}
    assert error.value.detail


def test_pdf_without_text_is_reported_as_scanned(tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path / "blank.pdf", ["", "", ""])
    with pytest.raises(ParseError) as error:
        parse_file(pdf, "pdf", max_pdf_pages=10)
    assert error.value.code == "no_text"


def test_docx_paragraphs_and_tables(tmp_path: Path) -> None:
    path = make_docx(
        tmp_path / "a.docx",
        ["Introduction paragraph.", "Second paragraph."],
        table=[["Region", "Revenue"], ["EMEA", "120"]],
    )
    [page] = parse_file(path, "docx", max_pdf_pages=10)
    assert page.number is None
    assert "Introduction paragraph." in page.text
    assert "EMEA | 120" in page.text


def test_zip_bomb_is_refused_before_extraction(tmp_path: Path) -> None:
    with pytest.raises(ParseError) as error:
        parse_file(make_zip_bomb(tmp_path / "bomb.docx"), "docx", max_pdf_pages=10)
    assert error.value.code == "unsafe_archive"


def test_text_must_be_utf8(tmp_path: Path) -> None:
    path = tmp_path / "latin1.txt"
    path.write_bytes("café".encode("latin-1"))
    with pytest.raises(ParseError) as error:
        parse_file(path, "text", max_pdf_pages=10)
    assert error.value.code == "not_utf8"


async def test_sandbox_returns_pages(tmp_path: Path) -> None:
    pdf = make_pdf(
        tmp_path / "a.pdf",
        ["Sandboxed page one has a sentence.", "Sandboxed page two has another."],
    )
    pages = await parse_in_sandbox(pdf, "pdf", max_pdf_pages=10, timeout_seconds=30, memory_mb=1024)
    assert [p.number for p in pages] == [1, 2]


async def test_sandbox_reports_parse_errors(tmp_path: Path) -> None:
    bomb = make_zip_bomb(tmp_path / "bomb.docx")
    with pytest.raises(ParseError) as error:
        await parse_in_sandbox(bomb, "docx", max_pdf_pages=10, timeout_seconds=30, memory_mb=1024)
    assert error.value.code == "unsafe_archive"


async def test_sandbox_kills_a_parser_that_hangs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Stand in for a pathological file by making the child run an endless loop.
    monkeypatch.setattr(
        "backend.ingestion.sandbox.sys.executable",
        "/bin/sh",
    )
    monkeypatch.setattr(
        "backend.ingestion.sandbox._worker_command",
        lambda: ["-c", "while :; do :; done"],
    )
    with pytest.raises(ParseError) as error:
        await parse_in_sandbox(
            tmp_path / "x", "pdf", max_pdf_pages=10, timeout_seconds=0.5, memory_mb=1024
        )
    assert error.value.code == "timeout"


async def test_sandbox_child_does_not_see_application_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.ingestion.sandbox import _child_environment

    monkeypatch.setenv("EIA_JWT_SECRET", "must-not-leak")
    monkeypatch.setenv("EIA_DATABASE_URL", "postgresql://secret")
    env = _child_environment()
    assert not any(key.startswith("EIA_") for key in env)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("../../etc/passwd", "passwd"),
        ("C:\\Users\\ada\\report.pdf", "report.pdf"),
        ("bell\x07and\x00null.txt", "bellandnull.txt"),
        ("  spaced   out  name.md ", "spaced out name.md"),
        ("...", "upload"),
        (None, "upload"),
        ("a" * 400 + ".pdf", "a" * 255),
    ],
)
def test_safe_filename(raw: str | None, expected: str) -> None:
    from backend.api.documents import safe_filename

    assert safe_filename(raw) == expected


async def test_sandbox_stops_a_parser_that_exhausts_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "backend.ingestion.sandbox._worker_command",
        lambda: ["-c", "bytearray(2 * 1024 ** 3)"],
    )
    with pytest.raises(ParseError) as error:
        await parse_in_sandbox(
            tmp_path / "x", "pdf", max_pdf_pages=10, timeout_seconds=30, memory_mb=256
        )
    assert error.value.code == "parser_failed"


def test_hashing_embedder_is_deterministic_normalised_and_word_based() -> None:
    from backend.ingestion.embedding import HashingEmbedder

    embedder = HashingEmbedder()
    first = embedder.embed_query("Quarterly revenue grew")
    assert first == embedder.embed_query("quarterly REVENUE grew")
    assert len(first) == 384
    assert abs(sum(v * v for v in first) - 1.0) < 1e-9

    def cosine(a: list[float], b: list[float]) -> float:
        return sum(x * y for x, y in zip(a, b, strict=True))

    [shares, unrelated] = embedder.embed_passages(["revenue grew strongly", "the cat sat"])
    assert cosine(first, shares) > cosine(first, unrelated)
