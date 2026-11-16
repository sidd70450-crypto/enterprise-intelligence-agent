"""Builders for test documents, so no binary fixtures are committed."""

import zipfile
from pathlib import Path

import docx
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas


def make_pdf(path: Path, pages: list[str]) -> Path:
    pdf = canvas.Canvas(str(path), pagesize=A4)
    for text in pages:
        y = 800
        for line in text.split("\n"):
            pdf.drawString(50, y, line)
            y -= 14
        pdf.showPage()
    pdf.save()
    return path


def make_docx(path: Path, paragraphs: list[str], table: list[list[str]] | None = None) -> Path:
    document = docx.Document()
    for paragraph in paragraphs:
        document.add_paragraph(paragraph)
    if table:
        grid = document.add_table(rows=len(table), cols=len(table[0]))
        for r, row in enumerate(table):
            for c, value in enumerate(row):
                grid.cell(r, c).text = value
    document.save(str(path))
    return path


def make_zip_bomb(path: Path) -> Path:
    """A DOCX-shaped archive whose single entry expands about 1000-fold."""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", b"\0" * (50 * 1024 * 1024))
    return path
