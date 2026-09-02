"""Block parsing and database rendering helpers."""

import html
import logging
from urllib.parse import unquote
from typing import Any

from .utils import (
    plain_text_from_rich,
    property_value_to_html,
    property_value_to_str,
    rich_text_to_html,
)

LOGGER = logging.getLogger(__name__)


def block_caption(block: dict) -> str:
    return rich_text_to_html(block.get(block["type"], {}).get("caption", []))


def render_block(block: dict, ctx: dict) -> str:
    """Render a single Notion block as HTML."""
    btype = block.get("type", "")
    data = block.get(btype, {})

    if btype == "paragraph":
        text = rich_text_to_html(data.get("rich_text", []))
        if not text:
            return '<div class="nb-paragraph empty">&nbsp;</div>'
        return f'<div class="nb-paragraph">{text}</div>'

    if btype in ("heading_1", "heading_2", "heading_3"):
        tag = f"h{btype[-1]}"
        text = rich_text_to_html(data.get("rich_text", []))
        color = data.get("color", "default")
        style = f'color:{_color(data.get("color", "default"))}' if color != "default" else ""
        return f'<{tag} class="nb-{btype}"{" style=\""+style+"\"" if style else ""}>{text}</{tag}>'

    if btype == "bulleted_list_item":
        text = rich_text_to_html(data.get("rich_text", []))
        return f'<li class="nb-bulleted">{text}</li>'

    if btype == "numbered_list_item":
        text = rich_text_to_html(data.get("rich_text", []))
        return f'<li class="nb-numbered">{text}</li>'

    if btype == "to_do":
        text = rich_text_to_html(data.get("rich_text", []))
        checked = "checked" if data.get("checked") else ""
        return (
            f'<div class="nb-todo">'
            f'<input type="checkbox" disabled {checked}/>'
            f'<span>{text}</span></div>'
        )

    if btype == "toggle":
        text = rich_text_to_html(data.get("rich_text", []))
        inner = "".join(render_block(child, ctx) for child in block.get("children", []))
        return f'<div class="nb-toggle"><div class="nb-toggle-title">▶ {text}</div><div class="nb-toggle-body">{inner}</div></div>'

    if btype == "quote":
        text = rich_text_to_html(data.get("rich_text", []))
        return f'<blockquote class="nb-quote">{text}</blockquote>'

    if btype == "callout":
        text = rich_text_to_html(data.get("rich_text", []))
        icon = render_icon(data.get("icon"))
        color = data.get("color", "default")
        bg = _callout_background(color)
        style = f' style="background-color:{bg}"' if bg else ""
        return f'<div class="nb-callout"{style}><div class="nb-callout-icon">{icon}</div><div class="nb-callout-body">{text}</div></div>'

    if btype == "code":
        code = html.escape(plain_text_from_rich(data.get("rich_text", [])))
        language = data.get("language", "")
        return f'<pre class="nb-code"><code class="language-{language}">{code}</code></pre>'

    if btype == "divider":
        return '<hr class="nb-divider"/>'

    if btype == "image":
        src, alt = _image_src(block)
        if not src:
            return ""
        if src.startswith("data:"):
            img_tag = f'<img src="{src}" alt="{html.escape(alt)}"/>'
        else:
            # embed remote url as-is
            img_tag = f'<img src="{html.escape(src)}" alt="{html.escape(alt)}"/>'
        caption = block_caption(block)
        cap_html = f'<figcaption>{caption}</figcaption>' if caption else ""
        return f'<figure class="nb-image">{img_tag}{cap_html}</figure>'

    if btype == "video":
        return _external_link_block(block, "video", "[Video]")
    if btype == "file":
        return _external_link_block(block, "file", "[File]")
    if btype == "pdf":
        return _external_link_block(block, "pdf", "[PDF]")

    if btype == "bookmark":
        url = data.get("url", "")
        caption = rich_text_to_html(data.get("caption", []))
        title = html.escape(data.get("url", url))
        return f'<div class="nb-bookmark"><a href="{html.escape(url)}">{title}</a>{caption}</div>'

    if btype == "embed":
        url = data.get("url", "")
        return f'<div class="nb-embed"><a href="{html.escape(url)}">[Embed] {html.escape(url)}</a></div>'

    if btype == "equation":
        expr = html.escape(data.get("expression", ""))
        return f'<div class="nb-equation">$${expr}$$</div>'

    if btype == "table":
        return render_table_block(block)

    if btype == "column_list":
        columns = block.get("children", [])
        ratio = _column_ratios(columns)
        cols_html = ""
        for i, col in enumerate(columns):
            w = ratio[i]
            inner = "".join(render_block(child, ctx) for child in col.get("children", []))
            cols_html += f'<div class="nb-column" style="flex:{w}">{inner}</div>'
        return f'<div class="nb-column-list">{cols_html}</div>'

    if btype == "column":
        # handled inside column_list
        inner = "".join(render_block(child, ctx) for child in block.get("children", []))
        return f'<div class="nb-column">{inner}</div>'

    if btype == "link_to_page":
        pid = data.get("page_id") or data.get("database_id") or ""
        title = ctx.get("page_title_map", {}).get(pid, pid)
        return f'<div class="nb-link-to-page"><a href="https://www.notion.so/{pid.replace('-', '')}">→ {html.escape(title)}</a></div>'

    if btype == "child_page":
        title = html.escape(data.get("title", "Untitled"))
        pid = block["id"]
        return f'<div class="nb-child-page"><a href="https://www.notion.so/{pid.replace('-', '')}">→ {title}</a></div>'

    if btype == "child_database":
        # Will be handled by renderer with actual query results.
        db_id = block["id"]
        return f'<div class="nb-database-placeholder" data-db-id="{db_id}"></div>'

    if btype == "table_of_contents":
        return '<div class="nb-toc">[Table of contents]</div>'

    if btype == "breadcrumb":
        return ""

    # unsupported block
    LOGGER.warning("Unsupported block type: %s", btype)
    return f'<div class="nb-unsupported">[{btype}]</div>'


def render_table_block(block: dict) -> str:
    rows = block.get("children", [])
    if not rows:
        return ""
    table_width = block.get("table", {}).get("table_width")
    has_column_header = block.get("table", {}).get("has_column_header", False)
    has_row_header = block.get("table", {}).get("has_row_header", False)
    html_parts = ['<table class="nb-table">']
    for i, row in enumerate(rows):
        tag = "th" if (i == 0 and has_column_header) else "td"
        cells = row.get("table_row", {}).get("cells", [])
        html_parts.append("<tr>")
        for j, cell in enumerate(cells):
            cell_tag = "th" if (j == 0 and has_row_header and tag == "td") else tag
            content = rich_text_to_html(cell)
            html_parts.append(f"<{cell_tag}>{content}</{cell_tag}>")
        html_parts.append("</tr>")
    html_parts.append("</table>")
    return "".join(html_parts)


def render_database(db: dict, entries: list[dict], view_type: str, ctx: dict) -> str:
    """Render a database according to requested view type, optionally grouped."""
    entries = _sort_entries(entries, ctx.get("view", {}).get("sorts") or [])
    group_by = ctx.get("group_by")
    schema = db.get("properties", {})
    if group_by and group_by in schema:
        groups = _group_entries(entries, group_by, schema, ctx.get("view"))
        grouped_gallery = view_type == "gallery"
        size = ctx.get("gallery_size", "medium")
        capacity = {"small": 5, "medium": 3, "large": 2}.get(size, 3)
        parts = [f'<div class="nb-db-group-grid nb-db-group-grid-{size}">'] if grouped_gallery else []
        for group_key, group_entries in groups.items():
            span = min(capacity, max(1, len(group_entries)))
            style = f' style="--group-span:{span}"' if grouped_gallery else ""
            parts.append(f'<div class="nb-db-group"{style}>')
            parts.append(f'<div class="nb-db-group-title">{html.escape(str(group_key))}</div>')
            if view_type == "gallery":
                parts.append(render_database_gallery(db, group_entries, ctx))
            elif view_type == "list":
                parts.append(render_database_list(db, group_entries, ctx))
            else:
                parts.append(render_database_table(db, group_entries, ctx))
            parts.append('</div>')
        if grouped_gallery:
            parts.append('</div>')
        return "".join(parts)
    if view_type == "gallery":
        return render_database_gallery(db, entries, ctx)
    if view_type == "list":
        return render_database_list(db, entries, ctx)
    return render_database_table(db, entries, ctx)


def _group_entries(entries: list[dict], group_by: str, schema: dict,
                   view: dict | None = None) -> dict[str, list[dict]]:
    """Group entries in the property's configured option order."""
    groups: dict[str, list[dict]] = {}
    for entry in entries:
        key = _group_key(entry, group_by, schema)
        groups.setdefault(key, []).append(entry)
    prop_schema = schema.get(group_by, {})
    ptype = prop_schema.get("type", "")
    options = prop_schema.get(ptype, {}).get("options", [])
    order = {option.get("name", ""): index for index, option in enumerate(options)}
    group_cfg = (view or {}).get("configuration", {}).get("group_by", {}) or {}
    sort_cfg = group_cfg.get("sort", {}) or {}
    sort_type = sort_cfg.get("type", "manual")
    if sort_type in ("ascending", "alphabetical"):
        key_fn = lambda item: (item[0] == "(empty)", item[0].casefold())
    elif sort_type == "descending":
        key_fn = lambda item: (item[0] != "(empty)", item[0].casefold())
    else:
        key_fn = lambda item: (item[0] == "(empty)", order.get(item[0], len(order)), item[0].casefold())
    return dict(sorted(groups.items(), key=key_fn, reverse=sort_type == "descending"))


def _sort_entries(entries: list[dict], sorts: list[dict]) -> list[dict]:
    ordered = list(entries)
    for spec in reversed(sorts):
        prop_name = spec.get("property")
        if not prop_name:
            continue
        reverse = spec.get("direction") == "descending"
        ordered.sort(
            key=lambda entry: (
                not bool(property_value_to_str(entry.get("properties", {}).get(prop_name, {}))),
                property_value_to_str(entry.get("properties", {}).get(prop_name, {})).casefold(),
            ),
            reverse=reverse,
        )
    return ordered


def _group_key(entry: dict, group_by: str, schema: dict) -> str:
    prop = entry.get("properties", {}).get(group_by, {})
    ptype = schema.get(group_by, {}).get("type", "")
    if ptype == "multi_select":
        items = prop.get("multi_select", [])
        return items[0].get("name", "(empty)") if items else "(empty)"
    val = property_value_to_str(prop)
    return val if val else "(empty)"


def render_database_table(db: dict, entries: list[dict], ctx: dict) -> str:
    schema = db.get("properties", {})
    # Order: title property first, then others in schema order
    props = _view_props(schema, ctx.get("view"))
    out = ['<div class="nb-db nb-db-table"><table class="nb-table wide-rotate">']
    out.append("<tr>")
    for prop_name in props:
        out.append(f"<th>{html.escape(prop_name)}</th>")
    out.append("</tr>")
    for entry in entries:
        out.append("<tr>")
        for prop_name in props:
            value = entry.get("properties", {}).get(prop_name, {})
            out.append(f"<td>{property_value_to_html(value, schema.get(prop_name))}</td>")
        out.append("</tr>")
    out.append("</table></div>")
    return "".join(out)


def render_database_gallery(db: dict, entries: list[dict], ctx: dict) -> str:
    schema = db.get("properties", {})
    props = _view_props(schema, ctx.get("view"))
    size = ctx.get("gallery_size", "medium")
    fit_class = "nb-db-gallery-fit" if ctx.get("gallery_fit") else ""
    size_class = f"nb-db-gallery-{size}"
    cards = []
    for idx, entry in enumerate(entries):
        title = _entry_title(entry, schema)
        cover = _entry_cover(entry, ctx)
        icon = _entry_icon(entry)
        fields = []
        for prop_name in props:
            if schema.get(prop_name, {}).get("type") == "title":
                continue
            value = entry.get("properties", {}).get(prop_name, {})
            val_html = property_value_to_html(value, schema.get(prop_name))
            if val_html:
                fields.append(
                    f'<div class="nb-db-gallery-field">'
                    f'<span class="nb-db-gallery-label">{html.escape(prop_name)}</span>'
                    f'<span class="nb-db-gallery-value">{val_html}</span>'
                    f'</div>'
                )
        cover_html = _gallery_cover_html(cover, icon, title, idx, ctx)
        card = (
            f'<div class="nb-db-gallery-card">{cover_html}'
            f'<div class="nb-db-gallery-body">'
            f'<div class="nb-db-gallery-title">{html.escape(title)}</div>'
            f'{"".join(fields)}</div></div>'
        )
        cards.append(card)
    return f'<div class="nb-db nb-db-gallery {fit_class} {size_class}">{"".join(cards)}</div>'


def _gallery_cover_html(cover: str, icon: str | None, title: str, idx: int, ctx: dict | None = None) -> str:
    fit_class = "nb-db-gallery-cover-fit" if (ctx and ctx.get("gallery_fit")) else ""
    if cover:
        return (
            f'<div class="nb-db-gallery-cover {fit_class}">'
            f'<img src="{html.escape(cover)}" alt="{html.escape(title)}"/>'
            f'</div>'
        )
    # No cover: use a vector panel with an icon or initial letter.
    hue = (idx * 37) % 360
    initial = html.escape(title[0].upper()) if title else "?"
    icon_html = render_icon(icon) if icon else f'<span class="nb-db-gallery-initial">{initial}</span>'
    return (
        f'<div class="nb-db-gallery-cover nb-db-gallery-cover-empty {fit_class}" style="--cover-hue:{hue}">'
        f'<div class="nb-db-gallery-cover-icon">{icon_html}</div>'
        f'</div>'
    )


def _entry_icon(entry: dict) -> dict | None:
    icon = entry.get("icon")
    if icon:
        return icon
    return None


def render_database_list(db: dict, entries: list[dict], ctx: dict) -> str:
    schema = db.get("properties", {})
    props = _view_props(schema, ctx.get("view"))
    items = []
    for entry in entries:
        title = _entry_title(entry, schema)
        fields = []
        for prop_name in props:
            if prop_name == title:
                continue
            value = entry.get("properties", {}).get(prop_name, {})
            val_html = property_value_to_html(value, schema.get(prop_name))
            if val_html:
                fields.append(f'<span class="nb-db-list-field">{html.escape(prop_name)}: {val_html}</span>')
        items.append(f'<li class="nb-db-list-item"><strong>{html.escape(title)}</strong>{" " + "".join(fields) if fields else ""}</li>')
    return f'<ul class="nb-db nb-db-list">{"".join(items)}</ul>'


def _ordered_props(schema: dict) -> list[str]:
    """Return property names with title first."""
    titles = [k for k, v in schema.items() if v.get("type") == "title"]
    others = [k for k, v in schema.items() if v.get("type") != "title"]
    return titles + others


def _view_props(schema: dict, view: dict | None) -> list[str]:
    config_props = ((view or {}).get("configuration") or {}).get("properties") or []
    if not config_props:
        return _ordered_props(schema)
    names_by_id = {unquote(str(prop.get("id", ""))): name for name, prop in schema.items()}
    visible = [
        names_by_id.get(unquote(str(item.get("property_id", ""))))
        for item in config_props
        if item.get("visible", True)
    ]
    return [name for name in visible if name in schema] or _ordered_props(schema)


def _entry_title(entry: dict, schema: dict) -> str:
    for name, prop in schema.items():
        if prop.get("type") == "title":
            return property_value_to_str(entry.get("properties", {}).get(name, {})) or "Untitled"
    return "Untitled"


def _entry_cover(entry: dict, ctx: dict | None = None) -> str:
    cover = entry.get("cover")
    if cover:
        if cover.get("type") == "external":
            return cover.get("external", {}).get("url", "")
        if cover.get("type") == "file":
            return cover.get("file", {}).get("url", "")
    # Fallback: use the first image block of the entry page as cover.
    eid = entry.get("id")
    if ctx and eid:
        blocks = ctx.get("entry_blocks_cache", {}).get(eid, [])
        for block in blocks:
            if block.get("type") == "image":
                url, _ = _image_src(block)
                if url:
                    return url
    return ""


def _image_src(block: dict):
    data = block.get("image", {})
    alt = plain_text_from_rich(data.get("caption", []))
    if data.get("type") == "external":
        return data.get("external", {}).get("url", ""), alt
    if data.get("type") == "file":
        return data.get("file", {}).get("url", ""), alt
    return "", alt


def _external_link_block(block: dict, key: str, label: str) -> str:
    data = block.get(key, {})
    url = ""
    if data.get("type") == "external":
        url = data.get("external", {}).get("url", "")
    elif data.get("type") == "file":
        url = data.get("file", {}).get("url", "")
    caption = block_caption(block)
    return f'<div class="nb-{key}"><a href="{html.escape(url)}">{label} {html.escape(url)}</a>{caption}</div>'


def render_icon(icon: dict | None) -> str:
    if not icon:
        return ""
    if icon.get("type") == "emoji":
        return icon.get("emoji", "")
    if icon.get("type") == "external":
        url = icon.get("external", {}).get("url", "")
        return f'<img class="nb-icon" src="{html.escape(url)}"/>'
    if icon.get("type") == "file":
        url = icon.get("file", {}).get("url", "")
        return f'<img class="nb-icon" src="{html.escape(url)}"/>'
    return ""


def _column_ratios(columns: list[dict]) -> list[float]:
    if not columns:
        return []
    return [1.0] * len(columns)


def _callout_background(color: str) -> str:
    """Return a suitable background color for a callout block."""
    from .utils import _notion_color
    if color == "default" or not color:
        return ""
    # Notion callouts use the background palette, so map foreground colors to it.
    bg_name = color if color.endswith("_background") else f"{color}_background"
    return _notion_color(bg_name)


def _color(color_name: str) -> str:
    from .utils import _notion_color
    return _notion_color(color_name)
