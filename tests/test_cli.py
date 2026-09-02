import tempfile
import unittest
from pathlib import Path

from notion_pdf.cli import build_parser, database_options_from_args
from notion_pdf.renderer import PageRenderer


class CliTests(unittest.TestCase):
    def test_html_alias_and_database_overrides(self):
        args = build_parser().parse_args([
            "page-id", "--token", "token", "--html", "--offline",
            "--database-view", "db-1=view-1",
            "--database-group", "db-1=分类",
            "--database-gallery-size", "db-1=small",
            "--database-media-fit", "db-1=contain",
        ])
        self.assertTrue(args.html_only)
        self.assertTrue(args.offline)
        self.assertEqual(database_options_from_args(args), {
            "db-1": {
                "view_id": "view-1", "group_by": "分类",
                "gallery_size": "small", "gallery_fit": True,
            }
        })

    def test_html_export_uses_existing_page_data(self):
        data = {
            "page": {"properties": {"名称": {
                "type": "title", "title": [{"plain_text": "离线页面"}]
            }}},
            "blocks": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "page.html"
            result = PageRenderer(None).export_html_from_data(data, str(output))
            self.assertEqual(result, str(output))
            self.assertIn("离线页面", output.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
