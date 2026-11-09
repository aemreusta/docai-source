from __future__ import annotations

import io
import logging
import re
import tempfile
import unicodedata
from copy import deepcopy
from pathlib import Path

import pymupdf
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph
from pdf2docx import Converter

from .layout import DocError, open_pdf

_HEADER_BAND = 0.07
_FOOTER_BAND = 0.93
_MIN_MARGIN_CHARS = 8
_TEXT_FLAGS = 0 | pymupdf.TEXT_MEDIABOX_CLIP | pymupdf.TEXT_CID_FOR_UNKNOWN_UNICODE


class _DropPdf2docx(logging.Filter):

    def filter(self, record: logging.LogRecord) -> bool:
        return "pdf2docx" not in record.pathname.replace("\\", "/")


logging.getLogger().addFilter(_DropPdf2docx())
logging.getLogger("pdf2docx").setLevel(logging.CRITICAL)


def pdf_to_docx(data: bytes) -> tuple[bytes, dict]:
    doc = open_pdf(data)
    pages = doc.page_count
    prepared = _bake_widgets(doc)
    doc.close()
    with tempfile.TemporaryDirectory() as tmp:
        src, out = Path(tmp) / "in.pdf", Path(tmp) / "out.docx"
        src.write_bytes(prepared)
        try:
            cv = Converter(str(src))
            try:
                cv.convert(str(out), multi_processing=False)
            finally:
                cv.close()
        except DocError:
            raise
        except Exception as e:
            raise DocError("docx-conversion") from e
        if not out.exists() or out.stat().st_size == 0:
            raise DocError("docx-conversion")
        docx = out.read_bytes()
    docx = _polish(docx, prepared)
    return docx, {"page_count": pages, **structure(docx)}


def pdf_plain_text(data: bytes) -> str:
    doc = pymupdf.open(stream=data)
    try:
        return "\n".join(page.get_text("text", flags=_TEXT_FLAGS) for page in doc)
    finally:
        doc.close()


def _bake_widgets(doc: pymupdf.Document) -> bytes:
    if not any(page.first_widget is not None for page in doc):
        return doc.tobytes()
    try:
        doc.bake(annots=False, widgets=True)
    except (RuntimeError, ValueError):
        return doc.tobytes()
    return doc.tobytes()


def _polish(docx: bytes, pdf: bytes) -> bytes:
    d = Document(io.BytesIO(docx))
    src = pymupdf.open(stream=pdf)
    try:
        pdf_text = unicodedata.normalize(
            "NFKC", "\n".join(page.get_text("text", flags=_TEXT_FLAGS) for page in src)
        )
        for paragraph in _body_paragraphs(d):
            _restore_spaces(paragraph, pdf_text)
        _promote_margins(d, src)
        for paragraph in _body_paragraphs(d):
            _mark_rtl(paragraph)
        for section in d.sections:
            for part in (section.header, section.footer):
                for paragraph in part.paragraphs:
                    _mark_rtl(paragraph)
                for table in part.tables:
                    for row in table.rows:
                        for cell in row.cells:
                            for paragraph in cell.paragraphs:
                                _mark_rtl(paragraph)
    finally:
        src.close()
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def _body_paragraphs(d: Document) -> list[Paragraph]:
    found = list(d.paragraphs)
    for table in d.tables:
        for row in table.rows:
            for cell in row.cells:
                found.extend(cell.paragraphs)
    return found


def _skip(ch: str) -> bool:
    return ch.isspace() or unicodedata.category(ch)[0] == "C"


def _compact(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)
    return "".join(ch for ch in s if not _skip(ch))


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", s)).strip()


def _restore_spaces(paragraph: Paragraph, pdf_text: str) -> None:
    runs = [r for r in paragraph.runs if _compact(r.text or "")]
    if not runs:
        return
    joined = "".join(r.text or "" for r in runs)
    spaced = _spaced_slice(pdf_text, joined)
    if spaced is None:
        spaced = _space_words(joined, pdf_text)
    if spaced is None:
        return
    planned = _plan_runs(runs, spaced)
    if planned is None or _compact("".join(planned)) != _compact(joined):
        return
    for run, text in zip(runs, planned):
        if (run.text or "") != text:
            run.text = text


def _pdf_tokens(pdf_text: str) -> list[str]:
    tokens: list[str] = []
    for raw in re.findall(r"\S+", pdf_text):
        compact = _compact(raw)
        if len(compact) >= 2:
            tokens.append(compact)
    return tokens


def _space_words(text: str, pdf_text: str) -> str | None:
    compact = _compact(text)
    tokens = _pdf_tokens(pdf_text)
    if len(compact) < 6 or not tokens:
        return None
    boundaries: set[int] = set()
    index = 0
    matched = 0
    while index < len(compact):
        cands = [tok for tok in tokens if compact.startswith(tok, index)]
        if not cands:
            break
        best = max(cands, key=len)
        index += len(best)
        matched += len(best)
        boundaries.add(index)
    boundaries.discard(len(compact))
    if matched < 6 or not boundaries:
        return None
    out: list[str] = []
    seen = 0
    for ch in unicodedata.normalize("NFKC", text):
        if _skip(ch):
            if ch.isspace() and (not out or not out[-1].isspace()):
                out.append(" ")
            continue
        if seen in boundaries and (not out or not out[-1].isspace()):
            out.append(" ")
        out.append(ch)
        seen += 1
    spaced = "".join(out).strip()
    if _compact(spaced) != compact:
        return None
    return spaced


def _spaced_slice(pdf_text: str, text: str) -> str | None:
    target = _compact(text)
    if len(target) < 6:
        return None
    cmap = [i for i, ch in enumerate(pdf_text) if not _skip(ch)]
    compact_pdf = "".join(pdf_text[i] for i in cmap)
    start = compact_pdf.find(target)
    if start < 0 or compact_pdf.find(target, start + 1) >= 0:
        return None
    sl = pdf_text[cmap[start] : cmap[start + len(target) - 1] + 1]
    out: list[str] = []
    for ch in sl:
        if _skip(ch):
            if ch.isspace() and out and not out[-1].isspace():
                out.append(" ")
            continue
        out.append(ch)
    return "".join(out).strip()


def _plan_runs(runs, spaced: str) -> list[str] | None:
    pos = 0
    planned: list[str] = []
    for run in runs:
        need = _compact(run.text or "")
        got: list[str] = []
        have: list[str] = []
        while pos < len(spaced) and "".join(have) != need:
            ch = spaced[pos]
            pos += 1
            got.append(ch)
            if not ch.isspace():
                have.append(ch)
        if "".join(have) != need:
            return None
        while got and got[-1].isspace():
            got.pop()
            pos -= 1
        planned.append("".join(got))
    if pos < len(spaced) and spaced[pos:].strip():
        return None
    return planned


def _margin_lines(doc: pymupdf.Document, kind: str) -> list[str]:
    per_page: list[list[str]] = []
    for page in doc:
        height = page.rect.height
        lines: list[str] = []
        for block in page.get_text("dict", flags=_TEXT_FLAGS)["blocks"]:
            for line in block.get("lines", []):
                text = _norm("".join(span["text"] for span in line["spans"]))
                if len(text) < _MIN_MARGIN_CHARS:
                    continue
                y0, y1 = line["bbox"][1], line["bbox"][3]
                cy = (y0 + y1) / 2
                in_band = cy < height * _HEADER_BAND if kind == "header" else cy > height * _FOOTER_BAND
                if in_band:
                    lines.append(text)
        per_page.append(lines)
    if not per_page:
        return []
    common = set(per_page[0])
    for lines in per_page[1:]:
        common &= set(lines)
    return [ln for ln in per_page[0] if ln in common]


def _element_lines(elm) -> list[str]:
    if elm.tag == qn("w:p"):
        text = _norm(Paragraph(elm, None).text)
        return [text] if text else []
    if elm.tag == qn("w:tbl"):
        lines = []
        for p in elm.xpath("./w:tr/w:tc//w:p"):
            text = _norm(Paragraph(p, None).text)
            if text:
                lines.append(text)
        return lines
    return []


def _promote_margins(d: Document, src: pymupdf.Document) -> None:
    header_lines = set(_margin_lines(src, "header"))
    footer_lines = set(_margin_lines(src, "footer"))
    if not header_lines and not footer_lines:
        return
    section = d.sections[0]
    header = section.header
    footer = section.footer
    header.is_linked_to_previous = False
    footer.is_linked_to_previous = False
    seen_h: set[tuple[str, ...]] = set()
    seen_f: set[tuple[str, ...]] = set()
    body = d.element.body
    for elm in list(body.iterchildren()):
        if elm.tag == qn("w:sectPr"):
            continue
        lines = tuple(_element_lines(elm))
        if not lines:
            continue
        if header_lines and all(ln in header_lines for ln in lines):
            if lines in seen_h:
                elm.getparent().remove(elm)
            else:
                seen_h.add(lines)
                elm.getparent().remove(elm)
                header._element.append(elm)
        elif footer_lines and all(ln in footer_lines for ln in lines):
            if lines in seen_f:
                elm.getparent().remove(elm)
            else:
                seen_f.add(lines)
                elm.getparent().remove(elm)
                footer._element.append(elm)
    for extra in d.sections[1:]:
        _clone_part(header._element, extra.header)
        _clone_part(footer._element, extra.footer)


def _clone_part(source, target) -> None:
    target.is_linked_to_previous = False
    elm = target._element
    for child in list(elm):
        elm.remove(child)
    for child in source:
        elm.append(deepcopy(child))


def _mark_rtl(paragraph: Paragraph) -> None:
    text = paragraph.text or ""
    rtl = sum(1 for ch in text if "\u0590" <= ch <= "\u08ff")
    latin = sum(1 for ch in text if ("A" <= ch <= "Z") or ("a" <= ch <= "z") or ("\u00c0" <= ch <= "\u024f"))
    if rtl < 4 or rtl <= latin:
        return
    p_pr = paragraph._p.get_or_add_pPr()
    if p_pr.find(qn("w:bidi")) is None:
        p_pr.append(OxmlElement("w:bidi"))
    for run in paragraph.runs:
        r_pr = run._r.get_or_add_rPr()
        if r_pr.find(qn("w:rtl")) is None:
            r_pr.append(OxmlElement("w:rtl"))


def _containers(d: Document):
    yield d
    if d.sections:
        yield d.sections[0].header
        yield d.sections[0].footer


def structure(docx: bytes) -> dict:
    d = Document(io.BytesIO(docx))
    paragraphs = 0
    tables = 0
    for container in _containers(d):
        paragraphs += sum(1 for p in container.paragraphs if p.text.strip())
        tables += len(container.tables)
        for table in container.tables:
            for row in table.rows:
                for cell in row.cells:
                    paragraphs += sum(1 for p in cell.paragraphs if p.text.strip())
    images = sum(1 for rel in d.part.rels.values() if "image" in rel.reltype)
    return {"paragraphs": paragraphs, "tables": tables, "images": images}


def docx_text(docx: bytes) -> str:
    d = Document(io.BytesIO(docx))
    parts: list[str] = []
    for container in _containers(d):
        parts.extend(p.text for p in container.paragraphs)
        for table in container.tables:
            for row in table.rows:
                for cell in row.cells:
                    parts.extend(p.text for p in cell.paragraphs)
    return "\n".join(parts)
