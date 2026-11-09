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
from docx.shared import Pt
from docx.text.paragraph import Paragraph
from pdf2docx import Converter

from .layout import DocError, open_pdf, script_kind
from .render import ltr_signs

_HEADER_BAND = 0.07
_FOOTER_BAND = 0.93
_MIN_MARGIN_CHARS = 8
_SCRIPT_CONTEXT = 8
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
        marks = _script_marks(src)
        for paragraph in _body_paragraphs(d):
            _restore_spaces(paragraph, pdf_text)
            _mark_scripts(paragraph, marks)
        _promote_margins(d, src)
        _keep_pages_from_spilling(d)
        for paragraph in _body_paragraphs(d):
            _mark_rtl(paragraph)
        for section in d.sections:
            for part in (section.header, section.footer):
                if part.is_linked_to_previous:
                    continue
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
    kept = [_keep_original(run.text or "", text) for run, text in zip(runs, planned)]
    if any(text is None for text in kept):
        return
    for run, text in zip(runs, kept):
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


def _clusters(s: str) -> list[str]:
    found: list[str] = []
    for ch in s:
        if found and unicodedata.category(ch)[0] == "M":
            found[-1] += ch
        else:
            found.append(ch)
    return found


def _keep_original(original: str, planned: str) -> str | None:
    out: list[str] = []
    pos = 0
    space = ""
    for cluster in _clusters(original):
        need = _compact(cluster)
        if not need:
            if cluster.isspace():
                space = cluster
            else:
                out.append(cluster)
            continue
        if planned[pos : pos + 1].isspace():
            out.append(space or planned[pos])
            pos += 1
            while planned[pos : pos + 1].isspace():
                pos += 1
        space = ""
        if not planned.startswith(need, pos):
            return None
        out.append(cluster)
        pos += len(need)
    if planned[pos:].strip():
        return None
    return "".join(out) + planned[pos:]


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


def _script_marks(doc: pymupdf.Document) -> list[tuple[str, str, str, str]]:
    marks: list[tuple[str, str, str, str]] = []
    for page in doc:
        for block in page.get_text("dict", flags=_TEXT_FLAGS)["blocks"]:
            for line in block.get("lines", []):
                spans = [s for s in line["spans"] if _compact(s["text"])]
                base = None
                for i, span in enumerate(spans):
                    kind = None
                    if base:
                        kind = script_kind(
                            base["size"], base["origin"][1], span["size"], span["origin"][1], span["text"]
                        )
                    if kind:
                        before = _compact("".join(s["text"] for s in spans[:i]))[-_SCRIPT_CONTEXT:]
                        after = _compact("".join(s["text"] for s in spans[i + 1 :]))[:_SCRIPT_CONTEXT]
                        marks.append((before, _compact(span["text"]), after, kind))
                    else:
                        base = span
    return marks


def _mark_scripts(paragraph: Paragraph, marks: list[tuple[str, str, str, str]]) -> None:
    if not marks:
        return
    runs = [r for r in paragraph.runs if _compact(r.text or "")]
    parts = [_compact(r.text or "") for r in runs]
    for i, run in enumerate(runs):
        if i == 0:
            continue
        before = "".join(parts[:i])
        after = "".join(parts[i + 1 :])
        for tail, number, head, kind in marks:
            if parts[i] == number and before.endswith(tail) and after.startswith(head):
                if kind == "sup":
                    run.font.superscript = True
                else:
                    run.font.subscript = True
                if runs[i - 1].font.size:
                    run.font.size = runs[i - 1].font.size
                break


_NUMBER = re.compile(r"\d+")


def _margin_key(text: str) -> str:
    return _NUMBER.sub("#", text)


def _margin_lines(doc: pymupdf.Document, kind: str) -> dict[str, list[str | None]]:
    if kind == "header" and doc.page_count < 2:
        return {}
    per_page: list[dict[str, list[int]]] = []
    for page in doc:
        height = page.rect.height
        lines: dict[str, list[int]] = {}
        for block in page.get_text("dict", flags=_TEXT_FLAGS)["blocks"]:
            for line in block.get("lines", []):
                text = _norm("".join(span["text"] for span in line["spans"]))
                if len(text) < _MIN_MARGIN_CHARS:
                    continue
                y0, y1 = line["bbox"][1], line["bbox"][3]
                cy = (y0 + y1) / 2
                in_band = cy < height * _HEADER_BAND if kind == "header" else cy > height * _FOOTER_BAND
                if in_band:
                    lines.setdefault(_margin_key(text), [int(n) for n in _NUMBER.findall(text)])
        per_page.append(lines)
    if not per_page:
        return {}
    total = len(per_page)
    found: dict[str, list[str | None]] = {}
    for key, numbers in per_page[0].items():
        if not all(key in lines for lines in per_page):
            continue
        fields: list[str | None] | None = []
        for i in range(len(numbers)):
            values = [lines[key][i] for lines in per_page]
            if len(set(values)) == 1:
                fields.append(None)
            elif values == list(range(1, total + 1)):
                fields.append("PAGE")
            else:
                fields = None
                break
        if fields is None:
            continue
        if "PAGE" in fields:
            fields = [
                "NUMPAGES" if f is None and per_page[0][key][i] == total else f for i, f in enumerate(fields)
            ]
        found[key] = fields
    return found


def _margin_extent(doc: pymupdf.Document, kind: str, keys: dict[str, list[str | None]]) -> float | None:
    page = doc[0]
    edges: list[float] = []
    for block in page.get_text("dict", flags=_TEXT_FLAGS)["blocks"]:
        for line in block.get("lines", []):
            if _margin_key(_norm("".join(span["text"] for span in line["spans"]))) in keys:
                edges.append(line["bbox"][1] if kind == "header" else page.rect.height - line["bbox"][3])
    return min(edges) if edges else None


def _write_fields(p, fields: list[str | None]) -> None:
    paragraph = Paragraph(p, None)
    text = paragraph.text
    runs = p.findall(qn("w:r"))
    if not runs or "PAGE" not in fields or len(_NUMBER.findall(text)) != len(fields):
        return
    r_pr = runs[0].find(qn("w:rPr"))

    def run(content: str | None = None):
        r = OxmlElement("w:r")
        if r_pr is not None:
            r.append(deepcopy(r_pr))
        if content is not None:
            t = OxmlElement("w:t")
            t.set(qn("xml:space"), "preserve")
            t.text = content
            r.append(t)
        return r

    for r in runs:
        p.remove(r)
    pos = 0
    for match, field in zip(_NUMBER.finditer(text), fields):
        if field is None:
            continue
        if match.start() > pos:
            p.append(run(text[pos : match.start()]))
        simple = OxmlElement("w:fldSimple")
        simple.set(qn("w:instr"), field)
        simple.append(run(match.group()))
        p.append(simple)
        pos = match.end()
    if pos < len(text):
        p.append(run(text[pos:]))


def _settle_margin_paragraph(p) -> None:
    p_pr = p.find(qn("w:pPr"))
    spacing = p_pr.find(qn("w:spacing")) if p_pr is not None else None
    if spacing is not None:
        for side in ("w:before", "w:after"):
            if spacing.get(qn(side)) is not None:
                spacing.set(qn(side), "0")


def _drop_empty_paragraphs(part) -> None:
    elm = part._element
    for p in elm.findall(qn("w:p")):
        if len(elm) > 1 and not p.findall(".//" + qn("w:t")) and not p.findall(".//" + qn("w:drawing")):
            elm.remove(p)


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
    header_lines = _margin_lines(src, "header")
    footer_lines = _margin_lines(src, "footer")
    if not header_lines and not footer_lines:
        _settle_margin_distances(d, None, None)
        return
    section = d.sections[0]
    header = section.header
    footer = section.footer
    seen_h: set[tuple[str, ...]] = set()
    seen_f: set[tuple[str, ...]] = set()
    body = d.element.body
    for elm in list(body.iterchildren()):
        if elm.tag == qn("w:sectPr"):
            continue
        keys = tuple(_margin_key(ln) for ln in _element_lines(elm))
        if not keys:
            continue
        for lines, seen, part in ((header_lines, seen_h, header), (footer_lines, seen_f, footer)):
            if lines and all(key in lines for key in keys):
                elm.getparent().remove(elm)
                if keys not in seen:
                    seen.add(keys)
                    part.is_linked_to_previous = False
                    if elm.tag == qn("w:p"):
                        _write_fields(elm, lines[keys[0]])
                        _settle_margin_paragraph(elm)
                    part._element.append(elm)
                break
    if seen_h:
        _drop_page_top_spacers(body)
    header_edge = _margin_extent(src, "header", header_lines) if seen_h else None
    footer_edge = _margin_extent(src, "footer", footer_lines) if seen_f else None
    for i, extra in enumerate(d.sections):
        if seen_h:
            if i == 0:
                _drop_empty_paragraphs(header)
            else:
                _clone_part(header._element, extra.header)
        if seen_f:
            if i == 0:
                _drop_empty_paragraphs(footer)
            else:
                _clone_part(footer._element, extra.footer)
    _settle_margin_distances(d, header_edge, footer_edge)


def _settle_margin_distances(d: Document, header_edge: float | None, footer_edge: float | None) -> None:
    for section in d.sections:
        section.header_distance = Pt(header_edge) if header_edge is not None else section.top_margin
        section.footer_distance = Pt(footer_edge) if footer_edge is not None else section.bottom_margin


def _drop_page_top_spacers(body) -> None:
    at_top = True
    for elm in list(body.iterchildren()):
        if elm.tag == qn("w:sectPr"):
            continue
        p_pr = elm.find(qn("w:pPr")) if elm.tag == qn("w:p") else None
        ends_page = p_pr is not None and p_pr.find(qn("w:sectPr")) is not None
        empty = (
            elm.tag == qn("w:p")
            and not elm.findall(".//" + qn("w:t"))
            and not elm.findall(".//" + qn("w:drawing"))
        )
        if at_top and empty and not ends_page:
            body.remove(elm)
            continue
        at_top = ends_page


_PAGE_SLACK_TWIPS = 480


def _keep_pages_from_spilling(d: Document) -> None:
    last = None
    for elm in d.element.body.iterchildren():
        if elm.tag == qn("w:tbl"):
            last = None
        elif elm.tag == qn("w:p"):
            p_pr = elm.find(qn("w:pPr"))
            if (
                p_pr is not None
                and p_pr.find(qn("w:sectPr")) is not None
                and not elm.findall(".//" + qn("w:t"))
            ):
                _shrink_section_break(elm, p_pr)
                _give_back_space(last)
                last = None
            elif elm.findall(".//" + qn("w:t")):
                last = elm
    _give_back_space(last)


def _shrink_section_break(p, p_pr) -> None:
    spacing = p_pr.find(qn("w:spacing"))
    if spacing is None:
        spacing = OxmlElement("w:spacing")
        p_pr.insert(0, spacing)
    spacing.set(qn("w:before"), "0")
    spacing.set(qn("w:after"), "0")
    spacing.set(qn("w:line"), "20")
    spacing.set(qn("w:lineRule"), "exact")
    r_pr = p_pr.find(qn("w:rPr"))
    if r_pr is None:
        r_pr = OxmlElement("w:rPr")
        p_pr.insert(list(p_pr).index(p_pr.find(qn("w:sectPr"))), r_pr)
    if r_pr.find(qn("w:sz")) is None:
        size = OxmlElement("w:sz")
        size.set(qn("w:val"), "2")
        r_pr.append(size)


def _give_back_space(p) -> None:
    if p is None:
        return
    p_pr = p.find(qn("w:pPr"))
    spacing = p_pr.find(qn("w:spacing")) if p_pr is not None else None
    before = spacing.get(qn("w:before")) if spacing is not None else None
    if before and before.isdigit() and int(before) > _PAGE_SLACK_TWIPS:
        spacing.set(qn("w:before"), str(int(before) - _PAGE_SLACK_TWIPS))


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
        text = run.text or ""
        signed = ltr_signs(text)
        if signed != text:
            run.text = signed
            if not any("\u0590" <= ch <= "\u08ff" for ch in text):
                continue
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
