"""
documents.py — Extract text from uploaded documents for chat context.

The chat send flow is JSON (base64 for images), so documents follow the same
"process on the server, send content in the request" shape: the browser uploads
a file here, we extract plain text, and it POSTs that text back with the next
message (which the send flow injects into the model payload).

Raw binaries are never stored, but the extracted text now is: the chat send flow
writes every attachment to falcon.documents_store and records its storage id, so
a manuscript uploaded today is readable in a session next week. Passing
identity_id here stores it at upload time as well. Before this existed the text
survived exactly one turn, which is why documents appeared to vanish between
messages.

Supported: PDF, Word (.docx), Excel (.xlsx/.xlsm), PowerPoint (.pptx), and any
UTF-8 text format (csv, tsv, txt, md, json, xml, html, yaml, log). Legacy binary
.doc/.xls are rejected with a hint to re-save as the modern format.
"""
from __future__ import annotations

import io
import logging

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

logger = logging.getLogger("falcon.documents")

router = APIRouter(prefix="/documents", tags=["documents"])

MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB per file
MAX_TEXT_CHARS = 200_000  # cap extracted text so one file can't blow up context

# Text-like formats we decode directly.
TEXT_EXTS = {
    "txt", "md", "markdown", "csv", "tsv", "json", "xml", "html", "htm",
    "yaml", "yml", "log", "ini", "cfg", "rtf", "tex",
}


def _ext(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _extract_pdf(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    if getattr(reader, "is_encrypted", False):
        try:
            reader.decrypt("")  # try empty password
        except Exception:  # noqa: BLE001
            pass
    parts = []
    for i, page in enumerate(reader.pages, 1):
        try:
            txt = page.extract_text() or ""
        except Exception:  # noqa: BLE001
            txt = ""
        if txt.strip():
            parts.append(txt)
    return "\n\n".join(parts)


def _extract_docx(data: bytes) -> str:
    import docx  # python-docx

    d = docx.Document(io.BytesIO(data))
    parts = [p.text for p in d.paragraphs if p.text and p.text.strip()]
    for table in d.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                parts.append("\t".join(cells))
    return "\n".join(parts)


def _extract_xlsx(data: bytes) -> str:
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    parts: list[str] = []
    for ws in wb.worksheets:
        parts.append(f"# Sheet: {ws.title}")
        for row in ws.iter_rows(values_only=True):
            cells = ["" if c is None else str(c) for c in row]
            if any(c.strip() for c in cells):
                parts.append("\t".join(cells))
    try:
        wb.close()
    except Exception:  # noqa: BLE001
        pass
    return "\n".join(parts)


def _extract_pptx(data: bytes) -> str:
    from pptx import Presentation

    prs = Presentation(io.BytesIO(data))
    parts: list[str] = []
    for i, slide in enumerate(prs.slides, 1):
        parts.append(f"# Slide {i}")
        for shape in slide.shapes:
            if shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    line = "".join(run.text for run in para.runs)
                    if line.strip():
                        parts.append(line)
    return "\n".join(parts)


def _extract_text_bytes(data: bytes) -> str:
    for enc in ("utf-8", "utf-16", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _extract(name: str, ext: str, data: bytes) -> str:
    if ext == "pdf":
        return _extract_pdf(data)
    if ext == "docx":
        return _extract_docx(data)
    if ext in ("xlsx", "xlsm"):
        return _extract_xlsx(data)
    if ext == "pptx":
        return _extract_pptx(data)
    if ext in TEXT_EXTS:
        return _extract_text_bytes(data)
    if ext in ("doc", "xls", "ppt"):
        raise HTTPException(
            status_code=415,
            detail=f"Legacy .{ext} files aren't supported — re-save as .{ext}x and try again.",
        )
    # Last resort: attempt a text decode for unknown extensions.
    text = _extract_text_bytes(data)
    if "\x00" in text[:1000]:  # looks binary
        raise HTTPException(status_code=415, detail=f"Unsupported file type: .{ext or '?'}")
    return text


@router.get("/stored")
async def list_stored(identity_id: str = "", q: str = "", limit: int = 50) -> dict:
    """Documents durably stored for an identity, without their text."""
    from falcon import documents_store as Store

    docs = Store.search(identity_id, q, limit) if q else Store.list_documents(identity_id, limit)
    return {"documents": docs, "count": len(docs)}


@router.get("/stored/{storage_id}")
async def get_stored(storage_id: str, identity_id: str = "") -> dict:
    """One stored document, including its full text."""
    from falcon import documents_store as Store

    doc = Store.get(storage_id, identity_id)
    if not doc:
        raise HTTPException(status_code=404, detail=f"No stored document {storage_id!r}.")
    return doc


@router.delete("/stored/{storage_id}")
async def delete_stored(storage_id: str, identity_id: str = "") -> dict:
    """Delete one stored document. The only thing that removes stored content."""
    from falcon import documents_store as Store

    if not Store.delete(storage_id, identity_id):
        raise HTTPException(status_code=404, detail=f"No stored document {storage_id!r}.")
    return {"deleted": True, "storage_id": storage_id}


@router.post("/extract")
async def extract_document(
    file: UploadFile = File(...),
    identity_id: str = Form(""),
) -> dict:
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File is larger than 25 MB.")

    name = file.filename or "file"
    ext = _ext(name)
    try:
        text = _extract(name, ext, data)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("extraction failed for %s: %s", name, exc)
        raise HTTPException(status_code=422, detail=f"Could not read {name}: {exc}")

    text = (text or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail=f"No extractable text found in {name}.")

    truncated = len(text) > MAX_TEXT_CHARS
    if truncated:
        text = text[:MAX_TEXT_CHARS]

    result = {
        "filename": name,
        "chars": len(text),
        "truncated": truncated,
        "text": text,
        # Populated only when an identity is supplied. The chat send flow saves
        # attachments itself, so an extract without an identity is not a lost
        # document — it is one that gets stored a moment later, on send.
        "saved": False,
        "storage_id": "",
        "save_error": "",
    }

    if identity_id.strip():
        from falcon import documents_store as Store

        try:
            saved = Store.save(identity_id.strip(), name, text, source="upload")
            result["saved"] = saved["ok"]
            result["storage_id"] = saved.get("storage_id", "")
            result["save_error"] = saved.get("error", "")
            if not saved["ok"]:
                logger.error("document %r was NOT saved: %s", name, saved.get("error"))
        except Exception as exc:  # noqa: BLE001
            # Extraction succeeded, so the turn can still proceed — but never
            # report a save that did not happen.
            logger.error("document %r could not be stored: %s", name, exc)
            result["save_error"] = str(exc)

    return result
