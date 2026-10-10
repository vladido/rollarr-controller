from dataclasses import dataclass
import hashlib


@dataclass(frozen=True)
class Metainfo:
    infohash: str
    name: str
    files: tuple[tuple[str, int], ...]
    raw: bytes


class _Decoder:
    def __init__(self, data: bytes):
        self.data = data

    def value(self, pos: int = 0):
        token = self.data[pos:pos + 1]
        if token == b"i":
            end = self.data.index(b"e", pos)
            return int(self.data[pos + 1:end]), end + 1
        if token == b"l":
            out, pos = [], pos + 1
            while self.data[pos:pos + 1] != b"e":
                item, pos = self.value(pos)
                out.append(item)
            return out, pos + 1
        if token == b"d":
            out, pos = {}, pos + 1
            while self.data[pos:pos + 1] != b"e":
                key, pos = self.value(pos)
                value, pos = self.value(pos)
                out[key] = value
            return out, pos + 1
        if token.isdigit():
            colon = self.data.index(b":", pos)
            length = int(self.data[pos:colon])
            start, end = colon + 1, colon + 1 + length
            if end > len(self.data):
                raise ValueError("truncated byte string")
            return self.data[start:end], end
        raise ValueError(f"invalid bencode token at {pos}")


def _top_info_slice(data: bytes) -> tuple[dict, bytes]:
    decoder = _Decoder(data)
    if data[:1] != b"d":
        raise ValueError("torrent root must be a dictionary")
    root, pos, info_raw = {}, 1, None
    while data[pos:pos + 1] != b"e":
        key, pos = decoder.value(pos)
        start = pos
        value, pos = decoder.value(pos)
        root[key] = value
        if key == b"info":
            info_raw = data[start:pos]
    if pos + 1 != len(data) or info_raw is None:
        raise ValueError("invalid torrent metainfo")
    return root, info_raw


def _text(value: bytes) -> str:
    return value.decode("utf-8", "surrogateescape")


def parse_metainfo(data: bytes) -> Metainfo:
    root, info_raw = _top_info_slice(data)
    info = root[b"info"]
    name = _text(info.get(b"name.utf-8", info[b"name"]))
    files = []
    if b"files" in info:
        for item in info[b"files"]:
            parts = item.get(b"path.utf-8", item[b"path"])
            files.append(("/".join(_text(x) for x in parts), int(item[b"length"])))
    else:
        files.append((name, int(info[b"length"])))
    return Metainfo(hashlib.sha1(info_raw).hexdigest(), name, tuple(files), data)
