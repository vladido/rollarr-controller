from dataclasses import dataclass


@dataclass(frozen=True)
class Candidate:
    series_id: int
    season: int
    episodes: tuple[int, ...]
    infohash: str
    topic: str
    seeders: int
    size: int
    identity_verified: bool
    rejections: tuple[str, ...] = ()


@dataclass(frozen=True)
class Policy:
    min_seeders: int = 1
    max_bytes_per_episode: int = 8_000_000_000
    allowed_rejections: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Decision:
    accepted: bool
    reason: str


def evaluate(candidate: Candidate, *, series_id: int, season: int, first_missing: int,
             current_endpoint: int, seen_hashes: set[str], policy: Policy) -> Decision:
    if candidate.series_id != series_id:
        return Decision(False, "wrong_series")
    if candidate.season != season:
        return Decision(False, "wrong_season")
    if first_missing not in candidate.episodes:
        return Decision(False, "missing_not_covered")
    if not candidate.episodes or max(candidate.episodes) <= current_endpoint:
        return Decision(False, "not_growing")
    if candidate.infohash in seen_hashes:
        return Decision(False, "seen_hash")
    if candidate.seeders < policy.min_seeders:
        return Decision(False, "too_few_seeders")
    if candidate.size > policy.max_bytes_per_episode * len(candidate.episodes):
        return Decision(False, "too_large")
    if any(reason not in policy.allowed_rejections for reason in candidate.rejections):
        return Decision(False, "hard_rejection")
    if not candidate.identity_verified:
        return Decision(False, "identity_mismatch")
    if not candidate.topic:
        return Decision(False, "missing_topic")
    return Decision(True, "growing_pack")
