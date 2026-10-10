from dataclasses import dataclass
from .metainfo import Metainfo


@dataclass(frozen=True)
class ExistingTorrent:
    infohash: str
    topic: str
    root: str
    progress: float
    files: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class UpdatePlan:
    safe: bool
    reason: str
    reused_bytes: int = 0
    download_bytes: int = 0


def plan_update(old: ExistingTorrent, candidate_topic: str, candidate: Metainfo) -> UpdatePlan:
    if candidate_topic != old.topic:
        return UpdatePlan(False, "wrong_topic")
    if old.progress != 1.0:
        return UpdatePlan(False, "source_incomplete")
    if candidate.name != old.root:
        return UpdatePlan(False, "changed_root")
    before = dict(old.files)
    after = dict(candidate.files)
    if any(path not in after or after[path] != size for path, size in before.items()):
        return UpdatePlan(False, "changed_existing_file")
    added = {path: size for path, size in after.items() if path not in before}
    if not added:
        return UpdatePlan(False, "no_new_files")
    return UpdatePlan(True, "append_only", sum(before.values()), sum(added.values()))
