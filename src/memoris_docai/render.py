from __future__ import annotations

import html
import re
from typing import Any

import pymupdf

from .layout import _page_set, open_pdf

RTL = {"ar", "fa", "he", "ur"}
MIN_SCALE = 0.70


PAGE_MARGIN = 28.0


def _obstacles(page: pymupdf.Page) -> list[pymupdf.Rect]:
    out = []
    for d in page.get_drawings():
        r = d["rect"]
        if r.width < 2.5 or r.height < 2.5:
            out.append(pymupdf.Rect(r))
    return out


def available(
    rect: pymupdf.Rect,
    others: list[pymupdf.Rect],
    lines: list[pymupdf.Rect],
    page: pymupdf.Rect,
    *,
    rtl: bool = False,
    grow_x: bool = True,
) -> pymupdf.Rect:
    r = pymupdf.Rect(rect)
    if grow_x:
        if rtl:
            x0 = page.x0 + PAGE_MARGIN
            for o in others + lines:
                if o.y0 < r.y1 - 0.5 and o.y1 > r.y0 + 0.5 and o.x1 <= r.x0 + 0.5:
                    x0 = max(x0, o.x1 + 2)
            r.x0 = min(r.x0, x0)
        else:
            x1 = page.x1 - PAGE_MARGIN
            for o in others + lines:
                if o.y0 < r.y1 - 0.5 and o.y1 > r.y0 + 0.5 and o.x0 >= r.x1 - 0.5:
                    x1 = min(x1, o.x0 - 2)
            r.x1 = max(r.x1, x1)
    y1 = page.y1 - PAGE_MARGIN
    for o in others + lines:
        if o.x0 < r.x1 - 0.5 and o.x1 > r.x0 + 0.5 and o.y0 >= rect.y1 - 0.5:
            y1 = min(y1, o.y0 - 1)
    r.y1 = max(r.y1, y1)
    return r


def _paper(pix: pymupdf.Pixmap, r: pymupdf.Rect) -> tuple[float, float, float]:
    best = (1.0, 1.0, 1.0)
    lum = -1.0
    for x, y in ((r.x0 - 2, r.y0 - 2), (r.x1 + 2, r.y0 - 2), (r.x0 - 2, r.y1 + 2), (r.x1 + 2, r.y1 + 2)):
        xi, yi = int(min(max(x, 0), pix.width - 1)), int(min(max(y, 0), pix.height - 1))
        c = pix.pixel(xi, yi)[:3]
        v = sum(c)
        if v > lum:
            lum, best = v, (c[0] / 255, c[1] / 255, c[2] / 255)
    return best


def _side_by_side(a: list[float], b: list[float]) -> bool:
    overlap = min(a[3], b[3]) - max(a[1], b[1])
    h = min(a[3] - a[1], b[3] - b[1])
    return h > 0 and overlap > 0.5 * h and (b[0] >= a[2] - 1 or a[0] >= b[2] - 1)


def _row_groups(segs: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    items = sorted(segs, key=lambda s: (s["bbox"][1], s["bbox"][0]))
    groups: list[list[dict[str, Any]]] = []
    for s in items:
        placed = False
        for g in groups:
            if any(_side_by_side(s["bbox"], o["bbox"]) for o in g):
                g.append(s)
                placed = True
                break
        if not placed:
            groups.append([s])
    return groups


def _table_clusters(
    groups: list[list[dict[str, Any]]],
) -> list[list[list[dict[str, Any]]]]:
    multi = [g for g in groups if len(g) >= 2]
    multi = sorted(multi, key=lambda g: min(s["bbox"][1] for s in g))
    tables: list[list[list[dict[str, Any]]]] = []
    for g in multi:
        gy0 = min(s["bbox"][1] for s in g)
        if tables:
            prev_y1 = max(s["bbox"][3] for row in tables[-1] for s in row)
            if gy0 <= prev_y1 + 40:
                tables[-1].append(g)
                continue
        tables.append([g])
    return tables


def _mirror_x(bbox: list[float], left: float, right: float) -> list[float]:
    x0, y0, x1, y1 = bbox
    return [left + right - x1, y0, left + right - x0, y1]


def _assign_column(bbox: list[float], slots: list[tuple[float, float]]) -> int:
    x0, _, x1, _ = bbox
    best_i, best = 0, -1.0
    cx = 0.5 * (x0 + x1)
    for i, (a, b) in enumerate(slots):
        overlap = min(x1, b) - max(x0, a)
        if overlap > best:
            best_i, best = i, overlap
    if best > 0:
        return best_i
    return min(range(len(slots)), key=lambda i: abs(0.5 * (slots[i][0] + slots[i][1]) - cx))


def _mirror_table(
    table: list[list[dict[str, Any]]],
    groups: list[list[dict[str, Any]]],
    out: dict[str, list[float]],
) -> set[str]:
    template = max(table, key=lambda row: max(s["bbox"][2] for s in row) - min(s["bbox"][0] for s in row))
    template = sorted(template, key=lambda s: s["bbox"][0])
    n = len(template)
    if n < 2:
        return set()
    slots = [(s["bbox"][0], s["bbox"][2]) for s in template]
    cells = [s for row in table for s in row]
    assigned: dict[str, int] = {}
    for s in cells:
        col = _assign_column(s["bbox"], slots)
        assigned[s["id"]] = col
        a, b = slots[col]
        slots[col] = (min(a, s["bbox"][0]), max(b, s["bbox"][2]))
    for s in cells:
        assigned[s["id"]] = _assign_column(s["bbox"], slots)
        a, b = slots[assigned[s["id"]]]
        slots[assigned[s["id"]]] = (min(a, s["bbox"][0]), max(b, s["bbox"][2]))
    left = min(a for a, _ in slots)
    right = max(b for _, b in slots)
    mirrored_slots = []
    for a, b in slots:
        m = _mirror_x([a, 0, b, 1], left, right)
        mirrored_slots.append((m[0], m[2]))

    ty0 = min(s["bbox"][1] for s in cells)
    ty1 = max(s["bbox"][3] for s in cells)
    cell_ids = {s["id"] for s in cells}
    extras: list[tuple[dict[str, Any], int]] = []
    for g in groups:
        if len(g) != 1 or g[0]["id"] in cell_ids:
            continue
        s = g[0]
        x0, y0, x1, y1 = s["bbox"]
        if y0 < ty0 - 2 or y1 > ty1 + 36 or x1 <= left or x0 >= right:
            continue
        width = max(x1 - x0, 1.0)
        if not any(min(x1, c["bbox"][2]) - max(x0, c["bbox"][0]) > 0.3 * width for c in cells):
            continue
        extras.append((s, _assign_column(s["bbox"], slots)))

    locked: set[str] = set()
    for s in cells:
        col = assigned[s["id"]]
        a, b = mirrored_slots[col]
        _, y0, _, y1 = s["bbox"]
        out[s["id"]] = [a, y0, b, y1]
        locked.add(s["id"])
    for s, col in extras:
        a, b = mirrored_slots[col]
        _, y0, _, y1 = s["bbox"]
        out[s["id"]] = [a, y0, b, y1]
        locked.add(s["id"])
    return locked


def rtl_mirror_plan(segs: list[dict[str, Any]]) -> tuple[dict[str, list[float]], set[str]]:
    out = {s["id"]: list(s["bbox"]) for s in segs}
    groups = _row_groups(segs)
    no_grow: set[str] = set()
    mirrored: set[str] = set()
    for table in _table_clusters(groups):
        if len(table) >= 2:
            locked = _mirror_table(table, groups, out)
            no_grow.update(locked)
            mirrored.update(locked)
            continue
        row = table[0]
        left = min(s["bbox"][0] for s in row)
        right = max(s["bbox"][2] for s in row)
        if right - left < 40:
            continue
        for s in row:
            out[s["id"]] = _mirror_x(s["bbox"], left, right)
            mirrored.add(s["id"])
    for g in groups:
        if len(g) < 2 or all(s["id"] in mirrored for s in g):
            continue
        left = min(s["bbox"][0] for s in g)
        right = max(s["bbox"][2] for s in g)
        if right - left < 40:
            continue
        for s in g:
            out[s["id"]] = _mirror_x(s["bbox"], left, right)
    return out, no_grow


def mirror_bboxes(segs: list[dict[str, Any]]) -> dict[str, list[float]]:
    boxes, _ = rtl_mirror_plan(segs)
    return boxes


def soft_join(text: str) -> str:
    lines = [ln.strip() for ln in text.split("\n")]
    lines = [ln for ln in lines if ln]
    if len(lines) <= 1:
        return text.replace("\n", " ").strip() if text.strip() else text
    if all(":" in ln for ln in lines):
        return "\n".join(lines)
    if (
        all(len(ln) <= 48 for ln in lines)
        and len(lines) <= 4
        and not any(ln[:1].islower() for ln in lines[1:])
        and sum(1 for ln in lines if ln[:1].isupper() or ln[:1].isdigit()) == len(lines)
        and not any(ln[:1] in "([+/" for ln in lines[1:])
    ):
        return "\n".join(lines)
    return " ".join(lines)


_SIGNED = re.compile(r"(?<![\w)\]])([-+\u2212])(?=[.,]?[0-9])")


_NASKH_UNMAPPED = {
    0x01BE: "\u0644",
    0x01C2: "\u0644",
    0x01C1: "\u0644",
    0x01C4: "\u0644",
    0x0006: "\u0627",
    0x0009: "\u0623",
    0x000C: "\u0625",
    0x0015: "\u0622",
    0x0628: "\u0627",
    0x062E: "\u0623",
    0x062F: "\u0625",
    0x062A: "\u0622",
}


def _font_xrefs(doc: pymupdf.Document) -> set[int]:
    return {x for x in range(1, doc.xref_length()) if doc.xref_get_key(x, "BaseFont")[0] == "name"}


def _complete_arabic_text_layer(doc: pymupdf.Document, source_fonts: set[int]) -> None:
    for xref in _font_xrefs(doc) - source_fonts:
        if "Noto Naskh Arabic" not in doc.xref_get_key(xref, "BaseFont")[1].replace("#20", " "):
            continue
        kind, value = doc.xref_get_key(xref, "ToUnicode")
        if kind != "xref":
            continue
        stream = int(value.split()[0])
        cmap = doc.xref_stream(stream).decode("latin-1")
        lines = "\n".join(
            f"<{gid:04x}> <{ord(ch):04x}>" for gid, ch in _NASKH_UNMAPPED.items() if not _mapped(cmap, gid)
        )
        if lines:
            added = f"{lines.count(chr(10)) + 1} beginbfchar\n{lines}\nendbfchar\nendcmap"
            doc.update_stream(stream, cmap.replace("endcmap", added, 1).encode("latin-1"))


def _mapped(cmap: str, gid: int) -> bool:
    for low, high in re.findall(r"^<([0-9a-fA-F]{4})> <([0-9a-fA-F]{4})> <", cmap, flags=re.MULTILINE):
        if int(low, 16) <= gid <= int(high, 16):
            return True
    return re.search(rf"^<{gid:04x}> <[0-9a-fA-F]+>$", cmap, flags=re.MULTILINE | re.IGNORECASE) is not None


def ltr_signs(text: str) -> str:
    return _SIGNED.sub("\u200e\\1", text)


def _html(text: str, bold_label: bool = False, bold: bool = False) -> str:
    text = soft_join(text)
    out = []
    for line in text.split("\n"):
        if bold_label and ":" in line:
            k, v = line.split(":", 1)
            out.append(f"<b>{html.escape(k)}:</b>{html.escape(v)}")
        else:
            out.append(html.escape(line))
    body = "<br>".join(out)
    if bold and not bold_label:
        body = f"<b>{body}</b>"
    return body


def _identity_siblings(page_segs: list[dict[str, Any]], translated_ids: set[str]) -> dict[str, str]:
    extra: dict[str, str] = {}
    for group in _row_groups(page_segs):
        if not any(s["id"] in translated_ids for s in group):
            continue
        for s in group:
            if s["id"] not in translated_ids and s.get("kind") != "glyph":
                extra[s["id"]] = s["text"]
    return extra


def _vertical_grids(page: pymupdf.Page) -> list[list[dict[str, Any]]]:
    lines = []
    for d in page.get_drawings():
        items = d.get("items") or []
        if d.get("type") != "s" or len(items) != 1 or items[0][0] != "l":
            continue
        p0, p1 = items[0][1], items[0][2]
        if abs(p0.x - p1.x) > 0.5 or abs(p0.y - p1.y) < 8:
            continue
        lines.append(
            {
                "x": (p0.x + p1.x) / 2,
                "y0": min(p0.y, p1.y),
                "y1": max(p0.y, p1.y),
                "color": d.get("color"),
                "width": d.get("width") or 1.0,
            }
        )
    grids: list[list[dict[str, Any]]] = []
    for ln in sorted(lines, key=lambda v: (round(v["y0"]), round(v["y1"]), v["x"])):
        for g in grids:
            if abs(g[0]["y0"] - ln["y0"]) <= 1 and abs(g[0]["y1"] - ln["y1"]) <= 1:
                g.append(ln)
                break
        else:
            grids.append([ln])
    return [sorted(g, key=lambda v: v["x"]) for g in grids if len(g) >= 3]


def mirror_grid_lines(page: pymupdf.Page) -> list[list[dict[str, Any]]]:
    moved: list[list[dict[str, Any]]] = []
    for grid in _vertical_grids(page):
        left, right = grid[0]["x"], grid[-1]["x"]
        inner = grid[1:-1]
        if not inner:
            continue
        for ln in inner:
            pad = max(ln["width"], 0.5)
            page.add_redact_annot(
                pymupdf.Rect(ln["x"] - pad, ln["y0"] - 0.5, ln["x"] + pad, ln["y1"] + 0.5), fill=False
            )
        page.apply_redactions(
            images=pymupdf.PDF_REDACT_IMAGE_NONE,
            graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
            text=pymupdf.PDF_REDACT_TEXT_NONE,
        )
        for ln in inner:
            x = left + right - ln["x"]
            page.draw_line((x, ln["y0"]), (x, ln["y1"]), color=ln["color"], width=ln["width"])
        moved.append(grid)
    return moved


def grid_cell_boxes(segs: list[dict[str, Any]], grids: list[list[dict[str, Any]]]) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {}
    for grid in grids:
        xs = [ln["x"] for ln in grid]
        left, right = xs[0], xs[-1]
        y0, y1 = grid[0]["y0"], grid[0]["y1"]
        for s in segs:
            bx0, by0, bx1, by1 = s["bbox"]
            cy, cx = (by0 + by1) / 2, (bx0 + bx1) / 2
            if not (y0 <= cy <= y1 and left <= cx <= right):
                continue
            col = next((i for i in range(len(xs) - 1) if xs[i] <= cx <= xs[i + 1]), None)
            if col is None or bx0 < xs[col] - 1 or bx1 > xs[col + 1] + 1:
                continue
            pad_l, pad_r = max(bx0 - xs[col], 0.0), max(xs[col + 1] - bx1, 0.0)
            m0, m1 = left + right - xs[col + 1], left + right - xs[col]
            out[s["id"]] = [m0 + min(pad_r, 4.0), by0, m1 - pad_l, by1]
    return out


def _write_at_floor(
    page: pymupdf.Page, rect: pymupdf.Rect, body: str, css: str, text: str
) -> tuple[bool, float, str]:
    bottom = page.rect.y1 - PAGE_MARGIN
    tries = [
        ("box", rect),
        ("taller", pymupdf.Rect(rect.x0, rect.y0, rect.x1, max(rect.y1, bottom))),
        (
            "wider",
            pymupdf.Rect(
                page.rect.x0 + PAGE_MARGIN, rect.y0, page.rect.x1 - PAGE_MARGIN, max(rect.y1, bottom)
            ),
        ),
    ]
    for mode, r in tries:
        spare, scale = page.insert_htmlbox(r, body, css=css, scale_low=MIN_SCALE)
        if spare >= 0:
            return mode == "box", scale, mode
    page.add_text_annot(rect.tl, text, icon="Note")
    return False, MIN_SCALE, "note"


def render(
    source: bytes,
    segments: list[dict[str, Any]],
    translations: dict[str, str],
    target_lang: str,
    pages: list[int] | None = None,
) -> tuple[bytes, dict[str, Any]]:
    doc = open_pdf(source)
    source_fonts = _font_xrefs(doc)
    rtl = target_lang in RTL
    by_page: dict[int, list[dict[str, Any]]] = {}
    for s in segments:
        if s["translatable"] and s["id"] in translations:
            by_page.setdefault(s["page"], []).append(s)

    all_by_page: dict[int, list[dict[str, Any]]] = {}
    for s in segments:
        all_by_page.setdefault(s["page"], []).append(s)

    identity: dict[str, str] = {}
    for page_no, segs in list(by_page.items()):
        extra = _identity_siblings(all_by_page[page_no], {s["id"] for s in segs})
        if not extra:
            continue
        identity.update(extra)
        known = {s["id"] for s in segs}
        for s in all_by_page[page_no]:
            if s["id"] in extra and s["id"] not in known:
                segs.append(s)
                known.add(s["id"])

    results = []
    for page_no, segs in by_page.items():
        page = doc[page_no]
        grids: list[list[dict[str, Any]]] = []
        if rtl and not any(s.get("scanned") for s in segs):
            grids = mirror_grid_lines(page)
        lines = _obstacles(page)
        if rtl:
            write_box, no_grow = rtl_mirror_plan(all_by_page[page_no])
            cells = grid_cell_boxes(all_by_page[page_no], grids)
            write_box.update(cells)
            no_grow = set(no_grow) | set(cells)
        else:
            write_box = {s["id"]: list(s["bbox"]) for s in all_by_page[page_no]}
            no_grow = set()
        rects = {
            s["id"]: available(
                pymupdf.Rect(write_box[s["id"]]),
                [pymupdf.Rect(write_box[o["id"]]) for o in all_by_page[page_no] if o["id"] != s["id"]],
                [ln for ln in lines if not pymupdf.Rect(write_box[s["id"]]).contains(ln)],
                page.rect,
                rtl=rtl,
                grow_x=s["id"] not in no_grow,
            )
            for s in segs
        }
        scanned = any(s.get("scanned") for s in segs)
        if scanned:
            pix = page.get_pixmap(dpi=72)
            for s in segs:
                r = pymupdf.Rect(s["bbox"]) + (-1, -1, 1, 1)
                page.add_redact_annot(r, fill=_paper(pix, r))
            page.apply_redactions(
                images=pymupdf.PDF_REDACT_IMAGE_PIXELS,
                graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                text=pymupdf.PDF_REDACT_TEXT_REMOVE,
            )
        else:
            for s in segs:
                page.add_redact_annot(pymupdf.Rect(s["bbox"]), fill=False)
            page.apply_redactions(
                images=pymupdf.PDF_REDACT_IMAGE_NONE,
                graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                text=pymupdf.PDF_REDACT_TEXT_REMOVE,
            )
        sizes = sorted(o["size"] for o in all_by_page[page_no])
        median = sizes[len(sizes) // 2] if sizes else 10.0
        for s in segs:
            rect = rects[s["id"]]
            size = s["size"]
            if s.get("scanned"):
                box = pymupdf.Rect(write_box[s["id"]])
                size = round(
                    min(size, box.height / max(s["lines"], 1) * 0.8, median * 1.2 if not s["bold"] else size),
                    2,
                )
            degree = "°" in (translations.get(s["id"]) or identity.get(s["id"]) or s["text"])
            lh = "1.0" if degree else "1.15"
            css = (
                f"* {{font-family: sans-serif; font-size: {size}pt; color: {s['color']};"
                f" line-height: {lh}; margin: 0; padding: 0;"
                f" font-weight: {'bold' if s['bold'] else 'normal'};"
                f" direction: {'rtl' if rtl else 'ltr'}; text-align: {'right' if rtl else 'left'};"
                f" vertical-align: baseline; }}"
            )
            text = translations.get(s["id"], identity.get(s["id"], s["text"]))
            drawn = ltr_signs(text) if rtl else text
            body = _html(drawn, s.get("bold_label", False), s.get("bold", False))
            fit, scale, mode = _write_at_floor(page, rect, body, css, text)
            results.append(
                {"id": s["id"], "page": page_no, "fit": fit, "scale": round(scale, 3), "mode": mode}
            )

    doc.subset_fonts()
    _complete_arabic_text_layer(doc, source_fonts)
    if pages is not None:
        keep = sorted({p - 1 for p in _page_set(doc.page_count, pages)})
        doc.select(keep)
    out = doc.tobytes(garbage=4, deflate=True, use_objstms=1)
    total = len(results)
    fitted = sum(1 for r in results if r["fit"])
    report = {
        "page_count_source": doc.page_count,
        "page_count_result": pymupdf.open(stream=out, filetype="pdf").page_count,
        "segments": total,
        "fit_ratio": round(fitted / total, 4) if total else 1.0,
        "min_scale": min((r["scale"] for r in results), default=1.0),
        "overflow": [r["id"] for r in results if not r["fit"]],
        "overflow_modes": {r["id"]: r["mode"] for r in results if not r["fit"]},
    }
    return out, report
