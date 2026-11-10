from __future__ import annotations

import json
import posixpath

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, field_validator

from . import guard, images, storage
from .docx import pdf_to_docx
from .layout import DocError, extract, page_image, preview_pdf, segment
from .render import render
from .report import render_report

TIMEOUT_EXTRACT = 280
TIMEOUT_RENDER = 280
TIMEOUT_NORMALIZE = 90
TIMEOUT_PREVIEW = 90
TIMEOUT_DOCX = 110
TIMEOUT_REPORT = 110

app = FastAPI(title="memoris-docai", docs_url=None, redoc_url=None)
store: storage.Storage = storage.from_env()


@app.exception_handler(DocError)
async def doc_error(_, e: DocError):
    return JSONResponse(status_code=422, content={"code": e.code})


@app.exception_handler(guard.Busy)
async def busy(_, e: guard.Busy):
    return JSONResponse(status_code=503, content={"code": "busy"})


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
    pdf, pages = guard.run(images.to_pdf, [store.get(k) for k in r.source_keys], timeout=TIMEOUT_NORMALIZE)
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
    layout = guard.run(extract, store.get(r.source_key), r.source_lang, r.pages, timeout=TIMEOUT_EXTRACT)
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
    translations = (
        {t["id"]: t["text"] for t in raw if t.get("by") != "keep"} if isinstance(raw, list) else raw
    )
    pdf, report = guard.run(
        render, store.get(r.source_key), segs, translations, r.target_lang, r.pages, timeout=TIMEOUT_RENDER
    )
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
    page_count, text, thumbs = guard.run(preview_pdf, store.get(r.source_key), timeout=TIMEOUT_PREVIEW)
    for number, jpeg in enumerate(thumbs, start=1):
        store.put(storage.sibling(r.source_key, f"thumb-{number}.jpg"), jpeg, "image/jpeg")
    text_key = storage.sibling(r.source_key, "preview.txt")
    store.put(text_key, text.encode(), "text/plain")
    return {"page_count": page_count, "text_key": text_key}


class PageImageIn(BaseModel):
    source_key: str
    page: int = Field(ge=1, le=30)


@app.post("/v1/page-image")
def do_page_image(r: PageImageIn):
    jpeg = guard.run(page_image, store.get(r.source_key), r.page, timeout=TIMEOUT_PREVIEW)
    return Response(content=jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


class DocxIn(BaseModel):
    source_key: str


@app.post("/v1/docx")
def do_docx(r: DocxIn):
    docx, stats = guard.run(pdf_to_docx, store.get(r.source_key), timeout=TIMEOUT_DOCX)
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

    docx = guard.run(
        render_report,
        r.definition,
        r.report.model_dump(),
        opt(r.template_key),
        opt(r.logo_key),
        opt(r.signature_key),
        timeout=TIMEOUT_REPORT,
    )
    key = storage.sibling(r.source_key, r.name)
    store.put(key, docx, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    return {"docx_key": key}
