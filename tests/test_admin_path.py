import unittest
from unittest.mock import patch

from flask import Flask, redirect

from src.services.admin_path_service import validate_admin_path
from src.utils.admin_path_middleware import AdminPathMiddleware


class AdminPathTests(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)

        @app.get("/admin")
        def login():
            return '<a href="/admin/dashboard">Dashboard</a><script>fetch("/admin/api/admin-path")</script>'

        @app.get("/admin/dashboard")
        def dashboard():
            return redirect("/admin")

        @app.get("/static/admin.js")
        def script():
            return 'fetch("/admin/api/admin-path")', 200, {"Content-Type": "application/javascript"}

        app.wsgi_app = AdminPathMiddleware(app.wsgi_app)
        self.client = app.test_client()

    def test_custom_path_rewrites_routes_links_and_redirects(self):
        with patch("src.utils.admin_path_middleware.get_admin_path", return_value="/my-console"):
            self.assertEqual(self.client.get("/admin").status_code, 404)
            page = self.client.get("/my-console")
            self.assertEqual(page.status_code, 200)
            self.assertIn('/my-console/dashboard', page.get_data(as_text=True))
            self.assertIn('/my-console/api/admin-path', page.get_data(as_text=True))
            self.assertEqual(self.client.get("/my-console/dashboard").headers["Location"], "/my-console")
            self.assertIn('/my-console/api/admin-path', self.client.get("/static/admin.js").get_data(as_text=True))

    def test_path_validation(self):
        self.assertEqual(validate_admin_path("/my-console/"), "/my-console")
        for invalid in ("/api", "/static", "/a", "/two/parts", "http://example.com"):
            with self.assertRaises(ValueError):
                validate_admin_path(invalid)


if __name__ == "__main__":
    unittest.main()
