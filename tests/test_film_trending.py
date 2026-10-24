import unittest
from unittest.mock import patch

from src.services import film_trending_service as service


class FilmTrendingTests(unittest.TestCase):
    def setUp(self):
        service._cache.clear()

    @patch("src.services.film_trending_service.requests.get")
    def test_chart_is_normalized_and_cached(self, get):
        get.return_value.json.return_value = {
            "data": {"hits": {"hit": {"item": [
                {"title": "影片 A", "src": "https://example.com/a.jpg"},
                {"title": "影片 B", "src": "http://example.com/b.jpg"},
                {"title": "", "src": "https://example.com/empty.jpg"},
            ]}}}
        }
        items, stale = service.get_film_trending("电影")
        self.assertFalse(stale)
        self.assertEqual(items, [
            {"title": "影片 A", "poster": "https://example.com/a.jpg"},
            {"title": "影片 B", "poster": "https://example.com/b.jpg"},
        ])
        self.assertEqual(service.get_film_trending("电影"), (items, False))
        get.assert_called_once()

    @patch("src.services.film_trending_service.requests.get")
    def test_failed_refresh_keeps_last_chart(self, get):
        service._cache["电视剧"] = (0, [{"title": "旧榜单", "poster": ""}])
        get.side_effect = service.requests.RequestException("offline")
        self.assertEqual(service.get_film_trending("电视剧"),
                         ([{"title": "旧榜单", "poster": ""}], True))

    def test_unknown_category_is_rejected(self):
        with self.assertRaises(ValueError):
            service.get_film_trending("未知")


if __name__ == "__main__":
    unittest.main()
