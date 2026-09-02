"""Utility helpers."""

import re
import uuid
from urllib.parse import urlparse, unquote


NOTION_PAGE_RE = re.compile(
    r"(?:^|/)([0-9a-fA-F]{32})(?:\?|#|$)|"
    r"(?:^|/)([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})(?:\?|#|$)|"
    r"-([0-9a-fA-F]{32})(?:\?|#|$)"
)


def extract_page_id(url: str) -> str:
    """Extract a raw 32-char Notion page id from a URL."""
    parsed = urlparse(url.strip())
    path = unquote(parsed.path)
    match = NOTION_PAGE_RE.search(path)
    if not match:
        raise ValueError(f"Could not extract Notion page id from URL: {url}")
    raw = match.group(1) or match.group(2) or match.group(3)
    if not raw:
        raise ValueError(f"Could not extract Notion page id from URL: {url}")
    # normalize uuid form to 32-char raw id
    return raw.replace("-", "").lower()


def format_uuid(raw_id: str) -> str:
    """Convert 32-char raw id to hyphenated uuid for Notion API."""
    r = raw_id.replace("-", "").lower()
    return str(uuid.UUID(r))


def plain_text_from_rich(rich_texts: list) -> str:
    """Concatenate plain text from a Notion rich_text array."""
    return "".join(part.get("plain_text", "") for part in (rich_texts or []))


def rich_text_to_html(rich_texts: list) -> str:
    """Convert Notion rich_text annotations to inline HTML."""
    if not rich_texts:
        return ""
    out = ""
    for part in rich_texts:
        text = _escape(part.get("plain_text", ""))
        if not text:
            continue
        annotations = part.get("annotations", {})
        link = part.get("href")
        if annotations.get("code"):
            text = f"<code>{_escape(text)}</code>"
        if annotations.get("bold"):
            text = f"<strong>{text}</strong>"
        if annotations.get("italic"):
            text = f"<em>{text}</em>"
        if annotations.get("strikethrough"):
            text = f"<s>{text}</s>"
        if annotations.get("underline"):
            text = f"<u>{text}</u>"
        if annotations.get("color") and annotations["color"] != "default":
            color_name = annotations["color"]
            if color_name.endswith("_background"):
                bg = _notion_color(color_name)
                text = f'<span style="background-color:{bg};color:#1a1a1a;padding:0.05em 0.25em;border-radius:3px">{text}</span>'
            else:
                fg = _notion_color(color_name)
                text = f'<span style="color:{fg}">{text}</span>'
        if link:
            text = f'<a href="{_escape(link)}">{text}</a>'
        out += text
    return out


def _escape(value: str) -> str:
    return (value
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


def _notion_color(color_name: str) -> str:
    """Map Notion color names to CSS colors."""
    mapping = {
        "gray": "#9B9A97",
        "brown": "#64473A",
        "orange": "#D9730D",
        "yellow": "#DFAB01",
        "green": "#0F7B6C",
        "blue": "#0B6E99",
        "purple": "#6940A5",
        "pink": "#AD1A72",
        "red": "#E03E3E",
        "gray_background": "#EBECED",
        "brown_background": "#E9E5E3",
        "orange_background": "#FAEBDD",
        "yellow_background": "#FBF3DB",
        "green_background": "#EDF3EC",
        "blue_background": "#E7F3F8",
        "purple_background": "#F3F0F7",
        "pink_background": "#F9EEF3",
        "red_background": "#FDEBEC",
    }
    return mapping.get(color_name, color_name)


def property_value_to_str(value: dict) -> str:
    """Convert a Notion page property value dict to a display string."""
    if not value:
        return ""
    ptype = value.get("type", "")
    if ptype == "title":
        return plain_text_from_rich(value.get("title", []))
    if ptype == "rich_text":
        return plain_text_from_rich(value.get("rich_text", []))
    if ptype == "number":
        v = value.get("number")
        return "" if v is None else str(v)
    if ptype == "select":
        s = value.get("select")
        return s.get("name", "") if s else ""
    if ptype == "multi_select":
        return ", ".join(item.get("name", "") for item in value.get("multi_select", []) or [])
    if ptype == "status":
        s = value.get("status")
        return s.get("name", "") if s else ""
    if ptype == "date":
        d = value.get("date")
        if not d:
            return ""
        start = d.get("start", "")
        end = d.get("end", "")
        if end:
            return f"{start} → {end}"
        return start
    if ptype == "people":
        return ", ".join(p.get("name", "") or p.get("person", {}).get("email", "")
                       for p in value.get("people", []) or [])
    if ptype == "files":
        names = []
        for f in value.get("files", []) or []:
            if f.get("type") == "external":
                names.append(f.get("external", {}).get("url", "") or f.get("name", ""))
            else:
                names.append(f.get("name", ""))
        return ", ".join(names)
    if ptype == "checkbox":
        return "☑" if value.get("checkbox") else "☐"
    if ptype == "url":
        return value.get("url", "")
    if ptype == "email":
        return value.get("email", "")
    if ptype == "phone_number":
        return value.get("phone_number", "")
    if ptype == "formula":
        fv = value.get("formula", {})
        return str(fv.get("string", fv.get("number", "")))
    if ptype == "relation":
        return ", ".join(r.get("id", "") for r in value.get("relation", []) or [])
    if ptype == "rollup":
        rv = value.get("rollup", {})
        if rv.get("type") == "array":
            return ", ".join(property_value_to_str(v) for v in rv.get("array", []))
        return str(rv.get("number", rv.get("date", rv.get("string", ""))))
    if ptype == "created_time":
        return value.get("created_time", "")
    if ptype == "created_by":
        return value.get("created_by", {}).get("name", "")
    if ptype == "last_edited_time":
        return value.get("last_edited_time", "")
    if ptype == "last_edited_by":
        return value.get("last_edited_by", {}).get("name", "")
    if ptype == "button":
        return "[button]"
    if ptype == "unique_id":
        uid = value.get("unique_id", {})
        prefix = uid.get("prefix", "")
        number = uid.get("number", "")
        return f"{prefix}-{number}" if prefix else str(number)
    if ptype == "verification":
        return ""
    return ""


def format_number(value: int | float | None, number_format: str = "number") -> str:
    if value is None:
        return ""
    compact = f"{value:g}" if isinstance(value, float) else str(value)
    comma = f"{value:,}"
    formats = {
        "percent": lambda: f"{value * 100:g}%",
        "dollar": lambda: f"${comma}",
        "euro": lambda: f"€{comma}",
        "pound": lambda: f"£{comma}",
        "yen": lambda: f"¥{comma}",
        "yuan": lambda: f"¥{comma}",
        "won": lambda: f"₩{comma}",
        "ruble": lambda: f"₽{comma}",
        "rupee": lambda: f"₹{comma}",
        "real": lambda: f"R${comma}",
        "franc": lambda: f"CHF {comma}",
        "lira": lambda: f"₺{comma}",
        "krona": lambda: f"kr {comma}",
        "ringgit": lambda: f"RM {comma}",
        "number_with_commas": lambda: comma,
    }
    return formats.get(number_format, lambda: compact)()


def property_value_to_html(value: dict, schema_property: dict | None = None) -> str:
    """Render a page property using its data-source formatting metadata."""
    if not value:
        return ""
    schema_property = schema_property or {}
    ptype = value.get("type", schema_property.get("type", ""))
    if ptype in ("title", "rich_text"):
        return rich_text_to_html(value.get(ptype, []))
    if ptype == "number":
        fmt = schema_property.get("number", {}).get("format", "number")
        return _escape(format_number(value.get("number"), fmt))
    if ptype in ("select", "status"):
        option = value.get(ptype)
        return _option_tag(option) if option else ""
    if ptype == "multi_select":
        return "".join(_option_tag(item) for item in value.get("multi_select", []) or [])
    if ptype == "checkbox":
        checked = value.get("checkbox", False)
        return f'<span class="nb-checkbox {"checked" if checked else ""}">{"✓" if checked else ""}</span>'
    if ptype in ("url", "email", "phone_number"):
        raw = property_value_to_str(value)
        if not raw:
            return ""
        href = raw
        if ptype == "email":
            href = f"mailto:{raw}"
        elif ptype == "phone_number":
            href = f"tel:{raw}"
        return f'<a href="{_escape(href)}">{_escape(raw)}</a>'
    if ptype == "formula":
        formula = value.get("formula", {})
        if formula.get("type") == "number":
            return _escape(format_number(formula.get("number")))
    return _escape(property_value_to_str(value))


def _option_tag(option: dict) -> str:
    name = _escape(option.get("name", ""))
    color = option.get("color", "default")
    return f'<span class="nb-option nb-option-{_escape(color)}">{name}</span>'


def is_full_width_from_url_or_default(url: str, explicit: bool | None) -> bool:
    """Return whether page is full-width. Falls back to explicit flag, then False."""
    if explicit is not None:
        return explicit
    # Notion API does not expose full-width; no reliable way to infer from URL.
    return False
