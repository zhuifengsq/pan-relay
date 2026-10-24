import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from src.db import api_configs
from src.db.connection import SQLiteConnectionWrapper
from src.db.connection import _migrate_schema_columns
from src.services.api_config_service import _request_api
from src.services.search_service import fetch_data
from src.utils.api_headers import parse_api_headers, serialize_api_headers


class ApiRequestHeadersTest(unittest.TestCase):
    def test_headers_survive_create_copy_and_edit(self):
        with tempfile.TemporaryDirectory() as folder:
            path = f"{folder}/api.db"
            conn = sqlite3.connect(path)
            try:
                conn.execute("""CREATE TABLE api_config (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE, url TEXT,
                    method TEXT, request TEXT, headers TEXT, response TEXT, status TEXT,
                    response_time_ms INTEGER, is_enabled INTEGER, checked_at TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )""")
                conn.commit()
            finally:
                conn.close()

            def connect():
                return SQLiteConnectionWrapper(sqlite3.connect(path))

            with patch.object(api_configs, "get_db_connection", side_effect=connect):
                config = {
                    "name": "PanSou", "url": "https://example.test/api/search?kw=[[keyword]]",
                    "method": "get", "request": "", "headers": '{"Authorization":"Bearer example-token"}',
                    "response": "data.merged_by_type.*[] | [].[note, url]", "is_enabled": True,
                }
                self.assertTrue(api_configs.insert_config(config)[0])
                original = api_configs.get_all_configs()[0]
                self.assertEqual(config["headers"], original["headers"])
                success, _, copied_id = api_configs.copy_config(original["id"])
                self.assertTrue(success)
                self.assertEqual(config["headers"], api_configs.get_config_by_id(copied_id)["headers"])
                older_client_config = dict(config)
                older_client_config.pop("headers")
                self.assertTrue(api_configs.update_config(original["id"], older_client_config)[0])
                self.assertEqual(config["headers"], api_configs.get_config_by_id(original["id"])["headers"])
                config["headers"] = "{}"
                self.assertTrue(api_configs.update_config(original["id"], config)[0])
                self.assertEqual("{}", api_configs.get_config_by_id(original["id"])["headers"])

    def test_existing_api_table_gains_headers_column(self):
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE TABLE api_config (id INTEGER PRIMARY KEY, name TEXT)")
            conn.execute("INSERT INTO api_config (name) VALUES ('existing')")
            _migrate_schema_columns(conn)
            row = conn.execute("SELECT headers FROM api_config WHERE name = 'existing'").fetchone()
            self.assertEqual(("{}",), row)
        finally:
            conn.close()

    def test_authorization_header_is_sent_by_test_and_live_search(self):
        headers_json = '{"Authorization":"Bearer example-token"}'
        response = Mock(status_code=200)
        response.json.return_value = {"data": {"merged_by_type": {}}}
        with patch("src.services.api_config_service.requests.get", return_value=response) as test_get:
            _request_api({
                "url": "https://example.test/api/search?kw=film", "method": "get",
                "request": "", "headers": headers_json,
            })
            self.assertEqual("Bearer example-token", test_get.call_args.kwargs["headers"]["Authorization"])

        with patch("src.services.search_service.requests.get", return_value=response) as live_get:
            fetch_data("https://example.test/api/search?kw=film", "get", "", request_headers=headers_json)
            self.assertEqual("Bearer example-token", live_get.call_args.kwargs["headers"]["Authorization"])

        with patch("src.services.api_config_service.requests.post", return_value=response) as test_post:
            _request_api({
                "url": "https://example.test/api/search", "method": "post",
                "request": '{"kw":"film"}', "headers": headers_json,
            })
            self.assertEqual("Bearer example-token", test_post.call_args.kwargs["headers"]["Authorization"])

        with patch("src.services.search_service.requests.post", return_value=response) as live_post:
            fetch_data("https://example.test/api/search", "post", '{"kw":"film"}', request_headers=headers_json)
            self.assertEqual("Bearer example-token", live_post.call_args.kwargs["headers"]["Authorization"])

    def test_invalid_header_json_is_rejected(self):
        for value in ('["not an object"]', '{"Authorization":123}', '{"X-Test":"a\\r\\nb"}'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_api_headers(value)
        self.assertEqual("{}", serialize_api_headers(""))


if __name__ == "__main__":
    unittest.main()
