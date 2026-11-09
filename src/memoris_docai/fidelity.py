from __future__ import annotations

import io
import re
import unicodedata
from dataclasses import dataclass

import pymupdf
from docx import Document
from docx.oxml.ns import qn

from .docx import docx_text, pdf_plain_text, pdf_to_docx

W, H = pymupdf.paper_size("a4")
_CSS = "* {font-family: sans-serif; margin: 0; padding: 0;}"


@dataclass(frozen=True)
class Sample:
    name: str
    pdf: bytes
    words: tuple[str, ...]
    pages: int


def _words(s: str) -> list[str]:
    return re.findall(r"[\w\u0600-\u06ff]+", unicodedata.normalize("NFKC", s).lower(), re.UNICODE)


def _table(page: pymupdf.Page, y: float) -> None:
    cols = [130, 90, 110, 90]
    rows = [
        ["İlaç", "Göz", "Doz", "Süre"],
        ["Moksifloksasin", "Sağ", "4 kez", "7 gün"],
        ["Prednizolon", "Sağ", "3 kez", "14 gün"],
        ["Suni gözyaşı", "Her iki", "1,25 mg", "30 gün"],
    ]
    x0, rh = 56, 22
    xs = [x0]
    for c in cols:
        xs.append(xs[-1] + c)
    for r, row in enumerate(rows):
        yy = y + r * rh
        if r == 0:
            page.draw_rect(pymupdf.Rect(xs[0], yy, xs[-1], yy + rh), color=None, fill=(0.93, 0.93, 0.97))
        for c, cell in enumerate(row):
            weight = "bold" if r == 0 else "normal"
            page.insert_htmlbox(
                pymupdf.Rect(xs[c] + 3, yy + 2, xs[c + 1] - 3, yy + rh - 1),
                f"<span style='font-size:10pt;font-weight:{weight}'>{cell}</span>",
                css=_CSS,
            )
    for r in range(len(rows) + 1):
        page.draw_line((xs[0], y + r * rh), (xs[-1], y + r * rh), color=(0.4, 0.4, 0.45), width=0.6)
    for x in xs:
        page.draw_line((x, y), (x, y + len(rows) * rh), color=(0.4, 0.4, 0.45), width=0.6)


def table_pdf() -> bytes:
    doc = pymupdf.open()
    page = doc.new_page(width=W, height=H)
    page.insert_htmlbox(
        pymupdf.Rect(56, 78, 400, 106),
        "<span style='font-size:16pt'>Kurgusal doz tablosu</span>",
        css=_CSS,
    )
    _table(page, 120)
    data = doc.tobytes()
    doc.close()
    return data


def columns_pdf() -> bytes:
    doc = pymupdf.open()
    page = doc.new_page(width=W, height=H)
    page.insert_htmlbox(
        pymupdf.Rect(56, 78, 400, 106),
        "<span style='font-size:16pt'>Kurgusal iki sütun notu</span>",
        css=_CSS,
    )
    left = (
        "Sol sütun öyküsü. Hasta kurgusal bulanık görme tarif eder. "
        "Sistemik özette kontrollü diyabet vardır. Bilinen alerji yoktur."
    )
    right = "Kontrol saat 09.00 ile 11.30 arasında yeniden ölçülecek. Hedef basınç düşüktür."
    page.insert_htmlbox(pymupdf.Rect(56, 120, 270, 460), f"<p style='font-size:11pt'>{left}</p>", css=_CSS)
    page.insert_htmlbox(pymupdf.Rect(300, 120, 430, 560), f"<p style='font-size:11pt'>{right}</p>", css=_CSS)
    data = doc.tobytes()
    doc.close()
    return data


def arabic_pdf() -> bytes:
    doc = pymupdf.open()
    page = doc.new_page(width=W, height=H)
    html = (
        "<div dir='rtl' style='font-size:14pt'>"
        "تقرير خيالي. قطرة موكسيفلوكساسين أربع مرات يوميا لمدة سبعة أيام. "
        "العدسة +21,5 ديوبتر. لا توجد حساسية معروفة."
        "</div>"
    )
    page.insert_htmlbox(pymupdf.Rect(56, 80, W - 56, 280), html)
    data = doc.tobytes()
    doc.close()
    return data


def image_pdf() -> bytes:
    doc = pymupdf.open()
    page = doc.new_page(width=W, height=H)
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 80, 40), 0)
    pix.set_rect(pix.irect, (30, 90, 170))
    page.insert_image(pymupdf.Rect(56, 64, 176, 124), pixmap=pix)
    page.insert_htmlbox(
        pymupdf.Rect(56, 140, 420, 162),
        "<span style='font-size:12pt'>Kurgusal şekil alt yazısı</span>",
        css=_CSS,
    )
    page.insert_htmlbox(
        pymupdf.Rect(56, 168, 240, 190),
        "<span style='font-size:11pt'>Doz 1,25 mg</span>",
        css=_CSS,
    )
    data = doc.tobytes()
    doc.close()
    return data


def header_footer_pdf() -> bytes:
    doc = pymupdf.open()
    for n in (1, 2):
        page = doc.new_page(width=W, height=H)
        page.insert_htmlbox(
            pymupdf.Rect(56, 16, 420, 38),
            "<span style='font-size:10pt'>Kurgusal hastane başlığı</span>",
            css=_CSS,
        )
        page.insert_htmlbox(
            pymupdf.Rect(56, 110, 460, 140),
            f"<span style='font-size:12pt'>Gövde sayfa {n}. Kurgusal epikriz cümlesi.</span>",
            css=_CSS,
        )
        page.insert_htmlbox(
            pymupdf.Rect(56, H - 42, 420, H - 20),
            "<span style='font-size:9pt'>Kurgusal altbilgi demosu</span>",
            css=_CSS,
        )
    data = doc.tobytes()
    doc.close()
    return data


def form_pdf() -> bytes:
    doc = pymupdf.open()
    page = doc.new_page(width=W, height=H)
    page.insert_htmlbox(
        pymupdf.Rect(56, 48, 320, 76),
        "<span style='font-size:16pt'>Kurgusal form</span>",
        css=_CSS,
    )
    page.insert_htmlbox(
        pymupdf.Rect(56, 100, 150, 122),
        "<span style='font-size:11pt'>Protokol</span>",
        css=_CSS,
    )
    widget = pymupdf.Widget()
    widget.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
    widget.field_name = "protocol"
    widget.field_value = "DEMO-2026-0417"
    widget.rect = pymupdf.Rect(160, 104, 400, 126)
    widget.text_fontsize = 11
    page.add_widget(widget)
    page.insert_htmlbox(
        pymupdf.Rect(56, 148, 360, 172),
        "<span style='font-size:11pt'>Doz alanı 4 kez 1 damla</span>",
        css=_CSS,
    )
    doc.xref_set_key(widget.xref, "AP", "null")
    data = doc.tobytes()
    doc.close()
    return data


def corpus() -> list[Sample]:
    return [
        Sample("table", table_pdf(), ("moksifloksasin", "gün"), 1),
        Sample("columns", columns_pdf(), ("sütun", "yeniden", "ölçülecek"), 1),
        Sample("arabic", arabic_pdf(), ("قطرة", "أيام"), 1),
        Sample("image", image_pdf(), ("şekil", "doz"), 1),
        Sample("header_footer", header_footer_pdf(), ("başlığı", "altbilgi", "epikriz"), 2),
        Sample("form", form_pdf(), ("protokol", "damla"), 1),
    ]


def pdf_text(data: bytes) -> str:
    return pdf_plain_text(data)


def recall(source: str, got: str) -> float:
    src = _words(source)
    if not src:
        return 1.0
    have = set(_words(got))
    return sum(w in have for w in src) / len(src)


def letter_recall(source: str, got: str) -> float:

    def letters(s: str) -> list[str]:
        return [ch for ch in unicodedata.normalize("NFKC", s).lower() if ch.isalpha()]

    src = set(letters(source))
    if not src:
        return 1.0
    return len(src & set(letters(got))) / len(src)


def number_recall(source: str, got: str) -> float:
    nums = re.findall(r"\d+(?:[.,]\d+)?", source)
    if not nums:
        return 1.0
    have = set(re.findall(r"\d+(?:[.,]\d+)?", got))
    return sum(n in have for n in nums) / len(nums)


def table_shapes(docx: bytes) -> list[list[int]]:
    d = Document(io.BytesIO(docx))
    shapes = [[len(t.rows), len(t.columns)] for t in d.tables]
    for section in d.sections[:1]:
        for part in (section.header, section.footer):
            shapes.extend([len(t.rows), len(t.columns)] for t in part.tables)
    return shapes


def font_sizes(docx: bytes) -> list[float]:
    d = Document(io.BytesIO(docx))
    sizes: set[float] = set()
    paras = list(d.paragraphs)
    for t in d.tables:
        for row in t.rows:
            for cell in row.cells:
                paras.extend(cell.paragraphs)
    if d.sections:
        for part in (d.sections[0].header, d.sections[0].footer):
            paras.extend(part.paragraphs)
    for p in paras:
        for run in p.runs:
            if run.font.size:
                sizes.add(round(run.font.size.pt, 1))
    return sorted(sizes)


def column_count(docx: bytes) -> int:
    d = Document(io.BytesIO(docx))
    best = 1
    for section in d.sections:
        cols = section._sectPr.find(qn("w:cols"))
        if cols is None:
            continue
        num = cols.get(qn("w:num"))
        if num and int(num) > best:
            best = int(num)
    return best


def has_bidi(docx: bytes) -> bool:
    d = Document(io.BytesIO(docx))
    xml = d.element.xml
    for section in d.sections[:1]:
        xml += section.header._element.xml
        xml += section.footer._element.xml
    return "w:bidi" in xml


def margin_lengths(docx: bytes) -> tuple[int, int]:
    d = Document(io.BytesIO(docx))
    if not d.sections:
        return 0, 0
    header = sum(len(p.text.strip()) for p in d.sections[0].header.paragraphs)
    for t in d.sections[0].header.tables:
        for row in t.rows:
            for cell in row.cells:
                header += len(cell.text.strip())
    footer = sum(len(p.text.strip()) for p in d.sections[0].footer.paragraphs)
    return header, footer


def measure(pdf: bytes) -> dict:
    docx, stats = pdf_to_docx(pdf)
    text = docx_text(docx)
    source = pdf_text(pdf)
    header, footer = margin_lengths(docx)
    return {
        "docx": docx,
        "page_count": stats["page_count"],
        "text_recall": round(recall(source, text), 3),
        "letter_recall": round(letter_recall(source, text), 3),
        "number_recall": round(number_recall(source, text), 3),
        "tables": stats["tables"],
        "table_shapes": table_shapes(docx),
        "images": stats["images"],
        "font_sizes": font_sizes(docx),
        "paragraphs": stats["paragraphs"],
        "columns": column_count(docx),
        "bidi": has_bidi(docx),
        "header_chars": header,
        "footer_chars": footer,
    }


def public_metrics(row: dict) -> dict:
    return {k: v for k, v in row.items() if k != "docx"}
