import json
import time
import urllib.parse
import urllib.request
import uuid


def _multipart(fields: dict[str, str], files: dict[str, tuple[str, bytes]]) -> tuple[bytes, str]:
    boundary = "----rollarr-" + uuid.uuid4().hex
    out = bytearray()
    for name, value in fields.items():
        out.extend(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode())
    for name, (filename, data) in files.items():
        out.extend(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; filename=\"{filename}\"\r\nContent-Type: application/x-bittorrent\r\n\r\n".encode())
        out.extend(data); out.extend(b"\r\n")
    out.extend(f"--{boundary}--\r\n".encode())
    return bytes(out), f"multipart/form-data; boundary={boundary}"


class _Client:
    def __init__(self, base_url: str, headers: dict[str, str]):
        self.base_url = base_url.rstrip("/")
        self.headers = headers

    def request(self, method: str, path: str, *, data: bytes | None = None,
                headers: dict[str, str] | None = None, json_body=None,
                timeout: int = 120):
        merged = {**self.headers, **(headers or {})}
        if json_body is not None:
            data = json.dumps(json_body).encode()
            merged["Content-Type"] = "application/json"
        url = path if path.startswith(("http://", "https://")) else self.base_url + path
        req = urllib.request.Request(url, method=method, headers=merged, data=data)
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read()
            content_type = response.headers.get("Content-Type", "")
            return json.loads(body) if body and "json" in content_type else body


class SonarrClient(_Client):
    def __init__(self, base_url: str, api_key: str):
        super().__init__(base_url, {"X-Api-Key": api_key})

    def missing(self):
        return self.request("GET", "/api/v3/wanted/missing?page=1&pageSize=1000&sortKey=airDateUtc&sortDirection=ascending&includeSeries=true")

    def all_series(self):
        return self.request("GET", "/api/v3/series")

    def series(self, series_id: int):
        return self.request("GET", f"/api/v3/series/{series_id}")

    def episodes(self, series_id: int):
        return self.request("GET", f"/api/v3/episode?seriesId={series_id}")

    def releases(self, series_id: int, season: int):
        query = urllib.parse.urlencode({"seriesId": series_id, "seasonNumber": season})
        return self.request("GET", "/api/v3/release?" + query, timeout=300)

    def parse(self, title: str):
        query = urllib.parse.urlencode({"title": title})
        return self.request("GET", "/api/v3/parse?" + query)

    def download(self, url: str) -> bytes:
        return self.request("GET", url)

    def grab(self, release: dict, *, series_id: int, episode_ids: tuple[int, ...]):
        payload = dict(release)
        payload["seriesId"] = series_id
        payload["episodeIds"] = list(episode_ids)
        return self.request("POST", "/api/v3/release", json_body=payload)

    def refresh_downloads(self):
        return self.request("POST", "/api/v3/command", json_body={"name": "RefreshMonitoredDownloads"})


class QbitClient(_Client):
    def __init__(self, base_url: str, api_key: str, wait_seconds: float = 1.0):
        super().__init__(base_url, {"Authorization": "Bearer " + api_key})
        self.wait_seconds = wait_seconds

    def torrents(self):
        return self.request("GET", "/api/v2/torrents/info")

    def files(self, infohash: str):
        return self.request("GET", "/api/v2/torrents/files?" + urllib.parse.urlencode({"hash": infohash}))

    def add_stopped(self, torrent: bytes, save_path: str, category: str, tags: tuple[str, ...]):
        body, content_type = _multipart({"savepath": save_path, "category": category,
                                         "tags": ",".join(tags), "stopped": "true"},
                                        {"torrents": ("candidate.torrent", torrent)})
        return self.request("POST", "/api/v2/torrents/add", data=body,
                            headers={"Content-Type": content_type})

    def wait_exists(self, infohash: str, attempts: int = 10) -> bool:
        for _ in range(attempts):
            rows = self.request("GET", "/api/v2/torrents/info?" + urllib.parse.urlencode({"hashes": infohash}))
            if any(row.get("hash", "").lower() == infohash.lower() for row in rows):
                return True
            time.sleep(self.wait_seconds)
        return False

    def _hash_action(self, action: str, infohash: str):
        data = urllib.parse.urlencode({"hashes": infohash}).encode()
        return self.request("POST", f"/api/v2/torrents/{action}", data=data,
                            headers={"Content-Type": "application/x-www-form-urlencoded"})

    def recheck(self, infohash: str):
        return self._hash_action("recheck", infohash)

    def start(self, infohash: str):
        return self._hash_action("start", infohash)
