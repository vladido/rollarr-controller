from dataclasses import dataclass
import re


@dataclass(frozen=True)
class EpisodeSpan:
    season: int
    episodes: tuple[int, ...]
    declared_total: int | None = None

    @property
    def complete(self) -> bool:
        return self.declared_total is not None and self.episodes[-1] >= self.declared_total


def inventory_from_paths(paths: list[str]) -> dict[int, tuple[int, ...]]:
    video = {".mkv", ".mp4", ".avi", ".m4v", ".ts"}
    found: dict[int, set[int]] = {}
    pattern = re.compile(r"(?i)S(?P<season>\d{1,2})[ ._-]*E(?P<first>\d{1,3})(?:\s*[-–]\s*E?(?P<last>\d{1,3}))?")
    for path in paths:
        if not any(path.lower().endswith(ext) for ext in video):
            continue
        for match in pattern.finditer(path):
            season = int(match["season"])
            first = int(match["first"])
            last = int(match["last"] or first)
            if last < first:
                continue
            found.setdefault(season, set()).update(range(first, last + 1))
    return {season: tuple(sorted(episodes)) for season, episodes in found.items()}


def parse_episode_span(text: str) -> EpisodeSpan:
    match = re.search(
        r"(?i)S(?P<season>\d{1,2})E(?P<first>\d{1,3})\s*[-–]\s*E?(?P<last>\d{1,3})(?:\s+of\s+(?P<total>\d{1,3}))?",
        text,
    )
    if not match:
        raise ValueError("no episode span")
    first, last = int(match["first"]), int(match["last"])
    if last < first:
        raise ValueError("reversed episode span")
    return EpisodeSpan(
        season=int(match["season"]),
        episodes=tuple(range(first, last + 1)),
        declared_total=int(match["total"]) if match["total"] else None,
    )
