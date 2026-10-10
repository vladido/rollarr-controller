from dataclasses import dataclass
import re
import unicodedata


@dataclass(frozen=True)
class SeriesIdentity:
    title: str
    aliases: tuple[str, ...]
    expected_year: int


def _normal(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold().replace("ё", "е")
    return " ".join(re.findall(r"[\w]+", text, re.UNICODE))


def title_matches_series(release_title: str, series: SeriesIdentity) -> bool:
    years = {int(value) for value in re.findall(r"(?<!\d)(?:19|20)\d{2}(?!\d)", release_title)}
    allowed_years = {series.expected_year, series.expected_year - 1}
    if not years.intersection(allowed_years):
        return False
    head = re.split(r"(?i)(?:^|[ /._-])S\d{1,2}E", release_title, maxsplit=1)[0]
    segments = {_normal(part) for part in head.split("/")}
    aliases = {_normal(series.title), *(_normal(alias) for alias in series.aliases)}
    return bool((segments - {""}) & (aliases - {""}))


def normalized_release_title(release_title: str, series: SeriesIdentity) -> str:
    """Replace a verified compound tracker title with Sonarr's canonical title."""
    if not title_matches_series(release_title, series):
        raise ValueError("release identity is not verified")
    episode = re.search(r"(?i)(?<![\w])S\d{1,2}E\d{1,3}", release_title)
    if not episode:
        raise ValueError("release has no season/episode marker")
    return f"{series.title} {release_title[episode.start():]}"
