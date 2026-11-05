# -*- coding: utf-8 -*-
"""安全加固回归：可信代理 IP、登录限速、JWT 吊销/版本、request_id。"""
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("SQLITE_DB_PATH", "data/pan_relay.lead.db")

from app import app  # noqa: E402
from src.services import admin_token_service  # noqa: E402
from src.utils.auth_utils import create_jwt_token, decode_jwt_token, is_token_valid  # noqa: E402
from src.utils.login_throttle import reset_login_throttle  # noqa: E402
from src.utils.request_utils import get_client_ip  # noqa: E402


class SecurityHardeningTestCase(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        reset_login_throttle()

    def tearDown(self):
        reset_login_throttle()

    def test_01_request_id_header_is_echoed_and_generated(self):
        resp = self.client.get("/healthz")
        generated = resp.headers.get("X-Request-ID", "")
        self.assertTrue(generated)

        resp2 = self.client.get("/healthz", headers={"X-Request-ID": "trace-abc-123"})
        self.assertEqual(resp2.headers.get("X-Request-ID"), "trace-abc-123")

    def test_02_forwarded_headers_ignored_without_trusted_proxy(self):
        with patch.dict(os.environ, {"TRUSTED_PROXIES": ""}, clear=False):
            with app.test_request_context(
                "/", environ_base={"REMOTE_ADDR": "10.9.8.7"},
                headers={"X-Forwarded-For": "1.2.3.4"},
            ):
                self.assertEqual(get_client_ip(), "10.9.8.7")

    def test_03_forwarded_headers_trusted_only_from_proxy(self):
        with patch.dict(os.environ, {"TRUSTED_PROXIES": "10.9.8.7"}, clear=False):
            with app.test_request_context(
                "/", environ_base={"REMOTE_ADDR": "10.9.8.7"},
                headers={"X-Forwarded-For": "1.2.3.4"},
            ):
                self.assertEqual(get_client_ip(), "1.2.3.4")

    def test_04_login_lockout_after_repeated_failures(self):
        with patch("src.routes.auth_routes.verify_admin_credentials", return_value=(False, "bad")):
            for _ in range(5):
                resp = self.client.post("/login", data={"username": "admin", "password": "wrong"})
                self.assertEqual(resp.status_code, 200)
            locked = self.client.post("/login", data={"username": "admin", "password": "wrong"})
        self.assertEqual(locked.status_code, 429)
        self.assertTrue(locked.headers.get("Retry-After"))

    def test_05_logout_revokes_token(self):
        token = create_jwt_token("admin")
        self.assertTrue(is_token_valid(token))

        self.client.set_cookie("token", token)
        self.client.get("/logout")

        self.assertFalse(is_token_valid(token))
        status = self.client.get("/admin/api/password_status")
        self.assertEqual(status.status_code, 401)

    def test_06_change_password_invalidates_old_tokens(self):
        old_token = create_jwt_token("admin")
        self.client.set_cookie("token", old_token)

        with patch("src.routes.auth_routes.change_admin_password", return_value=(True, "ok")),                 patch("src.routes.auth_routes.get_admin_username", return_value="admin"):
            resp = self.client.post(
                "/admin/api/change_password",
                json={"old_password": "admin123", "new_password": "newpass456"},
            )

        self.assertEqual(resp.status_code, 200)
        self.assertFalse(is_token_valid(old_token))

        new_cookie = resp.headers.get("Set-Cookie", "")
        self.assertIn("token=", new_cookie)


if __name__ == "__main__":
    unittest.main()
