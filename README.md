# Notion-PDF

A Python tool that exports a Notion page to a styled PDF using the Notion API and a headless browser renderer. It includes both a command-line interface and a visual GUI with live A4 preview.

## Features

- Renders page properties as a compact info bar under the title.
- Supports common Notion blocks: paragraphs, headings, lists, toggles, callouts, code, tables, images, columns, bookmarks, embeds, equations, etc.
- Reads each child database's Notion views and renders its selected table / gallery / list configuration.
- Preserves view sorting, grouping order, visible properties and property presentation metadata.
- Formats numbers, select/status values and rich text according to the data-source schema.
- Automatic A4 orientation:
  - Default portrait A4.
  - `--full-width` switches default orientation to landscape A4.
  - Wide tables are rotated 90° in portrait mode and scaled to fit.
  - Adaptive-width blocks (e.g. gallery) use CSS grid to fill the content width.
- Group database entries by a shared property.
- Page numbers in the footer.
- Preserves rich text formatting (bold, italic, code, links, colors).

## Installation

```bash
cd C:\Users\admin\Documents\Bertie\Notion-PDF
pip install -r requirements.txt
playwright install chromium
```

## CLI usage

```bash
python -m notion_pdf "https://www.notion.so/YourPageTitle-0123456789abcdef0123456789abcdef" \
  --token "secret_xxx" \
  -o output.pdf
```

Token can also be read from a file (whitespace around the token is stripped). Both `--token token.txt` and `--token @token.txt` work as long as `token.txt` exists:

```bash
echo "secret_xxx" > token.txt
python -m notion_pdf "https://www.notion.so/YourPageTitle-0123456789abcdef0123456789abcdef" \
  --token token.txt \
  -o output.pdf
```

### CLI options

| Option | Description |
|--------|-------------|
| `--token, -t` | Notion integration token (required). If the value is an existing file path (or prefixed with `@`), the file contents are read and stripped. |
| `--output, -o` | Output PDF path |
| `--orientation` | `auto` (default), `portrait`, `landscape` |
| `--full-width` | Declare page as full-width (forces landscape when orientation=auto) |
| `--no-full-width` | Declare page as not full-width |
| `--view-type` | `auto` (default), `table`, `gallery`, `list` |
| `--group` | Group database entries by this property. Only databases that actually have this property are grouped. |
| `--gallery-fit` | Fit gallery cover images entirely inside the card (`object-fit: contain`) instead of cropping. |
| `--gallery-size` | Gallery card size: `small`, `medium` (default), `large`. |
| `--margin` | Page margin in mm (default: 15) |
| `--html-only` | Dump intermediate HTML for debugging |
| `--html` | Save standalone HTML instead of PDF (`--html-only` remains an alias) |
| `--offline` | Export only from the local pageData cache without contacting Notion |
| `--database-config` | Load per-database view/group/gallery settings from JSON |
| `--database-view` | Select a view for one database (`DATABASE_ID=VIEW_ID`, repeatable) |
| `--database-group` | Select a grouping property for one database |
| `--database-gallery-size` | Set `follow`, `small`, `medium`, or `large` per database |
| `--database-media-fit` | Set `follow`, `contain`, or `cover` per database |
| `--verbose, -v` | Debug logging |

### CLI example

```bash
python -m notion_pdf "https://www.notion.so/MyProject-abc123..." \
  --token "secret_xxx" \
  --orientation auto \
  --view-type gallery \
  --gallery-fit \
  --group "Status" \
  --margin 12 \
  -o my_project.pdf
```

Save HTML or export from cached pageData without network access:

```bash
python -m notion_pdf "https://www.notion.so/MyProject-abc123..." --token token.txt --html -o project.html
python -m notion_pdf "https://www.notion.so/MyProject-abc123..." --token token.txt --offline -o project.pdf
```

## GUI usage (recommended)

A PySide6-based visual interface is included. It loads Notion data and previews the rendered A4 output automatically.

```bash
python -m notion_pdf.gui
```

### How to use

1. Paste the Notion page URL and provide the token:
   - Enable **使用口令文件** to browse for a token file, or
   - Disable it to paste the token directly.
2. The page loads automatically once both URL and token are present.
3. Adjust export options on the left:
   - **页面设置** controls full width, orientation and margin.
   - **数据库设置** creates one tab per child database.
   - Each database tab selects one of its existing Notion views and controls grouping and gallery presentation independently.
4. The right panel shows a paginated A4 preview that updates live as you change options.
5. Click the **Export PDF** floating button in the bottom-right corner of the preview to save the PDF.

### GUI features

- Automatic page load when URL and token are ready.
- Incremental rendering: top-level content appears first, then nested children and databases are filled in as they load.
- A4 paginated preview with page numbers, matching the exported PDF layout as closely as possible.
- Default output filename is the Notion page title; you can override it with the **Output PDF** field.
- Token source, file path, pasted token, and export options are remembered via `QSettings`.
- Loading and export run in background threads so the UI stays responsive.
- Internal logs are written to `C:\Users\<username>\.notion_pdf\logs\gui_YYYYMMDD_HHMMSS.log` for troubleshooting.

## Known limitations

- The Notion API does not expose a user's live/active view or the page `full width` setting. The GUI therefore selects the first available view initially and lets the user choose another.
- Board, calendar, timeline, chart, map and dashboard views currently fall back to a table layout in PDF while retaining their supported property, sorting and grouping metadata.
- Very complex embedded content (Figma, maps, etc.) is rendered as a link.
- Page cover images are not currently rendered as a banner.
