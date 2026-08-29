"""Shared deterministic ordering for official CGTrader preview candidates."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class PreviewCandidate:
    url: str
    score: tuple[int, int, int]
    reason: str


class SharedPreviewSelector:
    """Choose official preview URLs without treating URL order as evidence."""

    def rank(self, candidates: list[str] | tuple[str, ...]) -> list[PreviewCandidate]:
        output: list[PreviewCandidate] = []
        seen: set[str] = set()
        for raw in candidates:
            url = str(raw or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            parsed = urlsplit(url)
            host = (parsed.hostname or "").casefold().removeprefix("www.")
            if parsed.scheme != "https" or host not in {"img-new.cgtrader.com", "www.cgtrader.com", "cgtrader.com"}:
                continue
            path = parsed.path.casefold()
            is_thumbnail = any(token in path for token in ("thumb", "thumbnail", "small", "_sm"))
            is_embed = "embed" in path or "avatar" in path
            is_original = any(token in path for token in ("/items/", "/models/", "/product/"))
            score = (2 if is_original else 0, 0 if is_thumbnail else 1, 0 if is_embed else 1)
            reason = "official_original_preview" if is_original and not is_thumbnail else "official_preview_fallback"
            output.append(PreviewCandidate(url=url, score=score, reason=reason))
        return sorted(output, key=lambda item: (-item.score[0], -item.score[1], -item.score[2], item.url))

    def select(self, candidates: list[str] | tuple[str, ...], *, limit: int = 3) -> list[PreviewCandidate]:
        return self.rank(candidates)[: max(1, int(limit))]


__all__ = ["PreviewCandidate", "SharedPreviewSelector"]
