from __future__ import annotations

from importlib import resources
from pathlib import Path

from .models import TranscriptSegment


DEFAULT_REPLACEMENTS = {
    "雨傘": "雨伞",
    "傘面": "伞面",
    "傘骨": "伞骨",
    "PoE": "POE",
    "pOE": "POE",
    "pEo": "PEO",
}


def load_default_terms() -> list[str]:
    try:
        text = resources.files("autocut.resources").joinpath("brand_terms.txt").read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    return parse_terms(text)


def load_terms_file(path: Path | None) -> list[str]:
    if not path:
        return []
    return parse_terms(path.read_text(encoding="utf-8-sig"))


def parse_terms(text: str) -> list[str]:
    terms: list[str] = []
    for line in text.splitlines():
        term = line.strip()
        if not term or term.startswith("#"):
            continue
        terms.append(term)
    return terms


def build_asr_terms(
    product: str,
    selling_points: list[str],
    extra_terms: list[str] | None = None,
    include_defaults: bool = True,
) -> list[str]:
    terms: list[str] = []
    if include_defaults:
        terms.extend(load_default_terms())
    if product:
        terms.append(product)
    terms.extend(point for point in selling_points if point)
    terms.extend(extra_terms or [])

    expanded: list[str] = []
    for term in terms:
        expanded.append(term)
        expanded.extend(expand_term(term))
    return dedupe_terms(expanded)


def expand_term(term: str) -> list[str]:
    expanded: list[str] = []
    if "伞" in term:
        expanded.append(term.replace("伞", "傘"))
    if "傘" in term:
        expanded.append(term.replace("傘", "伞"))
    upper = term.upper()
    if "POE" in upper:
        expanded.append(term.upper().replace("POE", "PEO"))
    if "PEO" in upper:
        expanded.append(term.upper().replace("PEO", "POE"))
    if "哈利波特" in term:
        expanded.extend(["Harry Potter", "霍格沃茨"])
    return expanded


def dedupe_terms(terms: list[str]) -> list[str]:
    seen = set()
    result: list[str] = []
    for term in terms:
        normalized = term.strip()
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result


def terms_to_prompt(terms: list[str]) -> str:
    if not terms:
        return ""
    selected = []
    total = 0
    for term in terms:
        if len(selected) >= 10:
            break
        total += len(term)
        if total > 55:
            break
        selected.append(term)
    joined = "、".join(selected)
    return f"热词：{joined}。"


def terms_to_hotwords(terms: list[str]) -> str:
    selected = []
    total = 0
    for term in terms:
        if len(selected) >= 16:
            break
        total += len(term)
        if total > 90:
            break
        selected.append(term)
    return " ".join(selected)


def normalize_transcript_segments(segments: list[TranscriptSegment]) -> list[TranscriptSegment]:
    for segment in segments:
        segment.text = normalize_text(segment.text)
        for word in segment.words:
            if "word" in word:
                word["word"] = normalize_text(str(word["word"]))
    return segments


def normalize_text(text: str) -> str:
    for source, target in DEFAULT_REPLACEMENTS.items():
        text = text.replace(source, target)
    return text
