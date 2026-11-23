"""
app/data/chunking.py
───────────────────
Shared chunking, category detection, and text cleaning for RAG.
Used by scripts/prepare_data.py and scripts/crawl_bis_sites.py.
"""
from __future__ import annotations

import re

# ── Chunking config ───────────────────────────────────────────────────────────

DEFAULT_CHUNK_SIZE = 500
DEFAULT_CHUNK_OVERLAP = 80

# ── Category detection ──────────────────────────────────────────────────────

CATEGORY_RULES: list[tuple[list[str], str]] = [
    (["hallmark", "huid", "gold", "jewellery", "jewelry", "ahc"], "hallmarking"),
    (["crs", "r-number", "electronics", "it product", "mobile", "laptop", "led"], "crs"),
    (["isi mark", "cm/l", "cml", "compulsory", "mandatory", "product certification"], "marks"),
    (["fmcs", "foreign manufacturer", "import"], "fmcs"),
    (["bee", "star rating", "energy efficiency", "bee label"], "energy"),
    (["lpg", "gas cylinder", "regulator", "petroleum"], "safety"),
    (["cable", "wire", "electrical", "mcb", "socket", "plug"], "electrical"),
    (["cement", "construction", "steel", "building material"], "construction"),
    (["toy", "helmet", "child", "children"], "safety"),
    (["certification process", "how to apply", "license", "application fee", "factory audit"], "compliance"),
    (["is standard", "indian standard", "is number", "bureau of indian standards"], "standards"),
]

IS_NUMBER_RE = re.compile(r"\bIS[:\s]?(\d{3,6})\b", re.IGNORECASE)

TAG_RULES: dict[str, list[str]] = {
    "hallmarking": ["hallmark", "huid", "gold", "jewellery"],
    "crs": ["crs", "r-number", "electronics", "registration"],
    "marks": ["isi", "cml", "certification", "mark"],
    "compliance": ["certification", "process", "apply", "license"],
    "electrical": ["cable", "electrical", "mcb", "switch"],
    "safety": ["safety", "mandatory", "critical"],
    "fmcs": ["fmcs", "foreign", "import"],
    "energy": ["bee", "star rating", "energy"],
    "standards": ["is standard", "indian standard"],
    "construction": ["cement", "steel", "construction"],
}


def detect_category(text: str) -> str:
    text_lower = text.lower()
    for keywords, category in CATEGORY_RULES:
        if any(kw in text_lower for kw in keywords):
            return category
    return "general"


def detect_tags(text: str, category: str) -> list[str]:
    tags = TAG_RULES.get(category, []).copy()
    for m in IS_NUMBER_RE.finditer(text):
        tag = f"IS {m.group(1)}"
        if tag not in tags:
            tags.append(tag)
    return tags[:8]


def extract_is_number(text: str) -> str | None:
    m = IS_NUMBER_RE.search(text)
    return f"IS {m.group(1)}" if m else None


def clean_text(text: str) -> str:
    """Remove noise, normalize whitespace."""
    text = re.sub(r"Page\s+\d+\s+of\s+\d+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    lines = [l for l in text.splitlines() if len(l.strip()) > 3 or l.strip() == ""]
    text = "\n".join(lines)
    return text.strip()


def chunk_text(
    text: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> list[str]:
    """Split text into overlapping chunks (paragraph/sentence aware)."""
    paragraphs = [p.strip() for p in re.split(r"\n\n+", text) if p.strip()]
    chunks: list[str] = []
    current = ""

    for para in paragraphs:
        if len(current) + len(para) + 2 <= chunk_size:
            current = (current + "\n\n" + para).strip()
        else:
            if current:
                chunks.append(current)
            if len(para) > chunk_size:
                sentences = re.split(r"(?<=[.!?])\s+", para)
                sub = ""
                for sent in sentences:
                    if len(sub) + len(sent) + 1 <= chunk_size:
                        sub = (sub + " " + sent).strip()
                    else:
                        if sub:
                            chunks.append(sub)
                        sub = sent
                if sub:
                    chunks.append(sub)
                current = chunks[-1][-overlap:] if chunks else ""
            else:
                overlap_text = current[-overlap:] if current else ""
                current = (overlap_text + "\n\n" + para).strip()

    if current:
        chunks.append(current)
    return [c for c in chunks if len(c) >= 80]
