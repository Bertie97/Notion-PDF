"""Notion API client."""

import logging
from typing import Any

import requests

from .utils import extract_page_id, extract_view_id, format_uuid, plain_text_from_rich

LOGGER = logging.getLogger(__name__)
BASE = "https://api.notion.com/v1"


class NotionClient:
    def __init__(self, token: str):
        self.token = token
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": "2026-03-11",
            "Content-Type": "application/json",
        }

    def _get(self, path: str, params: dict | None = None) -> dict:
        url = f"{BASE}{path}"
        resp = requests.get(url, headers=self.headers, params=params or {}, timeout=60)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, body: dict) -> dict:
        url = f"{BASE}{path}"
        resp = requests.post(url, headers=self.headers, json=body, timeout=60)
        resp.raise_for_status()
        return resp.json()

    def get_page(self, page_url_or_id: str) -> dict:
        raw_id = extract_page_id(page_url_or_id)
        page_id = format_uuid(raw_id)
        return self._get(f"/pages/{page_id}")

    def get_database(self, database_id: str) -> dict:
        return self._get(f"/databases/{database_id}")

    def get_data_source(self, data_source_id: str) -> dict:
        return self._get(f"/data_sources/{data_source_id}")

    def list_views(self, *, database_id: str | None = None,
                   data_source_id: str | None = None) -> list[dict]:
        params = {}
        if database_id:
            params["database_id"] = database_id
        if data_source_id:
            params["data_source_id"] = data_source_id
        results = []
        cursor = None
        while True:
            page_params = dict(params)
            if cursor:
                page_params["start_cursor"] = cursor
            data = self._get("/views", page_params)
            results.extend(data.get("results", []))
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
        return results

    def get_view(self, view_id: str) -> dict:
        return self._get(f"/views/{view_id}")

    def get_database_bundle(self, database_id: str) -> tuple[dict, list[dict], list[dict]]:
        """Return data-source schema, entries and full view objects."""
        database = self.get_database(database_id)
        sources = database.get("data_sources", [])
        if not sources:
            # Defensive compatibility with database-shaped responses.
            entries = self.query_database(database_id)
            return database, entries, []
        source_id = sources[0]["id"]
        data_source = self.get_data_source(source_id)
        data_source["_database"] = database
        entries = self.query_data_source(source_id)
        view_refs = self.list_views(database_id=database_id)
        views = []
        for ref in view_refs:
            try:
                views.append(self.get_view(ref["id"]))
            except Exception as exc:
                LOGGER.warning("Failed to retrieve view %s: %s", ref.get("id"), exc)
        return data_source, entries, views

    def query_data_source(self, data_source_id: str, page_size: int = 100) -> list[dict]:
        results = []
        cursor = None
        while True:
            body = {"page_size": page_size}
            if cursor:
                body["start_cursor"] = cursor
            data = self._post(f"/data_sources/{data_source_id}/query", body)
            results.extend(data.get("results", []))
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
        return results

    def query_database(self, database_id: str, page_size: int = 100) -> list[dict]:
        results = []
        cursor = None
        while True:
            body = {"page_size": page_size}
            if cursor:
                body["start_cursor"] = cursor
            data = self._post(f"/databases/{database_id}/query", body)
            results.extend(data.get("results", []))
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
        return results

    def get_block_children(self, block_id: str) -> list[dict]:
        results = []
        cursor = None
        while True:
            params = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            data = self._get(f"/blocks/{block_id}/children", params)
            results.extend(data.get("results", []))
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
        return results

    def get_page_with_blocks(self, page_url_or_id: str) -> dict:
        try:
            page = self.get_page(page_url_or_id)
        except requests.HTTPError as exc:
            if not self._is_database_not_page_error(exc):
                raise
            return self._get_database_document(page_url_or_id)
        page_id = page["id"]
        blocks = self.get_block_children(page_id)
        return {"page": page, "blocks": blocks}

    @staticmethod
    def _is_database_not_page_error(exc: requests.HTTPError) -> bool:
        response = exc.response
        if response is None or response.status_code != 400:
            return False
        try:
            error = response.json()
        except ValueError:
            return False
        return (
            error.get("code") == "validation_error"
            and "is a database, not a page" in error.get("message", "")
        )

    def _get_database_document(self, database_url_or_id: str) -> dict:
        database_id = format_uuid(extract_page_id(database_url_or_id))
        database, entries, views = self.get_database_bundle(database_id)
        database_meta = database.get("_database", database)
        title = database_meta.get("title", [])
        title_text = plain_text_from_rich(title) or database.get("name") or "Untitled"
        requested_view_id = extract_view_id(database_url_or_id)

        database["_views"] = views
        database["_database_id"] = database_id
        database["_root_database"] = True
        database["_requested_view_id"] = requested_view_id

        page = {
            "object": "page",
            "id": database_id,
            "icon": database_meta.get("icon"),
            "cover": database_meta.get("cover"),
            "parent": database_meta.get("parent", {}),
            "properties": {
                "title": {"type": "title", "title": title or [{"plain_text": title_text}]}
            },
        }
        block = {
            "object": "block",
            "id": database_id,
            "type": "child_database",
            "has_children": False,
            "child_database": {"title": title_text},
            "_db": database,
            "_entries": entries,
            "_root_database": True,
        }
        return {"page": page, "blocks": [block]}

    def download_file(self, url: str) -> bytes:
        resp = requests.get(url, timeout=120)
        resp.raise_for_status()
        return resp.content
