import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from notion_pdf.cache import PageDataCache, _contains_expired_signed_url, _is_expired_signed_url


class CacheImageExpiryTests(unittest.TestCase):
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

    def test_signed_image_url_expiry_is_detected(self):
        expired = "https://files.example/image.png?X-Amz-Date=20260902T130000Z&X-Amz-Expires=3600"
        fresh = "https://files.example/image.png?X-Amz-Date=20260902T130000Z&X-Amz-Expires=3600"
        now = datetime(2026, 9, 2, 14, 1, tzinfo=timezone.utc)
        self.assertTrue(_is_expired_signed_url(expired, now))
        self.assertFalse(_is_expired_signed_url(fresh, datetime(2026, 9, 2, 13, 30, tzinfo=timezone.utc)))
        self.assertTrue(_contains_expired_signed_url({"image": {"url": expired}}))

    def test_cache_load_discards_data_with_expired_image_url(self):
        expired = "https://files.example/image.png?X-Amz-Date=20200101T000000Z&X-Amz-Expires=1"
        with tempfile.TemporaryDirectory() as directory:
            cache = PageDataCache(Path(directory))
            cache.save("https://www.notion.so/example/3c1bf621a7e880efbb0dd7ab2e355b0d", "token", {"image": expired})
            self.assertIsNone(cache.load("https://www.notion.so/example/3c1bf621a7e880efbb0dd7ab2e355b0d", "token"))


if __name__ == "__main__":
    unittest.main()
