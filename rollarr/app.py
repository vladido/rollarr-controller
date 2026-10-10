import json
import hmac
import os
import queue
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .api import QbitClient, SonarrClient
from .engine import Config, Engine
from .ledger import Ledger


def _required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _boolean(name: str, default: str) -> bool:
    value = os.environ.get(name, default).strip().lower()
    if value not in {"true", "false"}:
        raise ValueError(f"{name} must be true or false")
    return value == "true"


def config_from_env():
    targets = []
    for item in filter(None, os.environ.get("ROLLARR_TARGETS", "").split(",")):
        tvdb, season, tracker, topic = item.split(":", 3)
        targets.append((int(tvdb), int(season), tracker, topic))
    config = Config(
        state_dir=Path(os.environ.get("ROLLARR_STATE_DIR", "/state")),
        dry_run=_boolean("ROLLARR_DRY_RUN", "true"),
        min_seeders=int(os.environ.get("ROLLARR_MIN_SEEDERS", "1")),
        max_bytes_per_episode=int(os.environ.get("ROLLARR_MAX_BYTES_PER_EPISODE", "12000000000")),
        category=os.environ.get("ROLLARR_CATEGORY", "tv-sonarr"),
        targets=tuple(targets),
        root_folder=os.environ.get("ROLLARR_ROOT_FOLDER", ""),
        quality_profile_id=int(os.environ.get("ROLLARR_QUALITY_PROFILE_ID", "0")),
    )
    endpoints = {
        "sonarr_url": _required("SONARR_URL"), "sonarr_key": _required("SONARR_API_KEY"),
        "qbit_url": _required("QBIT_URL"), "qbit_key": _required("QBIT_API_KEY"),
    }
    return config, endpoints


class ControlState:
    def __init__(self, *, dry_run: bool, state_dir: Path, token: str | None,
                 series_title=None):
        self.dry_run = dry_run
        self.state_dir = state_dir
        self.token = token
        self.series_title = series_title
        self.running = False
        self.queued = False
        self.last_run = None
        self.last_success = None
        self.last_success_at = None
        self.last_error = None
        self._lock = threading.Lock()

    def start_run(self, run_id: str, source: str, started_at: str) -> None:
        with self._lock:
            self.running = True
            self.queued = False
            self.last_run = {"run_id": run_id, "source": source,
                             "started_at": started_at, "finished_at": None,
                             "summary": None, "error": None}

    def set_queued(self, queued: bool) -> None:
        with self._lock:
            self.queued = queued

    def finish_run(self, summary: dict | None, error: Exception | None,
                   finished_at: str) -> None:
        with self._lock:
            self.running = False
            self.last_run["finished_at"] = finished_at
            self.last_run["summary"] = summary
            self.last_run["error"] = type(error).__name__ if error else None
            if error:
                self.last_error = type(error).__name__
            else:
                self.last_success = time.time()
                self.last_success_at = finished_at
                self.last_error = None

    def status(self) -> dict:
        with self._lock:
            return {
                "service": "rollarr",
                "version": __version__,
                "dry_run": self.dry_run,
                "running": self.running,
                "queued": self.queued,
                "capabilities": {"run_now": self.token is not None},
                "last_run": self.last_run.copy() if self.last_run else None,
                "last_success_at": self.last_success_at,
                "last_error": self.last_error,
            }


class RunCoordinator:
    def __init__(self, engine, state: ControlState, *, idempotency_ttl: float = 300,
                 idempotency_max_entries: int = 1024, clock=time.monotonic):
        self.engine = engine
        self.state = state
        self._queue = queue.Queue(maxsize=1)
        self._lock = threading.Lock()
        self._idempotency = {}
        self._stopping = threading.Event()
        self._thread = None
        self.last_exception = None
        self._idempotency_ttl = idempotency_ttl
        self._idempotency_max_entries = max(2, idempotency_max_entries)
        self._clock = clock

    def _prune_idempotency(self) -> None:
        now = self._clock()
        expired = [key for key, entry in self._idempotency.items()
                   if entry["finished_at"] is not None
                   and now - entry["finished_at"] >= self._idempotency_ttl]
        for key in expired:
            del self._idempotency[key]
        while len(self._idempotency) >= self._idempotency_max_entries:
            completed = [(entry["finished_at"], key)
                         for key, entry in self._idempotency.items()
                         if entry["finished_at"] is not None]
            if not completed:
                break
            del self._idempotency[min(completed)[1]]

    def submit(self, source: str, idempotency_key: str | None = None) -> tuple[bool, str | None, str | None]:
        with self._lock:
            self._prune_idempotency()
            if idempotency_key and idempotency_key in self._idempotency:
                entry = self._idempotency[idempotency_key]
                return True, entry["run_id"], entry["status"]
            run_id = uuid.uuid4().hex
            try:
                self._queue.put_nowait((run_id, source, idempotency_key))
            except queue.Full:
                return False, None, None
            if idempotency_key:
                self._idempotency[idempotency_key] = {
                    "run_id": run_id, "status": "queued", "finished_at": None}
            self.state.set_queued(True)
            return True, run_id, "queued"

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._work, daemon=True, name="rollarr-worker")
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def join(self) -> None:
        self._queue.join()

    def _work(self) -> None:
        while not self._stopping.is_set():
            try:
                run_id, source, idempotency_key = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            with self._lock:
                if idempotency_key:
                    self._idempotency[idempotency_key]["status"] = "running"
                self.state.start_run(run_id, source, _now())
                self.state.set_queued(not self._queue.empty())
            terminal_status = "completed"
            try:
                summary = self.engine.scan()
                self.last_exception = None
                self.state.finish_run(summary, None, _now())
                print(json.dumps({"event": "scan_complete", "run_id": run_id,
                                  "source": source, "dry_run": self.state.dry_run,
                                  **summary}), flush=True)
            except Exception as exc:
                terminal_status = "failed"
                self.last_exception = exc
                self.state.finish_run(None, exc, _now())
                print(json.dumps({"event": "scan_failed", "run_id": run_id,
                                  "source": source, "error": type(exc).__name__}), flush=True)
            finally:
                with self._lock:
                    if idempotency_key:
                        self._idempotency[idempotency_key]["status"] = terminal_status
                        self._idempotency[idempotency_key]["finished_at"] = self._clock()
                self._queue.task_done()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_handler(state: ControlState, coordinator):
    class ControlHandler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass

        def do_GET(self):
            request = urlsplit(self.path)
            if request.path == "/live":
                self._json(200, {"ok": True})
                return
            if request.path == "/v1/status":
                self._json(200, state.status())
                return
            if request.path == "/v1/health":
                body = state.status()
                body["ok"] = body["last_error"] is None
                self._json(200 if body["ok"] else 503, body)
                return
            if request.path == "/health":
                body = {"ok": state.last_error is None,
                        "last_success": state.last_success,
                        "last_error": state.last_error}
                self._json(200 if body["ok"] else 503, body)
                return
            if request.path == "/v1/audit":
                query = parse_qs(request.query)
                try:
                    limit = int(query.get("limit", ["50"])[0])
                except ValueError:
                    self._json(400, {"error": "invalid_limit"})
                    return
                limit = min(200, max(1, limit))
                self._json(200, {"records": _read_audit(
                    state.state_dir, limit, state.series_title)})
                return
            else:
                self.send_response(404); self.end_headers(); return

        def do_POST(self):
            if urlsplit(self.path).path != "/v1/runs" or state.token is None:
                self._json(404, {"error": "not_found"})
                return
            authorization = self.headers.get("Authorization", "")
            expected = "Bearer " + state.token
            if not hmac.compare_digest(authorization.encode("utf-8"), expected.encode("utf-8")):
                self._json(401, {"error": "unauthorized"})
                return
            key = self.headers.get("Idempotency-Key")
            if key is not None and (not key or len(key) > 200):
                self._json(400, {"error": "invalid_idempotency_key"})
                return
            accepted, run_id, run_status = coordinator.submit("manual", key)
            if not accepted:
                self._json(409, {"error": "run_queue_full"})
                return
            self._json(202, {"run_id": run_id, "status": run_status})

        def _json(self, status, value):
            body = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return ControlHandler


_AUDIT_FIELDS = frozenset({
    "at", "event", "series_id", "tvdb_id", "season", "reason", "dry_run",
    "series_added", "groups", "candidates", "rejected", "adopted",
    "proposed_grab", "grabbed", "proposed_update", "updated", "confirmed",
    "error", "subject",
})
_AUDIT_EVENTS = frozenset({
    "pending_recovered", "import_confirmed", "series_added", "candidate_rejected",
    "torrent_probe_failed", "waiting_for_import", "proposed_grab", "proposed_update",
    "grab_submitted", "update_submitted", "scan_complete",
})
_AUDIT_REASONS = frozenset({
    "identity_mismatch", "hard_rejection", "too_few_seeders",
    "sonarr_parse_mismatch", "title_inventory_mismatch", "wrong_series",
    "wrong_season", "missing_not_covered", "not_growing", "seen_hash",
    "too_large", "missing_topic", "growing_pack",
})
_ERROR_CLASS = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,127}$")


def _safe_audit_value(key: str, value) -> bool:
    if key == "event":
        return value in _AUDIT_EVENTS
    if key == "error":
        return isinstance(value, str) and _ERROR_CLASS.fullmatch(value) is not None
    if key == "reason":
        return value in _AUDIT_REASONS
    if key == "at":
        return isinstance(value, str) and len(value) <= 64 and "://" not in value
    if key == "subject":
        return (isinstance(value, str) and 0 < len(value) <= 256
                and value == value.strip() and "://" not in value
                and all(ord(character) >= 32 for character in value))
    return isinstance(value, (int, float, bool, type(None)))


_AUDIT_SCAN_BYTES = 1024 * 1024
_AUDIT_SCAN_LINES = 2000


def _read_audit(state_dir: Path, limit: int, series_title=None) -> list[dict]:
    path = state_dir / "audit.jsonl"
    if not path.exists():
        return []
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        end = handle.tell()
        start = max(0, end - _AUDIT_SCAN_BYTES)
        handle.seek(start)
        tail = handle.read(end - start)
    if start:
        _, separator, tail = tail.partition(b"\n")
        if not separator:
            return []
    records = []
    title_cache = {}
    for raw_line in reversed(tail.splitlines()[-_AUDIT_SCAN_LINES:]):
        try:
            line = raw_line.decode("utf-8")
            source = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(source, dict):
            continue
        if source.get("event") not in _AUDIT_EVENTS:
            continue
        record = {key: source[key] for key in _AUDIT_FIELDS if key in source
                  and _safe_audit_value(key, source[key])}
        series_id = record.get("series_id")
        if "subject" not in record and type(series_id) is int and series_id > 0 and series_title:
            if series_id not in title_cache:
                try:
                    title_cache[series_id] = series_title(series_id)
                except Exception:
                    title_cache[series_id] = None
            title = title_cache[series_id]
            if _safe_audit_value("subject", title):
                record["subject"] = title
        records.append(record)
        if len(records) == limit:
            break
    return records


def main() -> None:
    config, endpoints = config_from_env()
    sonarr = SonarrClient(endpoints["sonarr_url"], endpoints["sonarr_key"])
    qbit = QbitClient(endpoints["qbit_url"], endpoints["qbit_key"])
    ledger = Ledger(config.state_dir / "ledger.db")
    engine = Engine(config, sonarr, qbit, ledger)
    token = os.environ.get("SERVARR_TOOLS_TOKEN") or None
    state = ControlState(
        dry_run=config.dry_run,
        state_dir=config.state_dir,
        token=token,
        series_title=lambda series_id: sonarr.series(series_id).get("title"),
    )
    coordinator = RunCoordinator(engine, state)
    server = ThreadingHTTPServer(
        ("0.0.0.0", int(os.environ.get("ROLLARR_HEALTH_PORT", "8080"))),
        make_handler(state, coordinator))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    coordinator.start()
    interval = max(60, int(os.environ.get("ROLLARR_POLL_INTERVAL", "900")))
    once = _boolean("ROLLARR_ONCE", "false")
    try:
        while True:
            coordinator.submit("scheduled")
            if once:
                coordinator.join()
                if coordinator.last_exception:
                    raise coordinator.last_exception
                break
            time.sleep(interval)
    finally:
        server.shutdown()
        server.server_close()
        coordinator.stop()


if __name__ == "__main__":
    main()
