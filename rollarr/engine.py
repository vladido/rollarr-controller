from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import json
import os

from .decisions import Candidate, Policy, evaluate
from .identity import SeriesIdentity, normalized_release_title, title_matches_series
from .inventory import inventory_from_paths
from .metainfo import parse_metainfo
from .topics import topic_identity



@dataclass(frozen=True)
class Config:
    state_dir: Path
    dry_run: bool = True
    min_seeders: int = 1
    max_bytes_per_episode: int = 12_000_000_000
    category: str = "tv-sonarr"
    targets: tuple[tuple[int, int, str, str], ...] = ()
    root_folder: str = ""
    quality_profile_id: int = 0


_BENIGN_PREFIXES = (
    "Existing file on disk has a equal or higher Custom Format score",
    "Existing file on disk has an equal or higher Custom Format score",
    "Release in queue has an equal or higher Custom Format score",
    "Release in queue has a equal or higher Custom Format score",
    "Full season pack",
    # Sonarr cannot estimate size when metadata has zero runtime. Rollarr
    # independently validates downloaded metainfo inventory and byte totals.
    "Runtime of all episodes is 0, unable to validate size until it is available",
)


def _reason_text(value) -> str:
    return value.get("reason", "") if isinstance(value, dict) else str(value)


def hard_rejections(values, identity_verified: bool) -> tuple[str, ...]:
    hard = []
    for value in values or ():
        reason = _reason_text(value)
        if any(reason.startswith(prefix) for prefix in _BENIGN_PREFIXES):
            continue
        if reason == "Unknown Series" and identity_verified:
            continue
        hard.append(reason)
    return tuple(hard)


class Engine:
    def __init__(self, config: Config, sonarr, qbit, ledger):
        self.config, self.sonarr, self.qbit, self.ledger = config, sonarr, qbit, ledger
        self.config.state_dir.mkdir(parents=True, exist_ok=True)

    def _audit(self, event: dict) -> None:
        event = {"at": datetime.now(timezone.utc).isoformat(), **event}
        path = self.config.state_dir / "audit.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        os.chmod(path, 0o600)

    def _save_metainfo(self, infohash: str, data: bytes) -> None:
        directory = self.config.state_dir / "torrents"
        directory.mkdir(mode=0o700, exist_ok=True)
        path = directory / f"{infohash}.torrent"
        path.write_bytes(data); os.chmod(path, 0o600)

    @staticmethod
    def _subject(title) -> dict[str, str]:
        if not isinstance(title, str) or not title.strip():
            return {}
        return {"subject": title.strip()}

    def _series_subject(self, series_id: int) -> dict[str, str]:
        try:
            title = self.sonarr.series(series_id).get("title")
        except Exception:
            return {}
        return self._subject(title)

    def _recover_pending(self) -> None:
        audit = self.config.state_dir / "audit.jsonl"
        torrents = self.config.state_dir / "torrents"
        if not audit.exists() or not torrents.exists():
            return
        pending_hashes = {row.infohash for row in self.ledger.pending()}
        for line in audit.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
                if event.get("event") not in {"grab_submitted", "update_submitted"}:
                    continue
                prefix = str(event["hash"])
                matches = list(torrents.glob(f"{prefix}*.torrent"))
                if len(matches) != 1:
                    continue
                meta = parse_metainfo(matches[0].read_bytes())
                if not meta.infohash.startswith(prefix) or meta.infohash in pending_hashes:
                    continue
                season = int(event["season"]); endpoint = int(event["endpoint"])
                inventories = inventory_from_paths([path for path, _ in meta.files])
                if set(inventories) != {season} or max(inventories[season]) != endpoint:
                    continue
                tracker, topic = str(event["topic"]).split(":", 1)
                state = self.ledger.topic_state(tracker, topic)
                if state and state.endpoint >= endpoint:
                    continue
                self.ledger.record_pending(tracker, topic, int(event["series_id"]), season,
                                           endpoint, meta.infohash, inventories[season])
                pending_hashes.add(meta.infohash)
                subject = self._subject(event.get("subject")) or self._series_subject(
                    int(event["series_id"]))
                self._audit({"event": "pending_recovered", "series_id": int(event["series_id"]),
                             "season": season, "topic": event["topic"],
                             "endpoint": endpoint, "hash": meta.infohash[:12],
                             **subject})
            except (KeyError, ValueError, TypeError, json.JSONDecodeError):
                continue

    @staticmethod
    def _series_identity(series: dict, expected_year: int) -> SeriesIdentity:
        aliases = []
        if series.get("originalTitle"):
            aliases.append(series["originalTitle"])
        for value in series.get("alternateTitles", []):
            title = value.get("title") if isinstance(value, dict) else str(value)
            if title:
                aliases.append(title)
        return SeriesIdentity(series["title"], tuple(dict.fromkeys(aliases)), expected_year)

    def scan(self, *, now: datetime | None = None) -> dict[str, int]:
        now = now or datetime.now(timezone.utc)
        summary = {key: 0 for key in ("series_added", "groups", "candidates", "rejected", "adopted",
                                      "proposed_grab", "grabbed", "proposed_update", "updated", "confirmed")}
        self._recover_pending()
        for pending in self.ledger.pending():
            imported = {row["episodeNumber"] for row in self.sonarr.episodes(pending.series_id)
                        if row["seasonNumber"] == pending.season and row.get("hasFile")}
            if not set(pending.episodes).issubset(imported):
                continue
            state = self.ledger.topic_state(pending.tracker, pending.topic)
            if not state or pending.endpoint > state.endpoint:
                self.ledger.record_acceptance(
                    pending.tracker, pending.topic, pending.series_id, pending.season,
                    pending.endpoint, pending.infohash)
            self.ledger.clear_pending(pending.infohash)
            summary["confirmed"] += 1
            self._audit({"event": "import_confirmed", "series_id": pending.series_id,
                         "season": pending.season,
                         "topic": f"{pending.tracker}:{pending.topic}",
                         "endpoint": pending.endpoint, "hash": pending.infohash[:12],
                         **self._series_subject(pending.series_id)})
        target_topics = {}
        if self.config.targets:
            current = {int(row["tvdbId"]): row for row in self.sonarr.all_series()}
            for tvdb_id, season, tracker, topic in self.config.targets:
                series = current.get(tvdb_id)
                if series is None:
                    self._audit({"event": "target_skipped", "tvdb_id": tvdb_id,
                                 "season": season, "reason": "series_absent"})
                    continue
                if series.get("monitored") is False:
                    self._audit({"event": "target_skipped", "tvdb_id": tvdb_id,
                                 "season": season, "reason": "series_unmonitored",
                                 **self._subject(series.get("title"))})
                    continue
                target_topics[(int(series["id"]), season)] = (tracker, topic)
        missing = []
        for episode in self.sonarr.missing().get("records", []):
            air = episode.get("airDateUtc")
            aired = not air or datetime.fromisoformat(air.replace("Z", "+00:00")) <= now
            if episode.get("monitored") and not episode.get("hasFile") and aired:
                missing.append(episode)
        groups: dict[tuple[int, int], list[dict]] = {}
        for episode in missing:
            groups.setdefault((episode["seriesId"], episode["seasonNumber"]), []).append(episode)
        # Explicitly watched rolling topics must also cover episodes whose
        # upstream metadata has no air date; Sonarr omits those from Wanted.
        for series_id, season in target_topics:
            watched_missing = []
            for row in self.sonarr.episodes(series_id):
                if row["seasonNumber"] == season and row.get("monitored") and not row.get("hasFile"):
                    watched_missing.append({**row, "seriesId": series_id})
            if watched_missing:
                groups[(series_id, season)] = watched_missing
        summary["groups"] = len(groups)
        summary["searched"] = 0
        summary["search_failed"] = 0
        last_search_error = None
        qrows = self.qbit.torrents()
        qby_hash = {row["hash"].lower(): row for row in qrows}
        seen = self.ledger.seen_hashes() | set(qby_hash)
        policy = Policy(self.config.min_seeders, self.config.max_bytes_per_episode, frozenset())

        for (series_id, season), wanted in groups.items():
            first_missing = min(row["episodeNumber"] for row in wanted)
            series = self.sonarr.series(series_id)
            if series.get("monitored") is False:
                self._audit({"event": "group_skipped", "series_id": series_id,
                             "season": season, "reason": "series_unmonitored",
                             **self._subject(series.get("title"))})
                continue
            dated = [row["airDateUtc"] for row in wanted if row.get("airDateUtc")]
            expected_year = (datetime.fromisoformat(min(dated).replace("Z", "+00:00")).year
                             if dated else int(series.get("year") or now.year))
            identity = self._series_identity(series, expected_year)
            episode_rows = [row for row in self.sonarr.episodes(series_id)
                            if row["seasonNumber"] == season]
            local_episodes = {row["episodeNumber"]: row["id"] for row in episode_rows}
            imported_episodes = {row["episodeNumber"] for row in episode_rows if row.get("hasFile")}
            choices = []
            adopted_current = False
            try:
                releases = self.sonarr.releases(series_id, season)
            except TimeoutError as exc:
                summary["search_failed"] += 1
                last_search_error = exc
                self._audit({"event": "release_search_failed", "series_id": series_id,
                             "season": season, "error": type(exc).__name__})
                continue
            summary["searched"] += 1
            for release in releases:
                topic = topic_identity((release.get("infoUrl") or "") + " " + (release.get("guid") or ""))
                if not topic or not release.get("downloadUrl"):
                    continue
                required_topic = target_topics.get((series_id, season))
                if required_topic and topic != required_topic:
                    continue
                verified = title_matches_series(release.get("title", ""), identity)
                hard = hard_rejections(release.get("rejections"), verified)
                seeders = int(release.get("seeders") or 0)
                if not verified or hard or seeders < self.config.min_seeders:
                    summary["rejected"] += 1
                    if not verified:
                        rejection_reason = "identity_mismatch"
                    elif hard:
                        rejection_reason = "hard_rejection"
                    else:
                        rejection_reason = "too_few_seeders"
                    self._audit({"event": "candidate_rejected", "series_id": series_id,
                                 "season": season, "reason": rejection_reason})
                    continue
                normalized_title = normalized_release_title(release["title"], identity)
                parsed = self.sonarr.parse(normalized_title)
                parsed_series_id = int((parsed.get("series") or {}).get("id") or 0)
                parsed_episodes = {
                    int(row["episodeNumber"])
                    for row in parsed.get("episodes", [])
                    if int(row.get("seasonNumber") or -1) == season
                }
                if parsed_series_id != series_id or not parsed_episodes:
                    summary["rejected"] += 1
                    self._audit({"event": "candidate_rejected", "series_id": series_id,
                                 "season": season, "reason": "sonarr_parse_mismatch"})
                    continue
                release = {**release, "title": normalized_title}
                try:
                    meta = parse_metainfo(self.sonarr.download(release["downloadUrl"]))
                except Exception as exc:
                    self._audit({"event": "torrent_probe_failed", "series_id": series_id,
                                 "season": season, "error": type(exc).__name__})
                    continue
                inventories = inventory_from_paths([path for path, _ in meta.files])
                if set(inventories) != {season}:
                    summary["rejected"] += 1
                    continue
                episodes = inventories[season]
                if not set(episodes).issubset(parsed_episodes):
                    summary["rejected"] += 1
                    self._audit({"event": "candidate_rejected", "series_id": series_id,
                                 "season": season, "reason": "title_inventory_mismatch"})
                    continue
                topic_key = f"{topic[0]}:{topic[1]}"
                candidate = Candidate(series_id, season, episodes, meta.infohash, topic_key,
                                      int(release.get("seeders") or 0), int(release.get("size") or sum(x[1] for x in meta.files)),
                                      verified, hard)
                state = self.ledger.topic_state(*topic)
                endpoint = state.endpoint if state else first_missing - 1
                qcurrent = qby_hash.get(meta.infohash)
                if qcurrent and verified and not hard:
                    actual_endpoint = max(episodes)
                    if set(episodes).issubset(imported_episodes) and (not state or actual_endpoint > state.endpoint):
                        self.ledger.record_acceptance(topic[0], topic[1], series_id, season,
                                                      actual_endpoint, meta.infohash)
                        if not self.config.dry_run:
                            self._save_metainfo(meta.infohash, meta.raw)
                        summary["adopted"] += 1
                    elif not set(episodes).issubset(imported_episodes):
                        self._audit({"event": "waiting_for_import", "series_id": series_id,
                                     "season": season, "topic": f"{topic[0]}:{topic[1]}",
                                     "endpoint": actual_endpoint, "hash": meta.infohash[:12]})
                    if actual_endpoint >= first_missing:
                        adopted_current = True
                    continue
                decision = evaluate(candidate, series_id=series_id, season=season,
                                    first_missing=first_missing, current_endpoint=endpoint,
                                    seen_hashes=seen - ({meta.infohash} if meta.infohash in qby_hash else set()), policy=policy)
                summary["candidates"] += 1
                if decision.accepted:
                    choices.append((max(episodes), int(release.get("qualityWeight") or 0),
                                    int(release.get("customFormatScore") or 0), state is not None,
                                    int(release.get("releaseWeight") or 0), candidate.seeders,
                                    -candidate.size, release, meta, topic, state, local_episodes))
                else:
                    summary["rejected"] += 1
                    self._audit({"event": "candidate_rejected", "series_id": series_id,
                                 "season": season, "hash": meta.infohash[:12], "reason": decision.reason})
            if adopted_current:
                continue
            if not choices:
                continue
            endpoint, _, _, _, _, _, _, release, meta, topic, state, episode_ids = max(
                choices, key=lambda x: x[:7])
            action = "grab" if state is None else "update"
            if self.config.dry_run:
                self._audit({"event": f"proposed_{action}", "series_id": series_id,
                             "season": season, "topic": f"{topic[0]}:{topic[1]}",
                             "endpoint": endpoint, "hash": meta.infohash[:12]})
                summary[f"proposed_{action}"] += 1
                continue
            ids = tuple(episode_ids[number] for number in inventory_from_paths([p for p, _ in meta.files])[season]
                        if number in episode_ids)
            candidate_episodes = inventory_from_paths([p for p, _ in meta.files])[season]
            self.sonarr.grab(release, series_id=series_id, episode_ids=ids)
            self.ledger.record_pending(topic[0], topic[1], series_id, season,
                                       endpoint, meta.infohash, candidate_episodes)
            if not self.qbit.wait_exists(meta.infohash):
                raise RuntimeError("Sonarr grab did not reach qBittorrent")
            self._save_metainfo(meta.infohash, meta.raw)
            self._audit({"event": f"{action}_submitted", "series_id": series_id,
                         "season": season, "topic": f"{topic[0]}:{topic[1]}",
                         "endpoint": endpoint, "hash": meta.infohash[:12],
                         "subject": series["title"]})
            summary["grabbed" if state is None else "updated"] += 1
        if last_search_error is not None and not summary["searched"]:
            raise last_search_error
        self._audit({"event": "scan_complete", "dry_run": self.config.dry_run, **summary})
        return summary
