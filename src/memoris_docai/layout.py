from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Any

import pymupdf

from . import ocr

MAX_PAGES = 30


class DocError(Exception):

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def open_pdf(data: bytes) -> pymupdf.Document:
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as e:
        raise DocError("not-a-pdf") from e
    if doc.needs_pass:
        raise DocError("encrypted")
    if doc.page_count == 0:
        raise DocError("empty")
    if doc.page_count > MAX_PAGES:
        raise DocError("too-many-pages")
    return doc


def _color(c: int) -> str:
    return f"#{c:06x}"


_ICON_FONT = re.compile(
    r"awesome|dingbat|wingding|webding|segoe.?ui.?symbol|material.?(icons|symbols)|icomoon|glyphicon|"
    r"academicons|remixicon|bootstrap.?icons|ionicons|octicons|feather|entypo|typicons",
    re.IGNORECASE,
)


def _private_use(c: str) -> bool:
    o = ord(c)
    return 0xE000 <= o <= 0xF8FF or o >= 0xF0000


def _char_width(ch: dict[str, Any]) -> float:
    box = ch["bbox"]
    return box[2] - box[0]


def _piled_label(chars: list[dict[str, Any]], start: int) -> int:
    if _char_width(chars[start]) <= 0.2 or not chars[start]["c"].isalpha():
        return 0
    edge = chars[start]["bbox"][2]
    end = start + 1
    while end < len(chars) and chars[end]["c"].isalpha() and _char_width(chars[end]) <= 0.2:
        box = chars[end]["bbox"]
        if abs(box[0] - edge) > 1 and abs(box[2] - edge) > 1:
            break
        end += 1
    if end >= start + 3:
        return end - start
    return 0


def _span_from(src: dict[str, Any], chars: list[dict[str, Any]], icon: bool) -> dict[str, Any]:
    box = [
        min(c["bbox"][0] for c in chars),
        min(c["bbox"][1] for c in chars),
        max(c["bbox"][2] for c in chars),
        max(c["bbox"][3] for c in chars),
    ]
    return {
        "text": "".join(c["c"] for c in chars),
        "bbox": box,
        "font": src.get("font") or "",
        "size": round(src["size"], 2),
        "baseline": chars[0]["origin"][1],
        "color": _color(src["color"]),
        "bold": bool(src["flags"] & 16),
        "italic": bool(src["flags"] & 2),
        "icon": icon,
    }


def _pieces(src: dict[str, Any]) -> list[dict[str, Any]]:
    chars = [c for c in src.get("chars") or [] if c["c"]]
    if not chars:
        return []
    if _ICON_FONT.search(src.get("font") or ""):
        return [_span_from(src, chars, True)] if "".join(c["c"] for c in chars).strip() else []
    out: list[dict[str, Any]] = []
    run: list[dict[str, Any]] = []

    def flush() -> None:
        if run and "".join(c["c"] for c in run).strip():
            out.append(_span_from(src, run.copy(), False))
        run.clear()

    i = 0
    while i < len(chars):
        piled = _piled_label(chars, i)
        if piled:
            flush()
            out.append(_span_from(src, chars[i : i + piled], True))
            i += piled
            continue
        if _private_use(chars[i]["c"]):
            flush()
            j = i
            while j < len(chars) and _private_use(chars[j]["c"]):
                j += 1
            out.append(_span_from(src, chars[i:j], True))
            i = j
            continue
        run.append(chars[i])
        i += 1
    flush()
    return out


def _lines_from(ln: dict[str, Any]) -> list[dict[str, Any]]:
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []

    def flush() -> None:
        nonlocal current
        if current:
            groups.append(current)
            current = []

    for src in ln["spans"]:
        for piece in _pieces(src):
            if piece["icon"]:
                flush()
                groups.append([piece])
                continue
            if current and piece["bbox"][0] > current[-1]["bbox"][2] + 1:
                flush()
            current.append(piece)
    flush()
    return [
        {
            "bbox": [
                min(s["bbox"][0] for s in spans),
                min(s["bbox"][1] for s in spans),
                max(s["bbox"][2] for s in spans),
                max(s["bbox"][3] for s in spans),
            ],
            "dir": list(ln["dir"]),
            "spans": spans,
        }
        for spans in groups
    ]


def extract(data: bytes, source_lang: str | None = None, pages: list[int] | None = None) -> dict[str, Any]:
    doc = open_pdf(data)
    wanted = _page_set(doc.page_count, pages)
    pages_out = []
    choice: ocr.Choice | None = None
    for page in doc:
        if (page.number + 1) not in wanted:
            continue
        textpage = None
        scanned = not page.get_text("text").strip()
        conf: float | None = None
        if scanned:
            sample = page.get_pixmap(dpi=150, colorspace=pymupdf.csGRAY).tobytes("png")
            if choice is None:
                choice = ocr.choose(source_lang, sample)
            textpage = _ocr_page(page, choice.packs, 300)
            if textpage is not None:
                conf, expected = ocr.measure(sample, choice.packs)
                got = _word_count(page, textpage)
                for dpi in ocr.RETRY_DPI:
                    if expected == 0 or got >= ocr.MIN_COVERAGE * expected:
                        break
                    retry = _ocr_page(page, choice.packs, dpi)
                    n = _word_count(page, retry) if retry is not None else 0
                    if n > got:
                        textpage, got = retry, n
        d = page.get_text(
            "rawdict", textpage=textpage, flags=pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES
        )
        blocks = []
        for b in d["blocks"]:
            if b.get("type") != 0:
                continue
            lines = []
            for ln in b["lines"]:
                lines.extend(_lines_from(ln))
            if textpage is not None:
                lines = _join_ocr_words(lines)
            if lines:
                blocks.append({"bbox": list(b["bbox"]), "lines": lines})
        out = {
            "number": page.number,
            "width": page.rect.width,
            "height": page.rect.height,
            "scanned": scanned,
            "needs_ocr": scanned and textpage is None,
            "blocks": blocks,
        }
        if scanned and textpage is not None:
            out["ocr_confidence"] = conf
            out["ocr_low_confidence"] = conf is None or conf < ocr.min_confidence() or not blocks
        pages_out.append(out)
    result: dict[str, Any] = {"version": 1, "page_count": len(pages_out), "pages": pages_out}
    if choice is not None:
        result["ocr"] = {"language": choice.language, "packs": choice.packs, "source": choice.source}
    return result


_RTL = re.compile(r"[\u0590-\u08ff\ufb1d-\ufdff\ufe70-\ufeff]")


def _join_ocr_words(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(lines) < 2:
        return lines

    def center(ln):
        return (ln["bbox"][1] + ln["bbox"][3]) / 2

    def height(ln):
        return max(ln["bbox"][3] - ln["bbox"][1], 1.0)

    def joinable(ln):
        return all(not s.get("icon") for s in ln["spans"]) and abs(ln["dir"][0] - 1) < 0.01

    rows: list[list[dict[str, Any]]] = []
    for ln in sorted(lines, key=lambda x: (center(x), x["bbox"][0])):
        if rows and joinable(ln) and all(joinable(r) for r in rows[-1]):
            ref = rows[-1][0]
            if abs(center(ln) - center(ref)) < 0.5 * min(height(ln), height(ref)):
                rows[-1].append(ln)
                continue
        rows.append([ln])
    out = []
    for row in rows:
        if len(row) == 1:
            out.append(row[0])
            continue
        text = "".join(s["text"] for ln in row for s in ln["spans"])
        rtl = len(_RTL.findall(text)) > len(text) / 3
        row.sort(key=lambda x: x["bbox"][0], reverse=rtl)
        spans = []
        for i, ln in enumerate(row):
            for s in ln["spans"]:
                spans.append(dict(s))
            if i < len(row) - 1 and spans and not spans[-1]["text"].endswith(" "):
                spans[-1]["text"] += " "
        out.append(
            {
                "bbox": [
                    min(ln["bbox"][0] for ln in row),
                    min(ln["bbox"][1] for ln in row),
                    max(ln["bbox"][2] for ln in row),
                    max(ln["bbox"][3] for ln in row),
                ],
                "dir": row[0]["dir"],
                "spans": spans,
            }
        )
    return out


def _ocr_page(page: pymupdf.Page, packs: str, dpi: int):
    try:
        return page.get_textpage_ocr(language=packs, dpi=dpi, full=True)
    except Exception:
        return None


def _word_count(page: pymupdf.Page, textpage) -> int:
    return len(page.get_text("text", textpage=textpage).split())


def _page_set(page_count: int, pages: list[int] | None) -> set[int]:
    if pages is None:
        return set(range(1, page_count + 1))
    wanted = set(pages)
    if not wanted or any(n < 1 or n > page_count for n in wanted):
        raise DocError("invalid-pages")
    return wanted


def _dominant(spans: list[dict[str, Any]], key: str) -> Any:
    weight: dict[Any, int] = {}
    for s in spans:
        weight[s[key]] = weight.get(s[key], 0) + len(s["text"])
    return max(weight, key=weight.get)


def _side_by_side(a: list[float], b: list[float]) -> bool:
    overlap = min(a[3], b[3]) - max(a[1], b[1])
    h = min(a[3] - a[1], b[3] - b[1])
    return h > 0 and overlap > 0.5 * h and (b[0] >= a[2] - 1 or a[0] >= b[2] - 1)


def _units(block: dict[str, Any]) -> list[list[dict[str, Any]]]:
    lines = block["lines"]
    if any(_side_by_side(a["bbox"], b["bbox"]) for i, a in enumerate(lines) for b in lines[i + 1 :]):
        return [[ln] for ln in lines]
    units = []
    previous = None
    for ln in lines:
        label = _bold_label([ln])
        style = (_dominant(ln["spans"], "bold") and not label, label)
        if style != previous:
            units.append([])
        units[-1].append(ln)
        previous = style
    return units


def _bold_label(lines: list[dict[str, Any]]) -> bool:
    for ln in lines:
        chars = [(c, s["bold"]) for s in ln["spans"] for c in s["text"]]
        colon = next((i for i, (c, _) in enumerate(chars) if c == ":"), -1)
        prefix, suffix = chars[:colon], chars[colon + 1 :]
        if (
            colon < 0
            or not any(not c.isspace() for c, _ in prefix)
            or not all(bold or c.isspace() for c, bold in prefix)
            or not any(not c.isspace() for c, _ in suffix)
            or not all(not bold or c.isspace() for c, bold in suffix)
        ):
            return False
    return True


def _bbox(lines: list[dict[str, Any]]) -> list[float]:
    return [
        min(ln["bbox"][0] for ln in lines),
        min(ln["bbox"][1] for ln in lines),
        max(ln["bbox"][2] for ln in lines),
        max(ln["bbox"][3] for ln in lines),
    ]


def _is_icon(span: dict[str, Any]) -> bool:
    if "icon" in span:
        return bool(span["icon"])
    chars = [c for c in span["text"] if not c.isspace()]
    return bool(chars) and (
        bool(_ICON_FONT.search(span.get("font", ""))) or all(_private_use(c) for c in chars)
    )


def _span_bbox(spans: list[dict[str, Any]]) -> list[float]:
    return [
        min(s["bbox"][0] for s in spans),
        min(s["bbox"][1] for s in spans),
        max(s["bbox"][2] for s in spans),
        max(s["bbox"][3] for s in spans),
    ]


def _value_bearing(ch: str) -> bool:
    if ch.isascii():
        return False
    form = unicodedata.decomposition(ch)
    if form.startswith("<square>"):
        return any(c.isdigit() for c in unicodedata.normalize("NFKC", ch))
    return ch == "\u00b5" or form.startswith(("<super>", "<sub>", "<fraction>"))


def fold_forms(text: str) -> str:
    out: list[str] = []
    start = 0
    for i, ch in enumerate(text):
        if _value_bearing(ch):
            out.append(unicodedata.normalize("NFKC", text[start:i]))
            out.append(ch)
            start = i + 1
    out.append(unicodedata.normalize("NFKC", text[start:]))
    return "".join(out)


_SCRIPT_TEXT = re.compile(r"[+\-\u2212]?\d+")
_SUPERSCRIPT = str.maketrans("0123456789+-\u2212", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁻")
_SUBSCRIPT = str.maketrans("0123456789+-\u2212", "₀₁₂₃₄₅₆₇₈₉₊₋₋")


def script_kind(base_size: float, base_y: float, size: float, y: float, text: str) -> str | None:
    if size >= base_size * 0.9 or not _SCRIPT_TEXT.fullmatch(text.strip()):
        return None
    if y < base_y - base_size * 0.1:
        return "sup"
    if y > base_y + base_size * 0.1:
        return "sub"
    return None


def _join_spans(spans: list[dict[str, Any]]) -> str:
    out = ""
    prev = ""
    base: dict[str, Any] | None = None
    for span in spans:
        raw = span["text"]
        text = raw.strip()
        kind = None
        if base and text and "baseline" in span and "baseline" in base:
            kind = script_kind(base["size"], base["baseline"], span["size"], span["baseline"], text)
        if kind:
            out += text.translate(_SUPERSCRIPT if kind == "sup" else _SUBSCRIPT)
            prev = raw
            continue
        if text:
            base = span
        if out and text:
            glued = not prev[-1:].isspace() and not raw[:1].isspace()
            if not (glued and (_value_bearing(out[-1]) or _value_bearing(text[0]))):
                out += " "
        out += text
        if text:
            prev = raw
    return out


def segment(layout: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for p in layout["pages"]:
        for i, b in enumerate(p["blocks"]):
            for u, lines in enumerate(_units(b)):
                icons = [s for ln in lines for s in ln["spans"] if _is_icon(s)]
                text_lines = [{**ln, "spans": [s for s in ln["spans"] if not _is_icon(s)]} for ln in lines]
                text_lines = [ln for ln in text_lines if ln["spans"]]
                for k, s in enumerate(icons):
                    h = hashlib.sha256((s["font"] + s["text"]).encode()).hexdigest()[:8]
                    out.append(
                        {
                            "id": f"p{p['number']}-b{i}-{u}-g{k}-{h}",
                            "page": p["number"],
                            "bbox": list(s["bbox"]),
                            "text": s["text"],
                            "size": s["size"],
                            "color": s["color"],
                            "bold": False,
                            "lines": 1,
                            "scanned": p["scanned"],
                            "bold_label": False,
                            "kind": "glyph",
                            "translatable": False,
                        }
                    )
                if not text_lines:
                    continue
                spans = [s for ln in text_lines for s in ln["spans"]]
                text = "\n".join(_join_spans(ln["spans"]) for ln in text_lines)
                text = fold_forms(text)
                h = hashlib.sha256(text.encode()).hexdigest()[:8]
                horizontal = all(abs(ln["dir"][1]) < 1e-3 for ln in text_lines)
                out.append(
                    {
                        "id": f"p{p['number']}-b{i}-{u}-{h}",
                        "page": p["number"],
                        "bbox": _span_bbox(spans) if icons else _bbox(text_lines),
                        "text": text,
                        "size": _dominant(spans, "size"),
                        "color": _dominant(spans, "color"),
                        "bold": _dominant(spans, "bold") and not _bold_label(text_lines),
                        "lines": len(text_lines),
                        "scanned": p["scanned"],
                        "bold_label": _bold_label(text_lines),
                        "translatable": horizontal and _needs_translation(text),
                    }
                )
    return out


_DECIMAL_COMMA = re.compile(r"\d,\d")


_CONTACT = re.compile(
    r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"
    r"|(?:https?://|www\.)\S+"
    r"|\b[\w-]+(?:\.[\w-]+)*\.(?:com|org|net|edu|gov|io|dev|info|eu|de|tr|uk|fr|nl|ch|at)\b(?:/\S*)?",
    re.IGNORECASE,
)


def _needs_translation(text: str) -> bool:
    words = _CONTACT.sub(" ", text)
    return any(c.isalpha() for c in words) or bool(_DECIMAL_COMMA.search(text))
