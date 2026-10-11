"""Applicant document storage: files on disk in uploads/<application_id>/, metadata in the database.

Accepted: PDF, PNG and JPG, up to 10 MB each. PDFs are rendered to page images with pypdfium2
before they are sent to the model. The SHA-256 stored with each document proves later that the
file was not changed after review.
"""
import hashlib
import io
import os
from pathlib import Path

from sqlalchemy.orm import Session

from db import PROJECT_ROOT, Document

MAX_BYTES = 10 * 1024 * 1024
DOC_TYPES = ("pay_stub", "bank_statement")
RENDER_SCALE = 2.0  # 144 dpi: small print stays readable for the vision model
# File signatures, so the type is checked from the content, not trusted from the file name.
SIGNATURES = {
    b"%PDF-": ("application/pdf", ".pdf"),
    b"\x89PNG\r\n\x1a\n": ("image/png", ".png"),
    b"\xff\xd8\xff": ("image/jpeg", ".jpg"),
}


class DocumentError(ValueError):
    """The file can't be accepted (wrong type, too large, unreadable)."""


def uploads_root() -> Path:
    """uploads/ in the project, or UPLOADS_DIR (tests point this at a scratch folder)."""
    root = Path(os.getenv("UPLOADS_DIR") or PROJECT_ROOT / "uploads")
    return root if root.is_absolute() else PROJECT_ROOT / root


def detect_type(data: bytes) -> tuple[str, str]:
    for magic, kind in SIGNATURES.items():
        if data.startswith(magic):
            return kind
    raise DocumentError("Only PDF, PNG and JPG files are accepted.")


def pdf_page_count(data: bytes) -> int:
    import pypdfium2 as pdfium

    try:
        pdf = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as exc:
        raise DocumentError(f"The PDF could not be read: {exc}") from exc
    try:
        return len(pdf)
    finally:
        pdf.close()


def save_document(session: Session, application_id: int, doc_type: str, filename: str,
                  data: bytes) -> Document:
    """Validate and store one file, then add its Document row to the session (caller commits)."""
    if doc_type not in DOC_TYPES:
        raise DocumentError(f"Unknown document type {doc_type!r}.")
    if not data:
        raise DocumentError("The file is empty.")
    if len(data) > MAX_BYTES:
        raise DocumentError(f"The file is {len(data) / 1e6:.1f} MB; the limit is 10 MB.")
    mime, ext = detect_type(data)
    pages = pdf_page_count(data) if mime == "application/pdf" else 1
    digest = hashlib.sha256(data).hexdigest()

    folder = uploads_root() / str(application_id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{doc_type}-{digest[:12]}{ext}"
    path.write_bytes(data)

    try:
        stored = path.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:  # UPLOADS_DIR outside the project (tests)
        stored = str(path)
    doc = Document(application_id=application_id, doc_type=doc_type, storage_path=stored,
                   original_filename=Path(filename).name[:300], mime_type=mime,
                   size_bytes=len(data), sha256=digest, page_count=pages)
    session.add(doc)
    return doc


def file_path(doc: Document) -> Path:
    path = Path(doc.storage_path)
    return path if path.is_absolute() else PROJECT_ROOT / path


def read_bytes(doc: Document) -> bytes:
    return file_path(doc).read_bytes()


def hash_matches(doc: Document) -> bool:
    """True if the file on disk still has the hash recorded at upload."""
    try:
        return hashlib.sha256(read_bytes(doc)).hexdigest() == doc.sha256
    except FileNotFoundError:
        return False


def page_images(doc: Document) -> list[bytes]:
    """PNG bytes for each page: PDFs are rendered, images are passed through."""
    data = read_bytes(doc)
    if doc.mime_type != "application/pdf":
        return [data]
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(data)
    try:
        images = []
        for page in pdf:
            buf = io.BytesIO()
            page.render(scale=RENDER_SCALE).to_pil().save(buf, format="PNG")
            images.append(buf.getvalue())
        return images
    finally:
        pdf.close()
