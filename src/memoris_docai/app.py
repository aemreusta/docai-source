from __future__ import annotations

import json
import posixpath

import pymupdf
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from . import images, storage
from .docx import pdf_to_docx
from .layout import DocError, extract, open_pdf, segment
from .render import render
from .report import render_report

app = FastAPI(title="memoris-docai", docs_url=None, redoc_url=None)
store: storage.Storage = storage.from_env()


@app.exception_handler(DocError)
async def doc_error(_, e: DocError):
    return JSONResponse(status_code=422, content={"code": e.code})


@app.exception_handler(ValueError)
async def bad_key(_, e: ValueError):
    return JSONResponse(status_code=400, content={"code": "invalid-key"})


@app.exception_handler(FileNotFoundError)
async def missing(_, e: FileNotFoundError):
    return JSONResponse(status_code=422, content={"code": "artifact-missing"})


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


class NormalizeIn(BaseModel):
    source_keys: list[str] = Field(min_length=1, max_length=30)
    out_key: str


@app.post("/v1/normalize")
def do_normalize(r: NormalizeIn):
    pdf, pages = images.to_pdf([store.get(k) for k in r.source_keys])
    store.put(r.out_key, pdf, "application/pdf")
    return {"pdf_key": r.out_key, "page_count": pages}


class ExtractIn(BaseModel):
    source_key: str
    pages: list[int] | None = None
    source_lang: str | None = Field(
        default=None, max_length=8, pattern=r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})?$"
    )


@app.post("/v1/extract")
def do_extract(r: ExtractIn):
    layout = extract(store.get(r.source_key), source_lang=r.source_lang, pages=r.pages)
    key = storage.sibling(r.source_key, "layout.json")
    store.put(key, json.dumps(layout).encode(), "application/json")
    return {
        "layout_key": key,
        "page_count": layout["page_count"],
        "needs_ocr": any(p["needs_ocr"] for p in layout["pages"]),
        "ocr": layout.get("ocr"),
        "ocr_low_confidence_pages": [p["number"] + 1 for p in layout["pages"] if p.get("ocr_low_confidence")],
    }


class SegmentIn(BaseModel):
    layout_key: str


@app.post("/v1/segment")
def do_segment(r: SegmentIn):
    segs = segment(json.loads(store.get(r.layout_key)))
    key = storage.sibling(r.layout_key, "segments.json")
    store.put(key, json.dumps(segs).encode(), "application/json")
    return {"segments_key": key, "count": sum(1 for s in segs if s["translatable"])}


class RenderIn(BaseModel):
    source_key: str
    layout_key: str
    translations_key: str
    target_lang: str
    result_version: int | None = Field(default=None)
    pages: list[int] | None = None

    @field_validator("result_version")
    @classmethod
    def result_version_range(cls, v: int | None) -> int | None:
        if v is None:
            return None
        if isinstance(v, bool) or v < 2 or v > 999:
            raise ValueError("result_version must be an integer from 2 to 999")
        return v


@app.post("/v1/render")
def do_render(r: RenderIn):
    segs = json.loads(store.get(storage.sibling(r.layout_key, "segments.json")))
    raw = json.loads(store.get(r.translations_key))
    translations = {t["id"]: t["text"] for t in raw} if isinstance(raw, list) else raw
    pdf, report = render(store.get(r.source_key), segs, translations, r.target_lang, pages=r.pages)
    name = f"translated.{r.target_lang}.pdf"
    if r.result_version is not None:
        name = f"translated.{r.target_lang}.v{r.result_version}.pdf"
    key = storage.sibling(r.source_key, name)
    store.put(key, pdf, "application/pdf")
    return {"result_key": key, "layout_report": report}


class PreviewIn(BaseModel):
    source_key: str


@app.post("/v1/preview")
def do_preview(r: PreviewIn):
    data = store.get(r.source_key)
    doc = open_pdf(data)
    chunks = []
    for page in doc:
        chunks.append(page.get_text("text"))
        rect = page.rect
        longest = max(rect.width, rect.height) or 1
        scale = min(1.0, 160.0 / longest)
        pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
        if max(pix.width, pix.height) > 160:
            raise DocError("thumbnail")
        store.put(
            storage.sibling(r.source_key, f"thumb-{page.number + 1}.jpg"), pix.tobytes("jpeg"), "image/jpeg"
        )
    text = "\n".join(chunks)[:8000]
    text_key = storage.sibling(r.source_key, "preview.txt")
    store.put(text_key, text.encode(), "text/plain")
    return {"page_count": doc.page_count, "text_key": text_key}


class DocxIn(BaseModel):
    source_key: str


@app.post("/v1/docx")
def do_docx(r: DocxIn):
    docx, stats = pdf_to_docx(store.get(r.source_key))
    base = posixpath.basename(r.source_key)
    name = (base[:-4] if base.lower().endswith(".pdf") else base) + ".docx"
    key = storage.sibling(r.source_key, name)
    store.put(key, docx, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    return {"docx_key": key, **stats}


class ReportSection(BaseModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,47}$")
    title: str = Field(max_length=160)
    paragraphs: list[str] = Field(max_length=2000)


class ReportBody(BaseModel):
    language: str = Field(pattern=r"^[a-z]{2,3}$")
    title: str = Field(max_length=300)
    date: str = Field(default="", max_length=10)
    sections: list[ReportSection] = Field(max_length=30)


class ReportIn(BaseModel):
    source_key: str
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,80}\.docx$")
    definition: dict
    report: ReportBody
    template_key: str | None = None
    logo_key: str | None = None
    signature_key: str | None = None


@app.post("/v1/report")
def do_report(r: ReportIn):

    def opt(key: str | None) -> bytes | None:
        return store.get(key) if key else None

    docx = render_report(
        r.definition, r.report.model_dump(), opt(r.template_key), opt(r.logo_key), opt(r.signature_key)
    )
    key = storage.sibling(r.source_key, r.name)
    store.put(key, docx, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    return {"docx_key": key}
