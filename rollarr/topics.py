import html
import re
from urllib.parse import parse_qs, urlsplit


_URL = re.compile(r"https?://[^\s\"'<>]+", re.I)


def topic_identity(text: str) -> tuple[str, str] | None:
    for raw in _URL.findall(html.unescape(text or "")):
        url = urlsplit(raw.rstrip(".,);"))
        host = (url.hostname or "").lower()
        query = parse_qs(url.query)
        if "nnmclub" in host and query.get("t", [""])[0].isdigit():
            return "nnmclub", query["t"][0]
        if "rutracker" in host and query.get("t", [""])[0].isdigit():
            return "rutracker", query["t"][0]
        if "rutor" in host:
            match = re.search(r"/torrent/(\d+)", url.path)
            if match:
                return "rutor", match.group(1)
    return None
