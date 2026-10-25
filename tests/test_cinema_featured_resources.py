import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from src.db import connection
from src.db import resources
from src.routes.resource_routes import resources_bp
from src.utils.auth_utils import create_jwt_token


class CinemaFeaturedResourcesTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = str(Path(self.temp_dir.name) / "test.db")
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executescript("""
                CREATE TABLE resources (
                    id INTEGER PRIMARY KEY, name TEXT, share_link TEXT,
                    cloud_name TEXT, type TEXT, health_status TEXT,
                    created_at TEXT
                );
                INSERT INTO resources VALUES
                    (1, '资源一', 'https://example.com/1', '百度网盘', '', 'ok', ''),
                    (2, '资源二', 'https://example.com/2', '夸克网盘', '', 'ok', ''),
                    (3, '资源三', 'https://example.com/3', '百度网盘', '', 'ok', '');
            """)
            connection._migrate_schema_columns(conn)
            conn.commit()
        finally:
            conn.close()
        def open_test_database():
            return connection.SQLiteConnectionWrapper(sqlite3.connect(self.db_path))
        self.connection_patch = patch.object(connection, "get_db_connection", side_effect=open_test_database)
        self.resources_patch = patch.object(resources, "get_db_connection", side_effect=open_test_database)
        self.connection_patch.start()
        self.resources_patch.start()
        self.addCleanup(self.connection_patch.stop)
        self.addCleanup(self.resources_patch.stop)

    def test_selected_order_and_deletion(self):
        self.assertEqual(resources.set_cinema_featured_resources([2, 1]), (True, ""))
        self.assertEqual([item["id"] for item in resources.get_cinema_featured_resources()], [2, 1])
        self.assertEqual(resources.get_cinema_featured_resource(2)["name"], "资源二")
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("DELETE FROM resources WHERE id = 2")
            conn.commit()
        finally:
            conn.close()
        self.assertEqual([item["id"] for item in resources.get_cinema_featured_resources()], [1])
        self.assertIsNone(resources.get_cinema_featured_resource(2))

    def test_rejects_duplicates_missing_and_more_than_ten_without_changing_order(self):
        resources.set_cinema_featured_resources([1, 2])
        for ids in ([1, 1], [1, 99], list(range(1, 12))):
            success, _ = resources.set_cinema_featured_resources(ids)
            self.assertFalse(success)
            self.assertEqual([item["id"] for item in resources.get_cinema_featured_resources()], [1, 2])

    def test_admin_api_requires_login_and_persists_selection(self):
        app = Flask(__name__)
        app.register_blueprint(resources_bp)
        client = app.test_client()
        self.assertEqual(client.put('/admin/api/resources/cinema-featured', json={'resource_ids': [1]}).status_code, 401)
        client.set_cookie('token', create_jwt_token())
        self.assertEqual(client.put('/admin/api/resources/cinema-featured', json={'resource_ids': [2, 1]}).status_code, 200)
        self.assertEqual([item['id'] for item in client.get('/admin/api/resources/cinema-featured').json['data']], [2, 1])
        self.assertEqual(client.put('/admin/api/resources/cinema-featured', json={'resource_ids': [3, 3]}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
