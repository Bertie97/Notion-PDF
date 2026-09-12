import tempfile
import unittest
from unittest.mock import patch

from notion_pdf.blocks import (
    _group_entries,
    _notion_resized_image_url,
    _sort_entries,
    _sort_entries_by_page_id,
    _sort_entries_by_property,
    _view_props,
    render_database_gallery,
)
from notion_pdf.utils import format_number, property_value_to_html
from notion_pdf.renderer import PageRenderer


class PropertyRenderingTests(unittest.TestCase):
    def test_number_uses_schema_format(self):
        value = {"type": "number", "number": 1234.5}
        schema = {"type": "number", "number": {"format": "dollar"}}
        self.assertEqual(property_value_to_html(value, schema), "$1,234.5")
        self.assertEqual(format_number(0.25, "percent"), "25%")

    def test_select_and_rich_text_are_structured_html(self):
        select = {"type": "select", "select": {"name": "进行中", "color": "blue"}}
        self.assertIn("nb-option-blue", property_value_to_html(select))
        rich = {"type": "rich_text", "rich_text": [{
            "plain_text": "重点", "annotations": {"bold": True}
        }]}
        self.assertEqual(property_value_to_html(rich), "<strong>重点</strong>")


class ViewRenderingTests(unittest.TestCase):
    def test_gallery_image_widths_match_print_quality_by_card_size(self):
        source = "https://files.example/image.png?X-Amz-Signature=a&part=1"
        expected_widths = {"small": 640, "medium": 1024, "large": 1600}

        for size, width in expected_widths.items():
            with self.subTest(size=size):
                resized = _notion_resized_image_url(source, "block/id", size)
                self.assertIn(f"width={width}", resized)
                self.assertIn("id=block%2Fid", resized)
                self.assertIn("https%3A%2F%2Ffiles.example%2Fimage.png", resized)
                self.assertNotIn("X-Amz-Signature=a&part=1", resized)

        self.assertEqual(_notion_resized_image_url(source, "", "small"), source)
        self.assertEqual(
            _notion_resized_image_url("data:image/png;base64,abc", "block-id", "small"),
            "data:image/png;base64,abc",
        )

    def test_gallery_image_falls_back_to_full_source(self):
        source = "https://files.example/image.png?token=a&part=1"
        body = render_database_gallery(
            {"properties": {"Name": {"type": "title"}}},
            [{
                "id": "page-id",
                "cover": {"type": "file", "file": {"url": source}},
                "properties": {"Name": {"type": "title", "title": [{"plain_text": "Card"}]}},
            }],
            {"gallery_fit": False, "gallery_size": "small", "view": None},
        )

        self.assertIn("width=640", body)
        self.assertIn('data-fallback-src="https://files.example/image.png?token=a&amp;part=1"', body)
        self.assertIn("this.onerror=null;this.src=this.dataset.fallbackSrc", body)

    def test_gallery_fallback_block_uses_image_block_id(self):
        body = render_database_gallery(
            {"properties": {"Name": {"type": "title"}}},
            [{
                "id": "page-id",
                "properties": {"Name": {"type": "title", "title": [{"plain_text": "Card"}]}},
            }],
            {
                "gallery_fit": False,
                "gallery_size": "large",
                "view": None,
                "entry_blocks_cache": {"page-id": [{
                    "id": "image-block-id",
                    "type": "image",
                    "image": {"type": "external", "external": {"url": "https://example.com/full.jpg"}},
                }]},
            },
        )

        self.assertIn("id=image-block-id", body)
        self.assertIn("width=1600", body)

    def test_groups_follow_select_option_order(self):
        schema = {"优先级": {"type": "select", "select": {"options": [
            {"name": "高"}, {"name": "中"}, {"name": "低"}
        ]}}}
        entries = [
            {"properties": {"优先级": {"type": "select", "select": {"name": "低"}}}},
            {"properties": {"优先级": {"type": "select", "select": {"name": "高"}}}},
        ]
        groups = _group_entries(entries, "优先级", schema)
        self.assertEqual(list(groups), ["高", "低"])

    def test_sort_field_follows_schema_option_order(self):
        schema = {"优先级": {"type": "select", "select": {
            "options": [{"name": "高"}, {"name": "中"}, {"name": "低"}]
        }}}
        entries = [
            {"id": "low", "properties": {"优先级": {"type": "select", "select": {"name": "低"}}}},
            {"id": "high", "properties": {"优先级": {"type": "select", "select": {"name": "高"}}}},
            {"id": "mid", "properties": {"优先级": {"type": "select", "select": {"name": "中"}}}},
        ]
        self.assertEqual(
            [entry["id"] for entry in _sort_entries_by_property(entries, "优先级", schema)],
            ["high", "mid", "low"],
        )

    def test_page_id_sort_normalizes_uuid_and_keeps_missing_ids_last(self):
        entries = [
            {"id": "BBBBBBBB-0000-0000-0000-000000000000"},
            {},
            {"id": "aaaaaaaa000000000000000000000000"},
            {"id": "00000000-0000-0000-0000-000000000001"},
        ]

        ordered = _sort_entries_by_page_id(entries)

        self.assertEqual(
            [entry.get("id") for entry in ordered],
            [
                "00000000-0000-0000-0000-000000000001",
                "aaaaaaaa000000000000000000000000",
                "BBBBBBBB-0000-0000-0000-000000000000",
                None,
            ],
        )

    def test_view_sort_property_id_resolves_against_schema(self):
        schema = {"优先级": {"id": "%3Aabc", "type": "select", "select": {
            "options": [{"name": "高"}, {"name": "低"}]
        }}}
        entries = [
            {"id": "low", "properties": {"优先级": {"type": "select", "select": {"name": "低"}}}},
            {"id": "high", "properties": {"优先级": {"type": "select", "select": {"name": "高"}}}},
        ]
        ordered = _sort_entries(entries, [{"property_id": ":abc", "direction": "ascending"}], schema)
        self.assertEqual([entry["id"] for entry in ordered], ["high", "low"])

    def test_view_property_visibility_and_order(self):
        schema = {
            "名称": {"id": "title", "type": "title"},
            "金额": {"id": "amt", "type": "number"},
            "备注": {"id": "note", "type": "rich_text"},
        }
        view = {"configuration": {"properties": [
            {"property_id": "title", "visible": True},
            {"property_id": "note", "visible": False},
            {"property_id": "amt", "visible": True},
        ]}}
        self.assertEqual(_view_props(schema, view), ["名称", "金额"])

    def test_view_property_ids_are_compared_decoded(self):
        db = {"properties": {"分类": {"id": "%3Aabc", "type": "select"}}}
        view = {"configuration": {"group_by": {"property_id": ":abc"}}}
        self.assertEqual(PageRenderer._view_group_property(db, view), "分类")

    def test_gallery_presentation_follows_view(self):
        view = {"configuration": {"cover_size": "large", "cover_aspect": "contain"}}
        self.assertEqual(PageRenderer._view_gallery_size(view), "large")
        self.assertTrue(PageRenderer._view_gallery_fit(view))

    def test_gallery_top_aligned_mode_uses_cover_and_top_position(self):
        body = render_database_gallery(
            {"properties": {"名称": {"type": "title"}}},
            [{"properties": {"名称": {"type": "title", "title": [{"plain_text": "测试"}]}}}],
            {"gallery_fit": "top", "gallery_size": "small", "view": None},
        )
        self.assertIn("nb-db-gallery-fit-top", body)
        self.assertIn("nb-db-gallery-cover-fit-top", body)

        css = PageRenderer(None)._build_css("portrait", False, 180, 267, preview_mode=False)
        self.assertIn(".nb-db-gallery-cover-fit-top img", css)
        self.assertIn("object-fit: cover", css)
        self.assertIn("object-position: top center", css)

    def test_gallery_adaptive_mode_emits_subject_alignment_hook(self):
        body = render_database_gallery(
            {"properties": {"名称": {"type": "title"}}},
            [{"properties": {"名称": {"type": "title", "title": [{"plain_text": "测试"}]}}}],
            {"gallery_fit": "auto", "gallery_size": "medium", "view": None},
        )
        self.assertIn("nb-db-gallery-fit-auto", body)
        self.assertIn("nb-db-gallery-cover-fit-auto", body)

        renderer = PageRenderer(None)
        html = renderer._build_html("测试", "", "", body, "portrait", False)
        self.assertIn("getImageData", html)
        self.assertIn("objectPosition", html)
        self.assertIn("subjectSize <= viewport", html)
        self.assertIn("leftMass >= rightMass", html)
        self.assertIn("maskCtx.fillRect", html)
        self.assertIn("size = 64", html)
        self.assertIn("document.createElement('canvas')", html)
        self.assertIn("img._foregroundMask ? pixels[i]", html)

    def test_database_follows_group_and_gallery_view_end_to_end(self):
        schema = {
            "名称": {"id": "title", "type": "title", "title": {}},
            "分类": {"id": "%3Aabc", "type": "select", "select": {
                "options": [{"name": "产品"}, {"name": "研究"}]
            }},
        }
        view = {"id": "view-1", "type": "gallery", "configuration": {
            "group_by": {"type": "select", "property_id": ":abc", "sort": {"type": "manual"}},
            "cover_size": "small", "cover_aspect": "contain",
        }}
        entries = [{
            "id": f"page-{index}",
            "properties": {
                "名称": {"type": "title", "title": [{"plain_text": f"条目 {index}"}]},
                "分类": {"type": "select", "select": {
                    "name": "产品" if index % 2 == 0 else "研究", "color": "blue"
                }},
            },
        } for index in range(20)]
        renderer = PageRenderer(None, database_options={
            "db-1": {"view_id": "view-1", "group_by": "分类", "gallery_size": "small"}
        })
        renderer._entry_blocks_cache = {entry["id"]: [] for entry in entries}
        body = renderer._render_blocks([{
            "id": "db-1", "type": "child_database", "_entries": entries,
            "_db": {"properties": schema, "_views": [view]},
        }])
        self.assertIn("nb-db-group-title\">产品", body)
        self.assertIn("nb-db-group-title\">研究", body)
        self.assertIn("nb-db-gallery-small", body)
        self.assertIn("nb-db-gallery-fit", body)

    def test_database_title_can_be_hidden(self):
        from notion_pdf.blocks import render_database

        db = {"properties": {"名称": {"type": "title"}}}
        entries = [{"properties": {"名称": {"type": "title", "title": [{"plain_text": "条目"}]}}}]
        shown = render_database(db, entries, "table", {"database_title": "资料库", "show_title": True})
        hidden = render_database(db, entries, "table", {"database_title": "资料库", "show_title": False})
        self.assertIn('nb-db-title">资料库', shown)
        self.assertNotIn('nb-db-title">资料库', hidden)

    def test_gallery_requests_all_entry_blocks_for_fallback_covers(self):
        class FakeClient:
            def __init__(self):
                self.ids = []

            def get_block_children(self, entry_id):
                self.ids.append(entry_id)
                return []

        client = FakeClient()
        entries = [{"id": f"entry-{index}", "properties": {}} for index in range(12)]
        renderer = PageRenderer(client)
        renderer._render_blocks([{
            "id": "db-1", "type": "child_database", "_entries": entries,
            "_db": {"properties": {}, "_views": [{"id": "view-1", "type": "gallery", "configuration": {}}]},
        }])
        self.assertEqual(set(client.ids), {entry["id"] for entry in entries})

    def test_small_gallery_groups_pack_without_splitting_groups(self):
        options = [{"name": name} for name in ("A", "B", "C", "D")]
        schema = {
            "名称": {"id": "title", "type": "title"},
            "分类": {"id": "kind", "type": "select", "select": {"options": options}},
        }
        categories = ["A", "A", "B", "C", "D", "D"]
        entries = [{"properties": {
            "名称": {"type": "title", "title": [{"plain_text": str(index)}]},
            "分类": {"type": "select", "select": {"name": category}},
        }} for index, category in enumerate(categories)]
        from notion_pdf.blocks import render_database
        body = render_database({"properties": schema}, entries, "gallery", {
            "group_by": "分类", "gallery_size": "small", "gallery_fit": False,
            "view": {"configuration": {"group_by": {"sort": {"type": "manual"}}}},
            "entry_blocks_cache": {},
        })
        self.assertIn("nb-db-group-grid-small", body)
        self.assertEqual(body.count('--group-span:2'), 2)
        self.assertEqual(body.count('--group-span:1'), 2)

    def test_preview_gallery_paginates_rows_with_ten_pixel_tolerance(self):
        script = PageRenderer(None)._preview_pagination_script("portrait")
        css = PageRenderer(None)._build_css("portrait", False, 180, 267, preview_mode=True)
        self.assertIn("getBoundingClientRect().top", script)
        self.assertIn("gallery.style.width = '180.0mm'", script)
        self.assertIn("currentPage.clientHeight + 10", script)
        self.assertIn("function rowOverflows()", script)
        self.assertIn("function rowExceedsPage(row)", script)
        self.assertIn("function moveToNewPageWithHeadings(element)", script)
        self.assertIn("previous.classList.contains('nb-db-title')", script)
        self.assertIn("document.body.dataset.paginationReady = 'true'", script)
        self.assertIn("@media print", css)
        self.assertIn("margin: 0", css)
        self.assertIn("page-break-after: always", css)
        table_script = PageRenderer(None)._table_layout_script()
        self.assertIn("cell.cloneNode(true)", table_script)
        self.assertIn("table.style.tableLayout = 'fixed'", table_script)
        self.assertIn("widths[donor] <= 5", table_script)
        self.assertIn("pass < 20", table_script)
        self.assertIn("Math.min(3, widths[donor] - 5)", table_script)

    def test_pdf_export_uses_completed_preview_pagination(self):
        calls = {}

        class FakePage:
            def goto(self, url):
                calls["url"] = url

            def wait_for_load_state(self, state):
                calls["load_state"] = state

            def wait_for_function(self, expression):
                calls["wait_expression"] = expression

            def get_attribute(self, selector, name):
                calls["attribute"] = (selector, name)
                return None

            def pdf(self, **kwargs):
                calls["pdf"] = kwargs

        class FakeBrowser:
            def new_page(self):
                return FakePage()

            def close(self):
                calls["closed"] = True

        class FakePlaywright:
            chromium = type("Chromium", (), {"launch": lambda self: FakeBrowser()})()

        class FakePlaywrightContext:
            def __enter__(self):
                return FakePlaywright()

            def __exit__(self, exc_type, exc_value, traceback):
                return False

        renderer = PageRenderer(None)
        rendered = {"html": "<html></html>", "orientation": "portrait", "title": "test"}
        with tempfile.TemporaryDirectory() as directory:
            output_path = f"{directory}/test.pdf"
            with patch.object(renderer, "render_html", return_value=rendered) as render_html:
                with patch("playwright.sync_api.sync_playwright", return_value=FakePlaywrightContext()):
                    renderer.export_pdf_from_data({}, output_path=output_path)

        self.assertTrue(render_html.call_args.kwargs["preview_mode"])
        self.assertEqual(calls["load_state"], "networkidle")
        self.assertIn("paginationReady", calls["wait_expression"])
        self.assertEqual(calls["attribute"], ("body", "data-pagination-error"))
        self.assertTrue(calls["closed"])


if __name__ == "__main__":
    unittest.main()
