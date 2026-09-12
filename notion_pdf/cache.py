"""Persistent cache for fully enriched Notion page data."""

import gzip
import hashlib
import json
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .utils import extract_page_id


def _is_expired_signed_url(value: str, now: datetime | None = None) -> bool:
    """Return whether a Notion/S3 signed URL has passed its query expiry."""
    parsed = urlparse(value)
    query = parse_qs(parsed.query)
    signed_date = query.get("X-Amz-Date", [""])[0]
    expires = query.get("X-Amz-Expires", [""])[0]
    if not signed_date or not expires:
        return False
    try:
        issued_at = datetime.strptime(signed_date, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        expires_at = issued_at + timedelta(seconds=int(expires))
    except (TypeError, ValueError):
        return False
    return (now or datetime.now(timezone.utc)) >= expires_at


def _contains_expired_signed_url(value) -> bool:
    if isinstance(value, str):
        return _is_expired_signed_url(value)
    if isinstance(value, dict):
        return any(_contains_expired_signed_url(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_expired_signed_url(item) for item in value)
    return False


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
            data = payload["data"]
            if _contains_expired_signed_url(data):
                return None
            return data, float(payload.get("saved_at", path.stat().st_mtime))
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
