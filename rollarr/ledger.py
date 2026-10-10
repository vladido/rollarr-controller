from dataclasses import dataclass
from pathlib import Path
from contextlib import contextmanager
import json, sqlite3
import os


@dataclass(frozen=True)
class TopicState:
    tracker: str
    topic: str
    series_id: int
    season: int
    endpoint: int
    infohash: str


@dataclass(frozen=True)
class PendingSubmission:
    tracker: str
    topic: str
    series_id: int
    season: int
    endpoint: int
    infohash: str
    episodes: tuple[int, ...]


class Ledger:
    def __init__(self, path: str | Path):
        self.path = str(path)
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS topics (
                    tracker TEXT NOT NULL, topic TEXT NOT NULL,
                    series_id INTEGER NOT NULL, season INTEGER NOT NULL,
                    endpoint INTEGER NOT NULL, infohash TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (tracker, topic)
                );
                CREATE TABLE IF NOT EXISTS seen_hashes (
                    infohash TEXT PRIMARY KEY,
                    seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS pending_submissions (
                    infohash TEXT PRIMARY KEY,
                    tracker TEXT NOT NULL, topic TEXT NOT NULL,
                    series_id INTEGER NOT NULL, season INTEGER NOT NULL,
                    endpoint INTEGER NOT NULL, episodes TEXT NOT NULL,
                    submitted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
            """)
        os.chmod(self.path, 0o600)

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def record_acceptance(self, tracker: str, topic: str, series_id: int,
                          season: int, endpoint: int, infohash: str) -> None:
        with self._connect() as db:
            row = db.execute("SELECT endpoint FROM topics WHERE tracker=? AND topic=?", (tracker, topic)).fetchone()
            if row and endpoint <= row["endpoint"]:
                raise ValueError("endpoint must grow")
            db.execute("INSERT OR IGNORE INTO seen_hashes(infohash) VALUES (?)", (infohash,))
            db.execute("""
                INSERT INTO topics(tracker,topic,series_id,season,endpoint,infohash)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(tracker,topic) DO UPDATE SET
                    series_id=excluded.series_id, season=excluded.season,
                    endpoint=excluded.endpoint, infohash=excluded.infohash,
                    updated_at=CURRENT_TIMESTAMP
            """, (tracker, topic, series_id, season, endpoint, infohash))

    def topic_state(self, tracker: str, topic: str) -> TopicState | None:
        with self._connect() as db:
            row = db.execute("SELECT tracker,topic,series_id,season,endpoint,infohash FROM topics WHERE tracker=? AND topic=?", (tracker, topic)).fetchone()
        return TopicState(**dict(row)) if row else None

    def states_for_series(self, series_id: int, season: int) -> list[TopicState]:
        with self._connect() as db:
            rows = db.execute("SELECT tracker,topic,series_id,season,endpoint,infohash FROM topics WHERE series_id=? AND season=? ORDER BY updated_at DESC", (series_id, season)).fetchall()
        return [TopicState(**dict(row)) for row in rows]

    def seen_hashes(self) -> set[str]:
        with self._connect() as db:
            return {row[0] for row in db.execute("SELECT infohash FROM seen_hashes")}

    def record_pending(self, tracker: str, topic: str, series_id: int, season: int,
                       endpoint: int, infohash: str, episodes: tuple[int, ...]) -> None:
        with self._connect() as db:
            db.execute("""
                INSERT OR REPLACE INTO pending_submissions
                    (infohash,tracker,topic,series_id,season,endpoint,episodes)
                VALUES (?,?,?,?,?,?,?)
            """, (infohash, tracker, topic, series_id, season, endpoint,
                  json.dumps(list(episodes))))

    def pending(self) -> list[PendingSubmission]:
        with self._connect() as db:
            rows = db.execute("""
                SELECT tracker,topic,series_id,season,endpoint,infohash,episodes
                FROM pending_submissions ORDER BY submitted_at
            """).fetchall()
        return [PendingSubmission(
            tracker=row["tracker"], topic=row["topic"], series_id=row["series_id"],
            season=row["season"], endpoint=row["endpoint"], infohash=row["infohash"],
            episodes=tuple(json.loads(row["episodes"]))) for row in rows]

    def clear_pending(self, infohash: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM pending_submissions WHERE infohash=?", (infohash,))
