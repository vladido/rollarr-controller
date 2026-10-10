from .metainfo import Metainfo


def execute_safe_update(qbit, candidate: Metainfo, save_path: str,
                        category: str, tags: tuple[str, ...]) -> None:
    qbit.add_stopped(candidate.raw, save_path, category, tags)
    if not qbit.wait_exists(candidate.infohash):
        raise RuntimeError("added torrent did not appear")
    qbit.recheck(candidate.infohash)
    qbit.start(candidate.infohash)
