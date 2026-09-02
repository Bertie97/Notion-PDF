"""Render a Notion page to HTML and then to PDF."""

import concurrent.futures
import html
import logging
import os
import tempfile
import textwrap
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from .api import NotionClient
from .blocks import render_block, render_database
from .utils import plain_text_from_rich, property_value_to_html

LOGGER = logging.getLogger(__name__)

A4_W_MM = 210.0
A4_H_MM = 297.0
MAX_WORKERS = 8


class PageRenderer:
    def __init__(
        self,
        client: NotionClient,
        orientation: str = "auto",
        full_width: bool | None = None,
        view_type: str = "auto",
        group_by: str | None = None,
        gallery_fit: bool = False,
        gallery_size: str = "medium",
        margin_mm: float = 15.0,
        page_size: str = "A4",
        database_options: dict[str, dict] | None = None,
    ):
        self.client = client
        self.orientation = orientation
        self.full_width = full_width
        self.view_type = view_type
        self.group_by = group_by
        self.gallery_fit = gallery_fit
        self.gallery_size = gallery_size
        self.margin_mm = margin_mm
        self.page_size = page_size
        self.database_options = database_options or {}

    def fetch_page_data(
        self,
        page_url_or_id: str,
        progress_callback: Any = None,
    ) -> dict:
        """Fetch all Notion data needed for rendering.

        If progress_callback is provided, it is called with (stage, data)
        after each major phase so the UI can render incrementally:
            - "page":      page metadata + top-level blocks available
            - "children":  all nested block children loaded
            - "databases": child databases enriched with entries
        """
        raw = self.client.get_page_with_blocks(page_url_or_id)
        page = raw["page"]
        blocks = raw["blocks"]
        parent = page.get("parent", {})
        if parent.get("type") == "data_source_id" and parent.get("data_source_id"):
            try:
                page["_schema"] = self.client.get_data_source(parent["data_source_id"]).get("properties", {})
            except Exception as exc:
                LOGGER.warning("Could not load parent data-source schema: %s", exc)

        def notify(stage: str, current_blocks: list[dict]) -> None:
            if progress_callback is not None:
                progress_callback(stage, {"page": page, "blocks": current_blocks})

        notify("page", blocks)

        blocks = self._load_children(blocks)
        notify("children", blocks)

        blocks = self._enrich_databases(blocks)
        notify("databases", blocks)

        return {
            "page": page,
            "blocks": blocks,
            "entry_blocks_cache": getattr(self, "_entry_blocks_cache", {}),
        }

    def _load_children(self, blocks: list[dict]) -> list[dict]:
        """Recursively load block children, fetching siblings in parallel."""
        to_fetch = [b for b in blocks if b.get("has_children", False)]
        if to_fetch:
            with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
                future_to_block = {
                    pool.submit(self.client.get_block_children, b["id"]): b
                    for b in to_fetch
                }
                for future in concurrent.futures.as_completed(future_to_block):
                    block = future_to_block[future]
                    try:
                        block["children"] = future.result()
                    except Exception as exc:
                        LOGGER.warning("Failed to load children for block %s: %s", block["id"], exc)
                        block["children"] = []
        # Recurse after fetching all siblings at this level.
        for block in blocks:
            if block.get("children"):
                block["children"] = self._load_children(block["children"])
        return blocks

    def _enrich_databases(self, blocks: list[dict]) -> list[dict]:
        """Pre-fetch database schema, entries and entry blocks for child_database blocks."""
        db_blocks = [b for b in blocks if b.get("type") == "child_database"]
        LOGGER.info("Found %d child_database block(s) to enrich", len(db_blocks))

        def fetch_db(block: dict) -> tuple[dict | None, list[dict]]:
            db_id = block["id"]
            try:
                LOGGER.info("Fetching database schema: %s", db_id)
                db, entries, views = self.client.get_database_bundle(db_id)
                LOGGER.info("Database %s loaded: %d entries", db_id, len(entries))
                db["_views"] = views
                db["_database_id"] = db_id
                return db, entries
            except Exception as exc:
                LOGGER.exception("Failed to enrich child_database %s", db_id)
                return None, []

        if db_blocks:
            with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
                results = list(pool.map(fetch_db, db_blocks))
            for block, (db, entries) in zip(db_blocks, results):
                block["_db"] = db
                block["_entries"] = entries
                if db:
                    self._ensure_entry_blocks_cache(entries, sample_size=10)

        for block in blocks:
            if block.get("children"):
                block["children"] = self._enrich_databases(block["children"])
        return blocks

    def render(
        self,
        page_url_or_id: str,
        orientation: str | None = None,
        full_width: bool | None = None,
        view_type: str | None = None,
        group_by: str | None = None,
        gallery_fit: bool | None = None,
        gallery_size: str | None = None,
        database_options: dict[str, dict] | None = None,
        preview_mode: bool = False,
    ) -> dict:
        """Fetch page and return dict with html, title, orientation, etc."""
        data = self.fetch_page_data(page_url_or_id)
        return self.render_html(
            data,
            orientation=orientation,
            full_width=full_width,
            view_type=view_type,
            group_by=group_by,
            gallery_fit=gallery_fit,
            gallery_size=gallery_size,
            database_options=database_options,
            preview_mode=preview_mode,
        )

    def render_html(
        self,
        data: dict,
        orientation: str | None = None,
        full_width: bool | None = None,
        view_type: str | None = None,
        group_by: str | None = None,
        gallery_fit: bool | None = None,
        gallery_size: str | None = None,
        database_options: dict[str, dict] | None = None,
        preview_mode: bool = False,
    ) -> dict:
        """Render cached page data to HTML."""
        orientation = orientation if orientation is not None else self.orientation
        full_width = full_width if full_width is not None else self.full_width
        view_type = view_type if view_type is not None else self.view_type
        group_by = group_by if group_by is not None else self.group_by
        gallery_fit = gallery_fit if gallery_fit is not None else self.gallery_fit
        gallery_size = gallery_size if gallery_size is not None else self.gallery_size
        database_options = database_options if database_options is not None else self.database_options

        page = data["page"]
        blocks = data["blocks"]
        cached_entry_blocks = data.get("entry_blocks_cache", {})
        if cached_entry_blocks:
            if not hasattr(self, "_entry_blocks_cache"):
                self._entry_blocks_cache = {}
            self._entry_blocks_cache.update(cached_entry_blocks)

        is_full_width = bool(full_width)
        if orientation == "landscape":
            orient = "landscape"
        elif orientation == "portrait":
            orient = "portrait"
        elif is_full_width:
            orient = "landscape"
        else:
            orient = "portrait"

        # Temporarily override instance parameters for rendering
        original_view_type = self.view_type
        original_group_by = self.group_by
        original_gallery_fit = self.gallery_fit
        original_gallery_size = self.gallery_size
        self.view_type = view_type
        self.group_by = group_by
        self.gallery_fit = gallery_fit
        self.gallery_size = gallery_size
        original_database_options = self.database_options
        self.database_options = database_options
        try:
            title = self._page_title(page)
            icon_html = self._page_icon(page)
            property_bar = self._render_properties(page)
            body_html = self._render_blocks(blocks)

            html_doc = self._build_html(
                title=title,
                icon=icon_html,
                property_bar=property_bar,
                body=body_html,
                orientation=orient,
                is_full_width=is_full_width,
                preview_mode=preview_mode,
            )
        finally:
            self.view_type = original_view_type
            self.group_by = original_group_by
            self.gallery_fit = original_gallery_fit
            self.gallery_size = original_gallery_size
            self.database_options = original_database_options

        return {
            "title": title,
            "html": html_doc,
            "orientation": orient,
        }

    def _page_title(self, page: dict) -> str:
        for prop in page.get("properties", {}).values():
            if prop.get("type") == "title":
                return plain_text_from_rich(prop.get("title", [])) or "Untitled"
        return "Untitled"

    def _page_icon(self, page: dict) -> str:
        icon = page.get("icon")
        if not icon:
            return ""
        if icon.get("type") == "emoji":
            return f'<span class="nb-page-icon">{icon.get("emoji", "")}</span>'
        if icon.get("type") == "external":
            url = icon.get("external", {}).get("url", "")
            return f'<img class="nb-page-icon" src="{html.escape(url)}"/>'
        if icon.get("type") == "file":
            url = icon.get("file", {}).get("url", "")
            return f'<img class="nb-page-icon" src="{html.escape(url)}"/>'
        return ""

    def _render_properties(self, page: dict) -> str:
        """Render page properties as a small info bar under the title."""
        items = []
        schema = page.get("_schema", {})
        for name, prop in page.get("properties", {}).items():
            if prop.get("type") == "title":
                continue
            val_html = property_value_to_html(prop, schema.get(name))
            if val_html:
                items.append(
                    f'<div class="nb-property-item">'
                    f'<span class="nb-property-name">{html.escape(name)}</span>'
                    f'<span class="nb-property-value">{val_html}</span>'
                    f'</div>'
                )
        if not items:
            return ""
        return f'<div class="nb-property-bar">{"".join(items)}</div>'

    def _render_blocks(self, blocks: list[dict]) -> str:
        parts = []
        # Flatten consecutive list items into <ul>/<ol>
        i = 0
        while i < len(blocks):
            block = blocks[i]
            btype = block.get("type", "")
            if btype == "bulleted_list_item":
                items, i = self._collect_list(blocks, i, "bulleted_list_item")
                parts.append(f'<ul class="nb-list nb-bulleted-list">{"".join(items)}</ul>')
            elif btype == "numbered_list_item":
                items, i = self._collect_list(blocks, i, "numbered_list_item")
                parts.append(f'<ol class="nb-list nb-numbered-list">{"".join(items)}</ol>')
            elif btype == "child_database":
                db_id = block["id"]
                db = block.get("_db")
                entries = block.get("_entries", [])
                if not db:
                    LOGGER.error(
                        "Rendering child_database %s as unavailable (_db=%r, _entries_len=%d)",
                        db_id, db, len(entries)
                    )
                    parts.append(f'<div class="nb-error">[Database unavailable]</div>')
                    i += 1
                    continue
                # Pre-fetch entry blocks for gallery cover fallbacks and heuristics.
                self._ensure_entry_blocks_cache(entries, sample_size=10)
                db_options = self.database_options.get(db_id, {})
                selected_view = self._selected_view(db, db_options.get("view_id"))
                view_type = db_options.get("view_type") or (
                    selected_view.get("type") if selected_view else self._resolve_view_type(db, entries)
                )
                if view_type not in ("table", "gallery", "list"):
                    view_type = "table"
                requested_group = db_options.get("group_by")
                group_by = requested_group or None
                requested_size = db_options.get("gallery_size")
                gallery_size = requested_size or self._view_gallery_size(selected_view) or self.gallery_size
                requested_fit = db_options.get("gallery_fit")
                gallery_fit = (
                    self._view_gallery_fit(selected_view)
                    if requested_fit is None else requested_fit
                )
                ctx = {
                    "client": self.client,
                    "entry_blocks_cache": getattr(self, "_entry_blocks_cache", {}),
                    "group_by": group_by,
                    "gallery_fit": self.gallery_fit if gallery_fit is None else gallery_fit,
                    "gallery_size": gallery_size,
                    "view": selected_view,
                }
                try:
                    parts.append(render_database(db, entries, view_type, ctx))
                except Exception as exc:
                    LOGGER.warning("Failed to render child_database %s: %s", db_id, exc)
                    parts.append(f'<div class="nb-error">[Database render error]</div>')
                i += 1
            else:
                parts.append(render_block(block, {"client": self.client}))
                i += 1
        return "".join(parts)

    def _collect_list(self, blocks: list[dict], start: int, list_type: str):
        items = []
        i = start
        while i < len(blocks) and blocks[i].get("type") == list_type:
            items.append(render_block(blocks[i], {"client": self.client}))
            i += 1
        return items, i

    def _ensure_entry_blocks_cache(self, entries: list[dict], sample_size: int = 10) -> None:
        """Fetch blocks for the first sample_size entries and cache them.

        This is used both for view-type heuristics and for picking fallback
        cover images when rendering a gallery.
        """
        if not hasattr(self, "_entry_blocks_cache"):
            self._entry_blocks_cache: dict[str, list[dict]] = {}
        todo = [
            entry for entry in entries[:sample_size]
            if entry.get("id") and entry.get("id") not in self._entry_blocks_cache
        ]

        def fetch(entry: dict) -> tuple[str, list[dict]]:
            eid = entry["id"]
            try:
                return eid, self.client.get_block_children(eid)
            except Exception as exc:
                logging.getLogger(__name__).debug("Could not fetch entry blocks for %s: %s", eid, exc)
                return eid, []

        if todo:
            with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
                for eid, blocks in pool.map(fetch, todo):
                    self._entry_blocks_cache[eid] = blocks

    def _resolve_view_type(self, db: dict, entries: list[dict]) -> str:
        if self.view_type in ("table", "gallery", "list"):
            return self.view_type
        # Heuristic for auto mode:
        # 1. schema has a files property named cover, or entries have cover.
        # 2. >=80% of sampled entries have an image as their first block.
        schema = db.get("properties", {})
        has_cover_prop = any(
            v.get("type") == "files" and "cover" in k.lower()
            for k, v in schema.items()
        )
        has_entries_with_cover = any(
            entry.get("cover") for entry in entries[:10]
        )
        if has_cover_prop or has_entries_with_cover:
            return "gallery"
        self._ensure_entry_blocks_cache(entries, sample_size=10)
        sample = entries[:10]
        if not sample:
            return "table"
        image_count = sum(
            1 for e in sample
            if self._entry_blocks_cache.get(e.get("id", ""), [{}])[0].get("type") == "image"
        )
        if image_count / len(sample) >= 0.8:
            return "gallery"
        return "table"

    @staticmethod
    def _selected_view(db: dict, view_id: str | None) -> dict | None:
        views = db.get("_views", [])
        if view_id:
            return next((view for view in views if view.get("id") == view_id), None)
        return views[0] if views else None

    @staticmethod
    def _view_group_property(db: dict, view: dict | None) -> str | None:
        group_by = ((view or {}).get("configuration") or {}).get("group_by") or {}
        property_id = group_by.get("property_id")
        if not property_id:
            return None
        for name, prop in db.get("properties", {}).items():
            if unquote(str(prop.get("id", ""))) == unquote(str(property_id)):
                return name
        return None

    @staticmethod
    def _view_gallery_size(view: dict | None) -> str | None:
        size = ((view or {}).get("configuration") or {}).get("cover_size")
        return size if size in ("small", "medium", "large") else None

    @staticmethod
    def _view_gallery_fit(view: dict | None) -> bool | None:
        aspect = ((view or {}).get("configuration") or {}).get("cover_aspect")
        if aspect == "contain":
            return True
        if aspect == "cover":
            return False
        return None

    def _build_html(self, title: str, icon: str, property_bar: str, body: str, orientation: str, is_full_width: bool, preview_mode: bool = False) -> str:
        page_width = A4_W_MM if orientation == "portrait" else A4_H_MM
        page_height = A4_H_MM if orientation == "portrait" else A4_W_MM
        content_width = page_width - 2 * self.margin_mm
        content_height = page_height - 2 * self.margin_mm

        css = self._build_css(
            orientation=orientation,
            is_full_width=is_full_width,
            content_width=content_width,
            content_height=content_height,
            preview_mode=preview_mode,
        )
        preview_attr = ' class="preview-mode"' if preview_mode else ""
        pagination_script = self._preview_pagination_script(orientation) if preview_mode else ""
        main_attrs = 'id="nb-content" class="nb-page-content" data-margin="{mm}" data-orientation="{ori}"'.format(mm=self.margin_mm, ori=orientation) if preview_mode else 'class="nb-page-content"'
        return textwrap.dedent(f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>{html.escape(title)}</title>
<style>
{css}
</style>
</head>
<body{preview_attr}>
<div class="nb-page">
  <header class="nb-page-header">
    <div class="nb-page-title-line">{icon}<h1 class="nb-page-title">{html.escape(title)}</h1></div>
    {property_bar}
  </header>
  <main {main_attrs}>
{body}
  </main>
</div>
{self._rotation_script(orientation)}
{pagination_script}
</body>
</html>""")

    def _build_css(self, orientation: str, is_full_width: bool, content_width: float, content_height: float, preview_mode: bool = False) -> str:
        preview_css = self._preview_css(orientation) if preview_mode else ""
        return textwrap.dedent(f"""
        @page {{
            size: A4 {orientation};
            margin: {self.margin_mm}mm;
            @bottom-center {{
                content: counter(page);
                font-size: 9pt;
                color: #666;
            }}
        }}
        {preview_css}
        * {{
            box-sizing: border-box;
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, "Noto Sans", "PingFang SC", "Microsoft YaHei", sans-serif;
            font-size: 11pt;
            line-height: 1.6;
            color: #222;
            margin: 0;
            padding: 0;
        }}
        .nb-page {{
            max-width: {content_width}mm;
            margin: 0 auto;
        }}
        .nb-page-header {{
            margin-bottom: 1.2em;
            border-bottom: 1px solid #eee;
            padding-bottom: 0.6em;
        }}
        .nb-page-title-line {{
            display: flex;
            align-items: center;
            gap: 0.4em;
            margin-bottom: 0.3em;
        }}
        .nb-page-icon {{
            font-size: 1.6em;
            line-height: 1;
            width: 1.2em;
            height: 1.2em;
            object-fit: contain;
        }}
        .nb-page-title {{
            font-size: 22pt;
            font-weight: 700;
            margin: 0;
            line-height: 1.2;
        }}
        .nb-property-bar {{
            display: flex;
            flex-wrap: wrap;
            gap: 0.6em 1.2em;
            font-size: 8.5pt;
            color: #555;
            margin-top: 0.4em;
        }}
        .nb-property-item {{
            display: flex;
            align-items: baseline;
            gap: 0.35em;
        }}
        .nb-property-name {{
            color: #999;
            font-weight: 500;
        }}
        .nb-property-value {{
            color: #111;
            font-weight: 500;
        }}
        .nb-option {{ display:inline-block; padding:1px 7px; margin:1px 4px 1px 0; border-radius:4px; background:#e9e9e7; white-space:nowrap; }}
        .nb-option-gray {{ background:#e3e2e0; }} .nb-option-brown {{ background:#eee0da; }}
        .nb-option-orange {{ background:#fadec9; }} .nb-option-yellow {{ background:#fdecc8; }}
        .nb-option-green {{ background:#dbeddb; }} .nb-option-blue {{ background:#d3e5ef; }}
        .nb-option-purple {{ background:#e8deee; }} .nb-option-pink {{ background:#f5e0e9; }}
        .nb-option-red {{ background:#ffe2dd; }}
        .nb-checkbox {{ display:inline-block; width:14px; height:14px; border:1px solid #9b9a97; border-radius:2px; line-height:12px; text-align:center; font-size:11px; }}
        .nb-checkbox.checked {{ background:#2383e2; color:white; border-color:#2383e2; }}
        .nb-page-content {{
            width: 100%;
        }}
        .nb-paragraph {{
            margin: 0.6em 0;
        }}
        .nb-paragraph.empty {{
            min-height: 1em;
        }}
        h1.nb-heading_1, h2.nb-heading_2, h3.nb-heading_3 {{
            margin: 1.2em 0 0.5em;
            line-height: 1.25;
            font-weight: 600;
        }}
        h1.nb-heading_1 {{ font-size: 17pt; }}
        h2.nb-heading_2 {{ font-size: 14pt; }}
        h3.nb-heading_3 {{ font-size: 12pt; }}
        .nb-list {{
            margin: 0.4em 0;
            padding-left: 1.5em;
        }}
        .nb-list li {{
            margin: 0.25em 0;
        }}
        .nb-todo {{
            display: flex;
            align-items: baseline;
            gap: 0.35em;
            margin: 0.25em 0;
        }}
        .nb-toggle {{
            margin: 0.6em 0;
            border-left: 2px solid #ddd;
            padding-left: 0.6em;
        }}
        .nb-toggle-title {{
            font-weight: 600;
            color: #444;
        }}
        .nb-toggle-body {{
            margin-top: 0.4em;
        }}
        .nb-quote, blockquote {{
            margin: 0.6em 0;
            padding: 0.4em 0.8em;
            border-left: 3px solid #ccc;
            color: #555;
            background: #f9f9f9;
        }}
        .nb-callout {{
            display: flex;
            gap: 0.5em;
            align-items: flex-start;
            padding: 0.6em 0.8em;
            border-radius: 4px;
            margin: 0.6em 0;
            background: #f3f3f3;
        }}
        .nb-callout-icon {{
            flex-shrink: 0;
            font-size: 1.1em;
        }}
        .nb-callout-body {{
            flex: 1;
        }}
        .nb-code {{
            background: #f5f5f5;
            padding: 0.6em 0.8em;
            border-radius: 4px;
            overflow-x: auto;
            font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
            font-size: 9.5pt;
            margin: 0.6em 0;
            white-space: pre-wrap;
            word-break: break-word;
        }}
        .nb-code code {{
            background: transparent;
            padding: 0;
        }}
        code {{
            background: #f0f0f0;
            padding: 0.05em 0.25em;
            border-radius: 3px;
            font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
            font-size: 0.92em;
        }}
        .nb-divider {{
            border: none;
            border-top: 1px solid #e0e0e0;
            margin: 1em 0;
        }}
        .nb-image {{
            margin: 0.8em 0;
            max-width: 100%;
        }}
        .nb-image img {{
            max-width: 100%;
            height: auto;
            display: block;
        }}
        figcaption {{
            font-size: 9pt;
            color: #666;
            margin-top: 0.3em;
        }}
        .nb-table {{
            width: 100%;
            border-collapse: collapse;
            margin: 0.8em 0;
            font-size: 9.5pt;
            table-layout: auto;
        }}
        .nb-table th, .nb-table td {{
            border: 1px solid #d0d0d0;
            padding: 0.35em 0.5em;
            text-align: left;
            vertical-align: top;
            word-wrap: break-word;
        }}
        .nb-table th {{
            background: #f5f5f5;
            font-weight: 600;
        }}
        .nb-db {{
            margin: 1em 0;
        }}
        .nb-db-gallery {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(70mm, 1fr));
            gap: 0.8em;
            align-items: stretch;
        }}
        .nb-db-gallery-small {{
            grid-template-columns: repeat(auto-fill, minmax(45mm, 1fr));
            gap: 0.5em;
        }}
        .nb-db-gallery-medium .nb-db-gallery-cover,
        .nb-db-gallery:not(.nb-db-gallery-small):not(.nb-db-gallery-large) .nb-db-gallery-cover {{
            height: 48mm;
        }}
        .nb-db-gallery-small .nb-db-gallery-cover {{
            height: 30mm;
        }}
        .nb-db-gallery-large {{
            grid-template-columns: repeat(auto-fill, minmax(90mm, 1fr));
            gap: 1em;
        }}
        .nb-db-gallery-large .nb-db-gallery-cover {{
            height: 70mm;
        }}
        .nb-db-gallery-card {{
            display: flex;
            flex-direction: column;
            background: #fff;
            border: 1px solid #e6e6e6;
            border-radius: 8px;
            overflow: hidden;
            box-shadow: 0 1px 3px rgba(0,0,0,0.06);
            break-inside: avoid;
        }}
        .nb-db-gallery-cover {{
            width: 100%;
            height: 48mm;
            overflow: hidden;
            background: #f0f0f0;
            position: relative;
        }}
        .nb-db-gallery-cover img {{
            width: 100%;
            height: 100%;
            object-fit: cover;
            display: block;
        }}
        .nb-db-gallery-cover-fit img {{
            object-fit: contain;
        }}
        .nb-db-gallery-cover-empty {{
            background: linear-gradient(135deg, hsl(var(--cover-hue, 200), 55%, 92%), hsl(var(--cover-hue, 200), 45%, 85%));
            display: flex;
            align-items: center;
            justify-content: center;
        }}
        .nb-db-gallery-cover-icon {{
            font-size: 2.2em;
            line-height: 1;
            color: rgba(0,0,0,0.45);
        }}
        .nb-db-gallery-cover-icon img {{
            width: 1.2em;
            height: 1.2em;
            object-fit: contain;
        }}
        .nb-db-gallery-initial {{
            font-weight: 700;
            font-size: 1.4em;
            color: rgba(0,0,0,0.45);
        }}
        .nb-db-gallery-body {{
            padding: 0.5em 0.6em 0.6em;
            flex: 1;
        }}
        .nb-db-gallery-title {{
            font-weight: 600;
            font-size: 10.5pt;
            margin-bottom: 0.35em;
            line-height: 1.25;
        }}
        .nb-db-gallery-field {{
            font-size: 8pt;
            margin: 0.18em 0;
            display: flex;
            align-items: baseline;
            gap: 0.3em;
        }}
        .nb-db-gallery-label {{
            color: #999;
            flex-shrink: 0;
        }}
        .nb-db-gallery-value {{
            color: #111;
            word-break: break-word;
            font-weight: 500;
        }}
        .nb-db-group {{
            margin: 1em 0;
        }}
        .nb-db-group-grid {{
            display: grid;
            gap: 0.8em;
            align-items: start;
            margin: 1em 0;
        }}
        .nb-db-group-grid-small {{ grid-template-columns: repeat(5, minmax(0, 1fr)); }}
        .nb-db-group-grid-medium {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }}
        .nb-db-group-grid-large {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
        .nb-db-group-grid > .nb-db-group {{
            grid-column: span var(--group-span, 1);
            min-width: 0;
            margin: 0;
            break-inside: avoid;
        }}
        .nb-db-group-grid > .nb-db-group > .nb-db-gallery {{
            grid-template-columns: repeat(var(--group-span, 1), minmax(0, 1fr));
            margin: 0;
        }}
        .nb-db-group-title {{
            font-size: 12pt;
            font-weight: 600;
            color: #222;
            margin: 0.8em 0 0.4em;
            padding-bottom: 0.2em;
            border-bottom: 1px solid #e0e0e0;
            break-after: avoid;
            page-break-after: avoid;
        }}
        .nb-db-list {{
            padding-left: 1.2em;
        }}
        .nb-db-list-item {{
            margin: 0.3em 0;
        }}
        .nb-db-list-field {{
            font-size: 8.5pt;
            color: #666;
            margin-left: 0.4em;
        }}
        .nb-column-list {{
            display: flex;
            gap: 1em;
            margin: 0.6em 0;
        }}
        .nb-column {{
            flex: 1;
            min-width: 0;
        }}
        .nb-bookmark, .nb-embed, .nb-video, .nb-file, .nb-pdf {{
            margin: 0.6em 0;
            padding: 0.4em 0.6em;
            background: #f8f8f8;
            border-radius: 4px;
            font-size: 9.5pt;
            word-break: break-all;
        }}
        .nb-equation {{
            margin: 0.6em 0;
            text-align: center;
            font-style: italic;
        }}
        .nb-error {{
            color: #c00;
            background: #fee;
            padding: 0.4em 0.6em;
            border-radius: 4px;
            margin: 0.5em 0;
        }}
        .nb-unsupported {{
            color: #888;
            font-style: italic;
            margin: 0.3em 0;
        }}
        .nb-child-page, .nb-link-to-page {{
            margin: 0.4em 0;
            padding: 0.3em 0.5em;
            background: #f4f4f4;
            border-radius: 4px;
        }}

        /* Wide table rotation for portrait mode */
        {self._wide_table_css(content_width, content_height, orientation)}
        """)

    def _wide_table_css(self, content_width: float, content_height: float, orientation: str) -> str:
        if orientation == "landscape":
            return """
            .nb-db-table, .nb-db-table .nb-table {
                max-width: 100%;
            }
            .nb-db-table .nb-table {
                transform: none;
            }
            """
        return f"""
        .wide-rotate-wrapper {{
            position: relative;
            width: 100%;
            height: {content_width}mm;
            margin: {self.margin_mm}mm 0;
            page-break-inside: avoid;
        }}
        .wide-rotate-wrapper .nb-table {{
            position: absolute;
            top: 0;
            left: 0;
            transform: rotate(-90deg) translateX(-100%);
            transform-origin: top left;
            width: {content_height}mm;
            max-width: {content_height}mm;
            height: auto;
        }}
        """

    def _preview_css(self, orientation: str) -> str:
        page_width = A4_W_MM if orientation == "portrait" else A4_H_MM
        page_height = A4_H_MM if orientation == "portrait" else A4_W_MM
        return f"""
        body.preview-mode {{
            background-color: #e5e5ea;
            padding: 10px 0;
        }}
        body.preview-mode .nb-page {{
            max-width: none;
            margin: 0;
        }}
        .preview-pages {{
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 16px;
        }}
        .preview-page {{
            width: {page_width}mm;
            height: {page_height}mm;
            padding: {self.margin_mm}mm;
            background: #fff;
            box-shadow: 0 2px 10px rgba(0,0,0,0.12);
            box-sizing: border-box;
            overflow: hidden;
            position: relative;
        }}
        .preview-page-footer {{
            position: absolute;
            bottom: {self.margin_mm * 0.6}mm;
            left: {self.margin_mm}mm;
            right: {self.margin_mm}mm;
            text-align: center;
            font-size: 9pt;
            color: #666;
        }}
        body.preview-mode .nb-image img {{
            max-height: {max(20.0, page_height - 2 * self.margin_mm - 12)}mm;
            object-fit: contain;
        }}
        """

    def _preview_pagination_script(self, orientation: str) -> str:
        page_width = A4_W_MM if orientation == "portrait" else A4_H_MM
        page_height = A4_H_MM if orientation == "portrait" else A4_W_MM
        return f"""
        <script>
        (function() {{
            function paginate() {{
                try {{
                    var content = document.getElementById('nb-content');
                    if (!content) return;
                    var header = document.querySelector('.nb-page-header');
                    var children = [];
                    Array.prototype.slice.call(content.children).forEach(function(child) {{
                        if (child.classList.contains('nb-db-group')) {{
                            Array.prototype.slice.call(child.children).forEach(function(part) {{ children.push(part); }});
                        }} else {{ children.push(child); }}
                    }});

                    var pagesContainer = document.createElement('div');
                    pagesContainer.className = 'preview-pages';
                    content.innerHTML = '';
                    content.appendChild(pagesContainer);

                    var currentPage = document.createElement('div');
                    currentPage.className = 'preview-page';
                    pagesContainer.appendChild(currentPage);

                    if (header) currentPage.appendChild(header);
                    function newPage() {{
                        currentPage = document.createElement('div');
                        currentPage.className = 'preview-page';
                        pagesContainer.appendChild(currentPage);
                    }}
                    function overflows() {{ return currentPage.scrollHeight > currentPage.clientHeight + 1; }}
                    function appendAtomic(child) {{
                        currentPage.appendChild(child);
                        if (overflows() && currentPage.children.length > 1) {{
                            newPage(); currentPage.appendChild(child);
                        }}
                        if (overflows() && child.querySelector && child.querySelector('img')) {{
                            var image = child.querySelector('img');
                            image.style.maxHeight = Math.max(80, currentPage.clientHeight - 80) + 'px';
                            image.style.objectFit = 'contain';
                        }}
                    }}
                    function appendList(list) {{
                        var items = Array.prototype.slice.call(list.children), shell = list.cloneNode(false);
                        currentPage.appendChild(shell);
                        items.forEach(function(item) {{
                            shell.appendChild(item);
                            if (overflows() && shell.children.length > 1) {{
                                shell.removeChild(item); newPage(); shell = list.cloneNode(false);
                                currentPage.appendChild(shell); shell.appendChild(item);
                            }}
                        }});
                    }}
                    function appendGallery(gallery) {{
                        var cards = Array.prototype.slice.call(gallery.children), shell = gallery.cloneNode(false);
                        currentPage.appendChild(shell);
                        cards.forEach(function(card) {{
                            shell.appendChild(card);
                            if (overflows()) {{
                                if (shell.children.length > 1) {{
                                    shell.removeChild(card); newPage(); shell = gallery.cloneNode(false);
                                    currentPage.appendChild(shell); shell.appendChild(card);
                                }} else if (currentPage.children.length > 1) {{
                                    var label = shell.previousElementSibling;
                                    if (!label || !label.classList.contains('nb-db-group-title')) label = null;
                                    shell.remove(); if (label) label.remove(); newPage();
                                    if (label) currentPage.appendChild(label);
                                    currentPage.appendChild(shell);
                                }}
                            }}
                        }});
                    }}
                    function appendGroupGrid(grid) {{
                        var groups = Array.prototype.slice.call(grid.children), shell = grid.cloneNode(false);
                        currentPage.appendChild(shell);
                        groups.forEach(function(group) {{
                            shell.appendChild(group);
                            if (overflows()) {{
                                if (shell.children.length > 1) {{
                                    shell.removeChild(group); newPage(); shell = grid.cloneNode(false);
                                    currentPage.appendChild(shell); shell.appendChild(group);
                                }} else if (currentPage.children.length > 1) {{
                                    shell.remove(); newPage(); shell = grid.cloneNode(false);
                                    currentPage.appendChild(shell); shell.appendChild(group);
                                }}
                                if (overflows() && shell.children.length === 1) {{
                                    shell.remove();
                                    var parts = Array.prototype.slice.call(group.children);
                                    parts.forEach(function(part) {{
                                        if (part.classList.contains('nb-db-gallery')) appendGallery(part);
                                        else appendAtomic(part);
                                    }});
                                    shell = grid.cloneNode(false);
                                    currentPage.appendChild(shell);
                                }}
                            }}
                        }});
                        if (!shell.children.length) shell.remove();
                    }}
                    function appendTable(wrapper) {{
                        var table = wrapper.matches('table') ? wrapper : wrapper.querySelector('table');
                        if (!table) {{ appendAtomic(wrapper); return; }}
                        var rows = Array.prototype.slice.call(table.querySelectorAll('tr'));
                        if (!rows.length) {{ appendAtomic(wrapper); return; }}
                        var head = rows[0].querySelector('th') ? rows.shift() : null;
                        var outer = null;
                        function makeTable() {{
                            outer = wrapper.cloneNode(false); var next = table.cloneNode(false);
                            if (outer.matches('table')) outer = next; else outer.appendChild(next);
                            if (head) next.appendChild(head.cloneNode(true));
                            currentPage.appendChild(outer); return next;
                        }}
                        var next = makeTable();
                        rows.forEach(function(row) {{
                            next.appendChild(row);
                            if (overflows()) {{
                                if (next.children.length > (head ? 2 : 1)) {{
                                    next.removeChild(row); newPage(); next = makeTable(); next.appendChild(row);
                                }} else if (currentPage.children.length > 1) {{
                                    var label = outer.previousElementSibling;
                                    if (!label || !label.classList.contains('nb-db-group-title')) label = null;
                                    outer.remove(); if (label) label.remove(); newPage();
                                    if (label) currentPage.appendChild(label);
                                    currentPage.appendChild(outer);
                                }}
                            }}
                        }});
                    }}
                    children.forEach(function(child) {{
                        if (child.classList.contains('nb-db-group-grid')) appendGroupGrid(child);
                        else if (child.classList.contains('nb-db-gallery')) appendGallery(child);
                        else if (child.classList.contains('nb-db-table') || child.matches('table.nb-table')) appendTable(child);
                        else if (child.matches('ul, ol')) appendList(child);
                        else appendAtomic(child);
                    }});

                    // Add page number footers
                    Array.prototype.forEach.call(pagesContainer.querySelectorAll('.preview-page'), function(page, idx) {{
                        var footer = document.createElement('div');
                        footer.className = 'preview-page-footer';
                        footer.textContent = String(idx + 1);
                        page.appendChild(footer);
                    }});

                }} catch (err) {{
                    console.error('Preview pagination failed:', err);
                }}
            }}
            if (document.readyState === 'loading') {{
                document.addEventListener('DOMContentLoaded', function() {{ setTimeout(paginate, 0); }});
            }} else {{
                setTimeout(paginate, 0);
            }}
        }})();
        </script>
        """

    def _rotation_script(self, orientation: str) -> str:
        if orientation == "landscape":
            return ""
        return """
        <script>
        (function() {
            function wrapWideTables() {
                document.querySelectorAll('.nb-table').forEach(function(table) {
                    if (table.closest('.wide-rotate-wrapper')) return;
                    var parent = table.parentElement;
                    if (!parent) return;
                    var parentWidth = parent.clientWidth;
                    // Use scrollWidth as a proxy for natural table width.
                    if (table.scrollWidth > parentWidth + 2) {
                        var wrapper = document.createElement('div');
                        wrapper.className = 'wide-rotate-wrapper';
                        parent.insertBefore(wrapper, table);
                        wrapper.appendChild(table);
                    }
                });
            }
            if (document.readyState === 'loading') {
                document.addEventListener('DOMContentLoaded', wrapWideTables);
            } else {
                wrapWideTables();
            }
        })();
        </script>
        """

    def export_pdf(
        self,
        page_url_or_id: str,
        output_path: str | None = None,
        orientation: str | None = None,
        full_width: bool | None = None,
        view_type: str | None = None,
        group_by: str | None = None,
        gallery_fit: bool | None = None,
        gallery_size: str | None = None,
        database_options: dict[str, dict] | None = None,
    ) -> str:
        data = self.fetch_page_data(page_url_or_id)
        return self.export_pdf_from_data(
            data,
            output_path=output_path,
            orientation=orientation,
            full_width=full_width,
            view_type=view_type,
            group_by=group_by,
            gallery_fit=gallery_fit,
            gallery_size=gallery_size,
            database_options=database_options,
        )

    def export_pdf_from_data(
        self,
        data: dict,
        output_path: str | None = None,
        orientation: str | None = None,
        full_width: bool | None = None,
        view_type: str | None = None,
        group_by: str | None = None,
        gallery_fit: bool | None = None,
        gallery_size: str | None = None,
        database_options: dict[str, dict] | None = None,
    ) -> str:
        """Export already fetched page data without any Notion API requests."""
        result = self.render_html(
            data,
            orientation=orientation,
            full_width=full_width,
            view_type=view_type,
            group_by=group_by,
            gallery_fit=gallery_fit,
            gallery_size=gallery_size,
            database_options=database_options,
        )
        html_doc = result["html"]
        orientation = result["orientation"]
        title = result["title"]

        if not output_path:
            safe_title = "".join(c if c.isalnum() or c in "-_ " else "_" for c in title).strip() or "notion"
            output_path = f"{safe_title}.pdf"

        html_path = Path(tempfile.gettempdir()) / f"notion_pdf_{os.getpid()}.html"
        html_path.write_text(html_doc, encoding="utf-8")

        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page()
                page.goto(f"file:///{html_path.as_posix()}")
                page.wait_for_load_state("networkidle")
                page.pdf(
                    path=output_path,
                    format="A4",
                    landscape=(orientation == "landscape"),
                    print_background=True,
                    margin={"top": "0", "bottom": "0", "left": "0", "right": "0"},
                )
                browser.close()
        except Exception as exc:
            LOGGER.error("PDF generation failed: %s", exc)
            raise
        finally:
            try:
                html_path.unlink()
            except OSError:
                pass
        return output_path

    def export_html_from_data(
        self,
        data: dict,
        output_path: str,
        orientation: str | None = None,
        full_width: bool | None = None,
        view_type: str | None = None,
        group_by: str | None = None,
        gallery_fit: bool | None = None,
        gallery_size: str | None = None,
        database_options: dict[str, dict] | None = None,
    ) -> str:
        """Write standalone, print-ready HTML from already fetched page data."""
        result = self.render_html(
            data,
            orientation=orientation,
            full_width=full_width,
            view_type=view_type,
            group_by=group_by,
            gallery_fit=gallery_fit,
            gallery_size=gallery_size,
            database_options=database_options,
        )
        Path(output_path).write_text(result["html"], encoding="utf-8")
        return output_path
