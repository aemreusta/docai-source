from __future__ import annotations

import io
import re
import warnings

import numpy as np
import pymupdf
from PIL import Image, ImageOps, UnidentifiedImageError

from . import ocr
from .layout import DocError

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:
    pillow_heif = None

MAX_PAGES = 30
MAX_PIXELS = 60_000_000
MAX_FILE_PIXELS = 120_000_000
DEFAULT_DPI = 200
FORMATS = {"JPEG", "PNG", "TIFF", "WEBP", "HEIF", "MPO"}
MAX_SKEW = 5.0

Image.MAX_IMAGE_PIXELS = MAX_PIXELS // 2


def kind(data: bytes) -> str | None:
    if data[:5] == b"%PDF-":
        return "pdf"
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[4:8] == b"ftyp" and data[8:12] in (b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1"):
        return "heic"
    return None


def frames(data: bytes) -> list[Image.Image]:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            img = Image.open(io.BytesIO(data))
    except Image.DecompressionBombError as e:
        raise DocError("image-too-large") from e
    except (UnidentifiedImageError, OSError, ValueError) as e:
        raise DocError("unsupported-image") from e
    if img.format not in FORMATS:
        raise DocError("unsupported-image")
    frame_count = getattr(img, "n_frames", 1)
    if frame_count > MAX_PAGES:
        raise DocError("too-many-pages")
    out = []
    total = 0
    for i in range(frame_count):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", Image.DecompressionBombWarning)
                img.seek(i)
                pixels = img.width * img.height
                total += pixels
                if pixels > MAX_PIXELS or total > MAX_FILE_PIXELS:
                    raise DocError("image-too-large")
                img.load()
                frame = ImageOps.exif_transpose(img)
        except Image.DecompressionBombError as e:
            raise DocError("image-too-large") from e
        except (UnidentifiedImageError, OSError, ValueError, EOFError) as e:
            raise DocError("unsupported-image") from e
        if frame.mode not in ("RGB", "L"):
            frame = (
                frame.convert("RGBA").convert("RGB")
                if "A" in frame.mode or frame.mode == "P"
                else frame.convert("RGB")
            )
        out.append(frame)
        if len(out) > MAX_PAGES:
            raise DocError("too-many-pages")
    return out


def osd_rotation(img: Image.Image) -> int:
    if "osd" not in ocr.installed_packs():
        return 0
    buf = io.BytesIO()
    small = img.convert("L")
    small.thumbnail((1600, 1600))
    small.save(buf, "PNG")
    out = ocr._run(buf.getvalue(), ["--psm", "0", "-l", "osd"])
    m = re.search(r"^Rotate:\s*(\d+)", out, re.MULTILINE)
    conf = re.search(r"^Orientation confidence:\s*([\d.]+)", out, re.MULTILINE)
    if not m or (conf and float(conf.group(1)) < 1.5):
        return 0
    deg = int(m.group(1)) % 360
    return deg if deg in (90, 180, 270) else 0


def skew_angle(img: Image.Image) -> float:
    g = img.convert("L")
    g.thumbnail((900, 900))
    a = np.asarray(g, dtype=np.float32)
    ink = (a < 128).astype(np.float32)
    if ink.mean() < 0.002:
        return 0.0
    base = Image.fromarray((ink * 255).astype(np.uint8))

    def score(angle: float) -> float:
        rotated = np.asarray(base.rotate(angle, resample=Image.BILINEAR, fillcolor=0), dtype=np.float32)
        return float(np.var(rotated.sum(axis=1)))

    best, best_s = 0.0, score(0.0)
    for step in (1.0, 0.25):
        center = best
        for k in np.arange(-4, 5):
            angle = center + k * step
            if abs(angle) > MAX_SKEW or angle == center:
                continue
            s = score(angle)
            if s > best_s * 1.0005:
                best, best_s = angle, s
    return round(best, 2)


def straighten(img: Image.Image) -> Image.Image:
    rot = osd_rotation(img)
    if rot:
        img = img.rotate(-rot, expand=True)
    angle = skew_angle(img)
    if abs(angle) >= 0.3:
        fill = 255 if img.mode == "L" else (255, 255, 255)
        img = img.rotate(angle, resample=Image.BICUBIC, expand=True, fillcolor=fill)
    return img


def dpi_of(img: Image.Image) -> float:
    dpi = img.info.get("dpi")
    try:
        x = float(dpi[0]) if dpi else 0.0
    except (TypeError, ValueError, IndexError):
        x = 0.0
    return x if 72 <= x <= 1200 else DEFAULT_DPI


def to_pdf(files: list[bytes]) -> tuple[bytes, int]:
    if not files:
        raise DocError("no-images")
    doc = pymupdf.open()
    for data in files:
        if kind(data) not in ("jpeg", "png", "tiff", "webp", "heic"):
            raise DocError("unsupported-image")
        for frame in frames(data):
            dpi = dpi_of(frame)
            page_img = straighten(frame)
            w_pt, h_pt = page_img.width * 72 / dpi, page_img.height * 72 / dpi
            buf = io.BytesIO()
            if page_img.mode == "L":
                page_img.save(buf, "PNG", optimize=True)
            else:
                page_img.save(buf, "JPEG", quality=88)
            page = doc.new_page(width=w_pt, height=h_pt)
            page.insert_image(page.rect, stream=buf.getvalue())
            if doc.page_count > MAX_PAGES:
                raise DocError("too-many-pages")
    return doc.tobytes(garbage=3, deflate=True), doc.page_count
