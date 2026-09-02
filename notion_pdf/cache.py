"""Persistent cache for fully enriched Notion page data."""

import gzip
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

from .utils import extract_page_id


class PageDataCache:
    def __init__(self, cache_dir: Path | None = None):
        self.cache_dir = cache_dir or self._default_dir()
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _default_dir() -> Path:
        preferred = Path.home() / ".notion_pdf" / "cache"
        try:
            preferred.mkdir(parents=True, exist_ok=True)
            probe = preferred / ".write-test"
            probe.write_bytes(b"")
            probe.unlink()
            return preferred
        except OSError:
            return Path(tempfile.gettempdir()) / "notion_pdf" / "cache"

    def _path(self, page_url_or_id: str, token: str) -> Path:
        page_id = extract_page_id(page_url_or_id)
        token_id = hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
        return self.cache_dir / f"{page_id}_{token_id}.json.gz"

    def load(self, page_url_or_id: str, token: str) -> tuple[dict, float] | None:
        path = self._path(page_url_or_id, token)
        try:
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                payload = json.load(handle)
            return payload["data"], float(payload.get("saved_at", path.stat().st_mtime))
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return None

    def save(self, page_url_or_id: str, token: str, data: dict) -> Path:
        path = self._path(page_url_or_id, token)
        temp_path = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
        payload = {"version": 1, "saved_at": time.time(), "data": data}
        try:
            with gzip.open(temp_path, "wt", encoding="utf-8", compresslevel=5) as handle:
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            os.replace(temp_path, path)
        finally:
            try:
                temp_path.unlink()
            except OSError:
                pass
        return path
