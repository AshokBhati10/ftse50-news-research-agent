from __future__ import annotations

import re

from .models import Citation, Passage
from .urls import normalize_url

CITE_RE = re.compile(r"\[(\d{1,3})\]")


class CitationTracker:
    """Gives each source URL one stable number for the whole report and checks the LLM's citations."""

    def __init__(self) -> None:
        self._by_url: dict[str, Citation] = {}

    def number_for(self, p: Passage) -> int:
        key = normalize_url(p.url)
        if key not in self._by_url:
            self._by_url[key] = Citation(number=len(self._by_url) + 1, url=p.url, title=p.title or p.url,
                                         source=p.source, date=p.date)
        return self._by_url[key].number

    def context_block(self, passages: list[Passage], max_chars_per_passage: int = 1800) -> str:
        """Numbered evidence for the synthesis prompt. Several passages may share one source number."""
        lines = []
        for p in passages:
            n = self.number_for(p)
            date = f", {p.date}" if p.date else ""
            kind = ", search snippet only" if p.kind == "snippet" else ""
            lines.append(f"[{n}] {p.title} ({p.source}{date}{kind})\n{p.text[:max_chars_per_passage]}")
        return "\n\n---\n\n".join(lines)

    def validate(self, answer: str) -> tuple[str, list[Citation]]:
        """Drops citations to unknown numbers and returns only the sources actually cited."""
        valid = {c.number for c in self._by_url.values()}
        cleaned = CITE_RE.sub(lambda m: m.group(0) if int(m.group(1)) in valid else "", answer)
        used = {int(n) for n in CITE_RE.findall(cleaned)}
        cited = sorted((c for c in self._by_url.values() if c.number in used), key=lambda c: c.number)
        return cleaned, cited

    def consulted(self) -> list[Citation]:
        """Every source that was shown to the model (whether or not it cited it)."""
        return sorted(self._by_url.values(), key=lambda c: c.number)

    @staticmethod
    def references_markdown(cited: list[Citation], heading: str = "Sources") -> str:
        rows = [f"[{c.number}] {c.title}. {c.source}" + (f", {c.date}" if c.date else "") + f". {c.url}"
                for c in cited]
        return f"\n\n## {heading}\n\n" + "\n".join(f"- {r}" for r in rows) if rows else ""
