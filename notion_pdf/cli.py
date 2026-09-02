"""Command line interface."""

import argparse
import json
import logging
import sys
from pathlib import Path

from .api import NotionClient
from .cache import PageDataCache
from .renderer import PageRenderer


def resolve_token(token_value: str) -> str:
    """Resolve token from a raw string or a file.

    - If token_value starts with '@', the remainder is treated as a file path.
    - Otherwise, if token_value is a path to an existing file, read that file.
    - Otherwise, return the value as-is.
    File contents are stripped of surrounding whitespace and newlines.
    """
    raw_path = token_value[1:] if token_value.startswith("@") else token_value
    path = Path(raw_path)
    if path.exists() and path.is_file():
        return path.read_text(encoding="utf-8").strip()
    if token_value.startswith("@"):
        raise FileNotFoundError(f"Token file not found: {path}")
    return token_value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="notion-pdf",
        description="Export a Notion page to a styled PDF.",
    )
    parser.add_argument("url", help="Notion page URL or page id")
    parser.add_argument(
        "--token", "-t", required=True,
        help="Notion integration token. If the value is an existing file path (or prefixed with '@'), its contents are read and stripped."
    )
    parser.add_argument("--output", "-o", default=None, help="Output PDF or HTML path")
    parser.add_argument(
        "--orientation",
        choices=["auto", "portrait", "landscape"],
        default="auto",
        help="Page orientation. 'auto' selects landscape if --full-width is set.",
    )
    parser.add_argument(
        "--full-width",
        action="store_true",
        default=None,
        help="Mark the page as full-width (forces landscape when orientation=auto).",
    )
    parser.add_argument(
        "--no-full-width",
        dest="full_width",
        action="store_false",
        default=None,
        help="Mark the page as not full-width.",
    )
    parser.add_argument(
        "--view-type",
        choices=["auto", "table", "gallery", "list"],
        default="auto",
        help="Override how child databases render; auto follows their selected Notion view.",
    )
    parser.add_argument(
        "--group",
        default=None,
        help="Group database entries by this property name. Only databases that have this property are grouped.",
    )
    parser.add_argument(
        "--gallery-fit",
        action="store_true",
        dest="gallery_fit",
        help="Fit gallery cover images entirely inside the card frame (object-fit: contain) instead of cropping.",
    )
    parser.add_argument(
        "--gallery-size",
        choices=["small", "medium", "large"],
        default="medium",
        dest="gallery_size",
        help="Gallery card size (default: medium).",
    )
    parser.add_argument(
        "--margin",
        type=float,
        default=15.0,
        help="Page margin in millimeters (default: 15).",
    )
    parser.add_argument(
        "--page-size",
        default="A4",
        help="Page size (default: A4).",
    )
    parser.add_argument(
        "--html", "--html-only",
        dest="html_only",
        action="store_true",
        help="Save standalone HTML instead of PDF.",
    )
    parser.add_argument(
        "--offline", action="store_true",
        help="Use cached pageData only; do not contact Notion.",
    )
    parser.add_argument(
        "--database-config", type=Path,
        help="JSON file containing per-database export options keyed by database id.",
    )
    parser.add_argument(
        "--database-view", action="append", default=[], metavar="DATABASE_ID=VIEW_ID",
        help="Select a Notion view for one database. Repeat for multiple databases.",
    )
    parser.add_argument(
        "--database-group", action="append", default=[], metavar="DATABASE_ID=PROPERTY",
        help="Override grouping for one database. Use an empty value to disable grouping.",
    )
    parser.add_argument(
        "--database-gallery-size", action="append", default=[], metavar="DATABASE_ID=SIZE",
        help="Set small, medium, large, or follow for one database.",
    )
    parser.add_argument(
        "--database-media-fit", action="append", default=[], metavar="DATABASE_ID=MODE",
        help="Set contain, cover, or follow for one database.",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print debug logs.",
    )
    return parser


def _assign_database_options(options: dict, values: list[str], key: str, convert=lambda value: value):
    for item in values:
        if "=" not in item:
            raise ValueError(f"Expected DATABASE_ID=value, got: {item}")
        database_id, value = item.split("=", 1)
        if not database_id:
            raise ValueError(f"Missing database id in: {item}")
        options.setdefault(database_id, {})[key] = convert(value)


def database_options_from_args(args) -> dict[str, dict]:
    options = {}
    if args.database_config:
        options = json.loads(args.database_config.read_text(encoding="utf-8"))
        if not isinstance(options, dict):
            raise ValueError("--database-config must contain a JSON object")
    _assign_database_options(options, args.database_view, "view_id")
    _assign_database_options(options, args.database_group, "group_by")
    def gallery_size(value: str):
        if value not in ("follow", "small", "medium", "large"):
            raise ValueError(f"Invalid gallery size: {value}")
        return None if value == "follow" else value
    _assign_database_options(options, args.database_gallery_size, "gallery_size", gallery_size)
    _assign_database_options(
        options, args.database_media_fit, "gallery_fit",
        lambda value: None if value == "follow" else {"contain": True, "cover": False}[value],
    )
    return options


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s: %(message)s")

    token = resolve_token(args.token)
    cache = PageDataCache()
    client = NotionClient(token)
    try:
        database_options = database_options_from_args(args)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    renderer = PageRenderer(
        client=client,
        orientation=args.orientation,
        full_width=args.full_width,
        view_type=args.view_type,
        group_by=args.group,
        gallery_fit=args.gallery_fit,
        gallery_size=args.gallery_size,
        margin_mm=args.margin,
        page_size=args.page_size,
    )

    try:
        cached = cache.load(args.url, token)
        if args.offline:
            if not cached:
                raise RuntimeError("No cached pageData is available for this page and token.")
            data = cached[0]
            logging.info("Using offline pageData cache.")
        else:
            try:
                data = renderer.fetch_page_data(args.url)
                cache.save(args.url, token, data)
            except Exception:
                if not cached:
                    raise
                logging.warning("Notion refresh failed; using cached pageData.")
                data = cached[0]
        if args.html_only:
            if args.output:
                output = args.output
            else:
                title = renderer._page_title(data["page"])
                safe_title = "".join(c if c.isalnum() or c in "-_ " else "_" for c in title).strip()
                output = f"{safe_title or 'notion_export'}.html"
            renderer.export_html_from_data(
                data, output,
                orientation=args.orientation,
                full_width=args.full_width,
                view_type=args.view_type,
                group_by=args.group,
                gallery_fit=args.gallery_fit,
                gallery_size=args.gallery_size,
                database_options=database_options,
            )
            print(f"HTML written to {output}")
        else:
            path = renderer.export_pdf_from_data(
                data, args.output,
                orientation=args.orientation,
                full_width=args.full_width,
                view_type=args.view_type,
                group_by=args.group,
                gallery_fit=args.gallery_fit,
                gallery_size=args.gallery_size,
                database_options=database_options,
            )
            print(f"PDF written to {path}")
    except Exception as exc:
        logging.error("Export failed: %s", exc)
        if args.verbose:
            logging.exception("Details")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
