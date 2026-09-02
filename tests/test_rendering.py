import unittest

from notion_pdf.blocks import _group_entries, _view_props
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


if __name__ == "__main__":
    unittest.main()
