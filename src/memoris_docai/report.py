from __future__ import annotations

import copy
import io
import re

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor

from .layout import DocError

PLACEHOLDER = re.compile(r"\{\{\s*(title|content|date|section\.[a-z][a-z0-9_]{0,47})\s*\}\}")
LABELS = {
    "tr": {"approval": "Hazırlayan", "date": "Tarih", "page": "Sayfa"},
    "en": {"approval": "Prepared by", "date": "Date", "page": "Page"},
}


def _paragraphs(doc):

    def walk(container):
        yield from container.paragraphs
        for t in getattr(container, "tables", []):
            for row in t.rows:
                for cell in row.cells:
                    yield from walk(cell)

    yield from walk(doc)
    for s in doc.sections:
        for part in (s.header, s.footer, s.first_page_header, s.first_page_footer):
            yield from walk(part)


def _fill_paragraph(p, values: dict[str, list[str]]) -> None:
    text = "".join(r.text for r in p.runs)
    if "{{" not in text or not PLACEHOLDER.search(text):
        return
    whole = PLACEHOLDER.fullmatch(text.strip())
    runs = p.runs
    if whole and len(values.get(whole.group(1), [])) > 1:
        lines = values[whole.group(1)]
        for r in runs[1:]:
            r.text = ""
        runs[0].text = lines[0]
        anchor = p._p
        for line in lines[1:]:
            clone = copy.deepcopy(p._p)
            anchor.addnext(clone)
            anchor = clone
            rs = clone.findall(qn("w:r"))
            for r in rs[1:]:
                clone.remove(r)
            for t in rs[0].findall(qn("w:t")):
                t.text = line
        return
    joined = PLACEHOLDER.sub(lambda m: " ".join(values.get(m.group(1), [])), text)
    for r in runs[1:]:
        r.text = ""
    runs[0].text = joined


def fill_template(template: bytes, values: dict[str, list[str]]) -> bytes:
    try:
        doc = Document(io.BytesIO(template))
    except Exception as e:
        raise DocError("invalid-template") from e
    for p in list(_paragraphs(doc)):
        _fill_paragraph(p, values)
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def _page_field(paragraph) -> None:
    run = paragraph.add_run()
    for kind, text in (("begin", None), (None, "PAGE"), ("end", None)):
        if kind:
            el = OxmlElement("w:fldChar")
            el.set(qn("w:fldCharType"), kind)
        else:
            el = OxmlElement("w:instrText")
            el.set(qn("xml:space"), "preserve")
            el.text = text
        run._r.append(el)


def build_from_definition(
    definition: dict, report: dict, logo: bytes | None, signature: bytes | None
) -> bytes:
    lang = report["language"] if report["language"] in LABELS else "en"
    labels = LABELS[lang]
    lh = definition["letterhead"]
    color = RGBColor.from_string(lh["color"].lstrip("#"))
    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = lh["font_family"]
    style.font.size = Pt(lh["font_size"])
    sec = doc.sections[0]
    head = sec.header.paragraphs[0]
    if logo:
        head.add_run().add_picture(io.BytesIO(logo), height=Pt(28))
        head.add_run("  ")
    name = head.add_run(lh["organization_name"])
    name.bold, name.font.color.rgb = True, color
    for line in (lh["address"], lh["header"]):
        if line.strip():
            sec.header.add_paragraph(line)
    foot = sec.footer.paragraphs[0]
    if lh["footer"].strip():
        foot.add_run(lh["footer"] + "  ")
    if lh["page_numbers"]:
        foot.add_run(labels["page"] + " ")
        _page_field(foot)
    foot.alignment = WD_ALIGN_PARAGRAPH.CENTER

    title = doc.add_paragraph()
    tr = title.add_run(report["title"])
    tr.bold, tr.font.size, tr.font.color.rgb = True, Pt(lh["font_size"] + 6), color
    if report.get("date"):
        doc.add_paragraph(f"{labels['date']}: {report['date']}")
    for s in report["sections"]:
        h = doc.add_paragraph()
        hr = h.add_run(s["title"])
        hr.bold, hr.font.color.rgb = True, color
        for text in s["paragraphs"]:
            doc.add_paragraph(text)

    ap = definition["approval"]
    if ap["name"].strip() or ap["title"].strip() or signature:
        doc.add_paragraph()
        p = doc.add_paragraph()
        p.add_run(labels["approval"] + ": ").bold = True
        p.add_run(" ".join(x for x in (ap["name"], ap["title"]) if x.strip()))
        if ap["date"].strip():
            doc.add_paragraph(f"{labels['date']}: {ap['date']}")
        if signature:
            doc.add_paragraph().add_run().add_picture(io.BytesIO(signature), height=Pt(36))
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def values_for(report: dict) -> dict[str, list[str]]:
    vals: dict[str, list[str]] = {"title": [report["title"]], "date": [report.get("date", "")], "content": []}
    for s in report["sections"]:
        vals["section." + s["key"]] = list(s["paragraphs"]) or [""]
        vals["content"].append(s["title"])
        vals["content"].extend(s["paragraphs"])
    return vals


def render_report(
    definition: dict, report: dict, template: bytes | None, logo: bytes | None, signature: bytes | None
) -> bytes:
    if template:
        return fill_template(template, values_for(report))
    return build_from_definition(definition, report, logo, signature)
