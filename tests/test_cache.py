import tempfile
import unittest
from pathlib import Path

from notion_pdf.cache import PageDataCache


class PageDataCacheTests(unittest.TestCase):
    def test_round_trip_and_token_partition(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = PageDataCache(Path(directory))
            url = "https://www.notion.so/3cdbf621a7e880a88862fdf8f469807e"
            data = {
                "page": {"id": "page"},
                "blocks": [{"id": "block"}],
                "entry_blocks_cache": {"entry": [{"id": "image"}]},
            }
            path = cache.save(url, "token-a", data)
            loaded, saved_at = cache.load(url, "token-a")
            self.assertEqual(loaded, data)
            self.assertGreater(saved_at, 0)
            self.assertTrue(path.exists())
            self.assertIsNone(cache.load(url, "token-b"))

    def test_corrupt_cache_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = PageDataCache(Path(directory))
            url = "https://www.notion.so/3cdbf621a7e880a88862fdf8f469807e"
            path = cache._path(url, "token")
            path.write_bytes(b"not gzip")
            self.assertIsNone(cache.load(url, "token"))


if __name__ == "__main__":
    unittest.main()
