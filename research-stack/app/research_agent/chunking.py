from __future__ import annotations

import re

from .models import Chunk, Document

MD_HEADING_RE = re.compile(r"^#{1,6}\s+\S")
TITLE_LINE_RE = re.compile(r"^[A-Z0-9][A-Za-z0-9 ,:()\-]{3,80}$")


def _split_paragraphs(text: str) -> list[str]:
    # Trafilatura separates paragraphs with single newlines, Markdown/PDF often with blank lines.
    paras = [p.strip() for p in re.split(r"\n+", text) if p.strip()]
    out: list[str] = []
    for p in paras:
        if len(p.split()) > 400:  # very long paragraph: split by sentences
            out.extend(s.strip() for s in re.split(r"(?<=[.!?])\s+", p) if s.strip())
        else:
            out.append(p)
    return out


def chunk_document(doc: Document, max_words: int = 300, overlap_words: int = 50) -> list[Chunk]:
    """Paragraph-aware chunking.

    ~300 words (~400 tokens) fits comfortably inside bge-reranker-v2-m3's 512-token window
    together with the query. Each chunk is prefixed with the title and nearest heading so it
    still makes sense when read alone.
    """
    chunks: list[Chunk] = []
    buf: list[str] = []
    buf_words = 0
    has_new = False  # True once buf holds text that is not just the overlap tail
    heading = ""

    def flush() -> None:
        nonlocal buf, buf_words, has_new
        if not has_new:
            return
        prefix = f"{doc.title}" + (f" | {heading}" if heading else "")
        chunks.append(Chunk(doc=doc, index=len(chunks), text=f"{prefix}\n\n" + "\n".join(buf)))
        tail = " ".join(" ".join(buf).split()[-overlap_words:]) if overlap_words else ""
        buf = [tail] if tail else []
        buf_words = len(tail.split())
        has_new = False

    for para in _split_paragraphs(doc.text):
        words = len(para.split())
        if MD_HEADING_RE.match(para):
            heading = para.lstrip("# ").strip()
            continue
        if words < 12 and TITLE_LINE_RE.match(para) and not para.endswith("."):
            heading = para  # likely a section title; keep it in the text too
        if has_new and buf_words + words > max_words:
            flush()
        buf.append(para)
        buf_words += words
        has_new = True
    flush()
    return chunks
