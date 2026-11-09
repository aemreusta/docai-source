from __future__ import annotations

import functools
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass

REGISTRY: dict[str, str] = {
    "tr": "tur",
    "en": "eng",
    "de": "deu",
    "ar": "ara",
    "fr": "fra",
    "ru": "rus",
    "es": "spa",
    "it": "ita",
    "nl": "nld",
}
DEFAULT_LANGUAGES = "tr,en,de,ar,fr,ru"
FALLBACK = "tur+eng"
TIMEOUT_S = 60


def tesseract() -> str | None:
    return shutil.which("tesseract")


@functools.lru_cache(maxsize=1)
def installed_packs() -> frozenset[str]:
    exe = tesseract()
    if not exe:
        return frozenset()
    try:
        out = subprocess.run(
            [exe, "--list-langs"], capture_output=True, text=True, timeout=TIMEOUT_S, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    lines = (out.stdout + out.stderr).splitlines()
    return frozenset(ln.strip() for ln in lines if re.fullmatch(r"[a-z_]{3,}", ln.strip()))


def supported() -> dict[str, str]:
    wanted = [
        c.strip() for c in os.environ.get("DOCAI_OCR_LANGUAGES", DEFAULT_LANGUAGES).split(",") if c.strip()
    ]
    packs = installed_packs()
    return {c: REGISTRY[c] for c in wanted if c in REGISTRY and REGISTRY[c] in packs}


def min_confidence() -> float:
    try:
        return float(os.environ.get("DOCAI_OCR_MIN_CONFIDENCE", "60"))
    except ValueError:
        return 60.0


def packs_for(lang: str) -> str:
    sup = supported()
    pack = sup.get(lang)
    if not pack:
        return FALLBACK
    if lang != "en" and "en" in sup:
        return f"{pack}+eng"
    return pack


@dataclass
class Choice:
    language: str | None
    packs: str
    source: str


def normalize_hint(hint: str | None) -> str | None:
    if not hint:
        return None
    h = hint.strip().lower().split("-")[0]
    return h if re.fullmatch(r"[a-z]{2}", h) and h != "au" else None


def choose(hint: str | None, sample_png: bytes | None) -> Choice:
    sup = supported()
    lang = normalize_hint(hint)
    if lang and lang in sup:
        return Choice(lang, packs_for(lang), "hint")
    if sample_png is not None and sup:
        detected = detect(sample_png, sup)
        if detected:
            return Choice(detected, packs_for(detected), "detected")
    return Choice(None, FALLBACK if {"tur", "eng"} <= installed_packs() else "eng", "fallback")


def _run(png: bytes, args: list[str]) -> str:
    exe = tesseract()
    if not exe:
        return ""
    with tempfile.NamedTemporaryFile(suffix=".png") as f:
        f.write(png)
        f.flush()
        try:
            out = subprocess.run(
                [exe, f.name, "stdout", *args], capture_output=True, text=True, timeout=TIMEOUT_S, check=False
            )
        except (OSError, subprocess.SubprocessError):
            return ""
    return out.stdout


def script(png: bytes) -> str | None:
    if "osd" not in installed_packs():
        return None
    m = re.search(r"^Script:\s*(\S+)", _run(png, ["--psm", "0", "-l", "osd"]), re.MULTILINE)
    return m.group(1) if m else None


_LATIN_CUES: dict[str, tuple[str, set[str]]] = {
    "tr": ("çğışöüİ", {"ve", "bir", "bu", "için", "ile", "olarak", "hasta", "günde", "tablet", "değil"}),
    "de": ("äöüß", {"und", "der", "die", "das", "nicht", "mit", "für", "ist", "zweimal", "täglich"}),
    "fr": ("éèêàçùâîô", {"le", "la", "les", "et", "des", "pour", "est", "une", "deux", "fois"}),
    "en": ("", {"the", "and", "of", "for", "with", "is", "daily", "twice", "patient", "tablet"}),
}


def guess_latin(text: str, candidates: set[str]) -> str | None:
    words = re.findall(r"[^\W\d_]+", text.lower())
    if len(words) < 3:
        return None
    best, best_score = None, 0.0
    for lang, (letters, stop) in _LATIN_CUES.items():
        if lang not in candidates:
            continue
        score = sum(2.0 for w in words if w in stop) + sum(1.5 for ch in text if ch in letters)
        if score > best_score:
            best, best_score = lang, score
    return best if best_score >= 2 else None


def detect(png: bytes, sup: dict[str, str]) -> str | None:
    sc = script(png)
    if sc == "Arabic":
        return "ar" if "ar" in sup else None
    if sc == "Cyrillic":
        return "ru" if "ru" in sup else None
    latin = [c for c in ("tr", "de", "fr", "en") if c in sup]
    if not latin:
        return None
    text = _run(png, ["--psm", "3", "-l", "+".join(sup[c] for c in latin)])
    return guess_latin(text, set(latin))


def measure(png: bytes, packs: str) -> tuple[float | None, int]:
    tsv = _run(png, ["--psm", "3", "-l", packs, "tsv"])
    confs = []
    for row in tsv.splitlines()[1:]:
        cols = row.split("\t")
        if len(cols) == 12 and cols[11].strip():
            try:
                c = float(cols[10])
            except ValueError:
                continue
            if c >= 0:
                confs.append(c)
    if not confs:
        return None, 0
    return round(sum(confs) / len(confs), 1), len(confs)


def confidence(png: bytes, packs: str) -> float | None:
    return measure(png, packs)[0]


RETRY_DPI = (200, 150)
MIN_COVERAGE = 0.6
