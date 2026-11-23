"""Per-turn evidence registered by retrieval tools and rendered by the server."""
import re
from contextvars import ContextVar
from datetime import date
from urllib.parse import quote

from app.core.config import get_settings
from app.schemas.chat import Citation

evidence: ContextVar[dict[str, Citation] | None] = ContextVar("evidence", default=None)
_REFERENCE = re.compile(r"\[S(\d+)\]")
_ABSTENTION = (
    "I couldn’t verify an answer from the available official BIS sources. "
    "Share the product model, standard number, or official notice so I can narrow the search."
)


def register_evidence(source: str, title: str, **metadata) -> str:
    current = evidence.get()
    if current is None or not get_settings().is_domain_allowed(source):
        return ""
    effective = metadata.get("effective_date")
    if effective:
        try:
            if date.fromisoformat(effective) > date.today():
                return ""
        except (TypeError, ValueError):
            return ""
    citation = Citation(url=source, title=title, **metadata)
    for reference, existing in current.items():
        if citation == existing:
            return reference
    reference = f"S{len(current) + 1}"
    current[reference] = citation
    return reference


def render_grounded_answer(text: str, sources: dict[str, Citation]) -> tuple[str, list[Citation]]:
    """Accept known citation IDs on every answer block and render their URLs server-side."""
    blocks = [block.strip() for block in text.split("\n") if block.strip()]
    if not blocks or any(not _REFERENCE.search(block) for block in blocks):
        return _ABSTENTION, []
    used = {}
    for match in _REFERENCE.finditer(text):
        ref = f"S{match[1]}"
        if ref not in sources:
            return _ABSTENTION, []
        used[ref] = sources[ref]

    def link(match: re.Match) -> str:
        ref = f"S{match[1]}"
        url = quote(used[ref].url, safe=":/?=&#%+;,@!$'*~_-")
        return f"[{ref}]({url})"

    # The renderer creates the only clickable links shown in generated guidance.
    text = text.replace("\\", "\\\\")
    raw = _REFERENCE.split(text)
    for index in range(0, len(raw), 2):
        raw[index] = raw[index].replace("[", r"\[").replace("]", r"\]").replace("<", "&lt;")
    escaped = "".join(part if index % 2 == 0 else f"[S{part}]" for index, part in enumerate(raw))
    return _REFERENCE.sub(link, escaped), list(used.values())
