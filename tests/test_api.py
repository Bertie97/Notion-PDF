import json
import unittest

import requests

from notion_pdf.api import NotionClient
from notion_pdf.renderer import PageRenderer
from notion_pdf.utils import extract_view_id


DATABASE_URL = (
    "https://app.notion.com/p/694bf621a7e8825084cc018dd9360bfc"
    "?v=a62bf621a7e882bc83f5083aefe9ecb2&source=copy_link"
)


class FakeDatabaseClient(NotionClient):
    def __init__(self):
        self.bundle_calls = 0

    def get_page(self, page_url_or_id):
        response = requests.Response()
        response.status_code = 400
        response.url = "https://api.notion.com/v1/pages/database-id"
        response._content = json.dumps({
            "object": "error",
            "code": "validation_error",
            "message": "Provided ID is a database, not a page. Use the retrieve database API instead.",
        }).encode()
        raise requests.HTTPError(response=response)

    def get_database_bundle(self, database_id):
        self.bundle_calls += 1
        database_meta = {
            "title": [{"plain_text": "任务数据库"}],
            "icon": {"type": "emoji", "emoji": "DB"},
            "parent": {"type": "workspace", "workspace": True},
        }
        data_source = {
            "id": "source-id",
            "name": "任务数据库",
            "properties": {"名称": {"type": "title"}},
            "_database": database_meta,
        }
        entries = [{
            "id": "entry-id",
            "properties": {"名称": {"type": "title", "title": [{"plain_text": "任务一"}]}},
        }]
        views = [{
            "id": "a62bf621-a7e8-82bc-83f5-083aefe9ecb2",
            "name": "看板视图",
            "type": "table",
            "configuration": {},
        }]
        return data_source, entries, views

    def get_block_children(self, block_id):
        return []


class NotionApiTests(unittest.TestCase):
    def test_extracts_database_view_id(self):
        self.assertEqual(
            extract_view_id(DATABASE_URL),
            "a62bf621-a7e8-82bc-83f5-083aefe9ecb2",
        )

    def test_database_url_becomes_renderable_root_document(self):
        client = FakeDatabaseClient()
        data = PageRenderer(client).fetch_page_data(DATABASE_URL)

        self.assertEqual(client.bundle_calls, 1)
        self.assertEqual(data["page"]["properties"]["title"]["title"][0]["plain_text"], "任务数据库")
        root = data["blocks"][0]
        self.assertTrue(root["_root_database"])
        self.assertEqual(root["_entries"][0]["id"], "entry-id")
        self.assertEqual(
            root["_db"]["_requested_view_id"],
            "a62bf621-a7e8-82bc-83f5-083aefe9ecb2",
        )

        html = PageRenderer(None).render_html(data)["html"]
        self.assertIn("任务数据库", html)
        self.assertIn("任务一", html)
        self.assertEqual(html.count('class="nb-db-title"'), 0)

    def test_page_url_keeps_existing_page_and_block_flow(self):
        class FakePageClient(FakeDatabaseClient):
            def get_page(self, page_url_or_id):
                return {"id": "page-id", "properties": {}}

            def get_block_children(self, block_id):
                return [{"id": "paragraph-id", "type": "paragraph"}]

        client = FakePageClient()
        data = client.get_page_with_blocks("0123456789abcdef0123456789abcdef")

        self.assertEqual(data["page"]["id"], "page-id")
        self.assertEqual(data["blocks"][0]["id"], "paragraph-id")
        self.assertEqual(client.bundle_calls, 0)

    def test_non_database_page_error_is_not_reclassified(self):
        client = FakeDatabaseClient()
        response = requests.Response()
        response.status_code = 404
        response._content = b'{"code":"object_not_found"}'
        error = requests.HTTPError(response=response)

        self.assertFalse(client._is_database_not_page_error(error))


if __name__ == "__main__":
    unittest.main()
