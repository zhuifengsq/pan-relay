import json
import os
import unittest
import uuid
from unittest.mock import patch

from app import app
from src.db import system_configs
from src.db.connection import get_db_connection
from src.db.logs import query_system_logs
from src.db.resources import search_resources_by_title_terms
from src.pan_operator import _execute_store
from src.db.system_configs import clear_config_cache, delete_config_value, get_config_value, set_config_value
from src.services.log_service import flush_log_queue, record_log
from src.services.search_service import _get_search_executor


class BackendOptimizationTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = app
        cls.client = cls.app.test_client()

    def test_01_config_cache_hits_and_invalidates_on_write(self):
        key = f"opt_test_{uuid.uuid4().hex[:8]}"
        set_config_value(key, {"v": 1})
        clear_config_cache()

        with patch("src.db.system_configs.get_db_connection", side_effect=system_configs.get_db_connection) as mocked:
            for _ in range(5):
                self.assertEqual(json.loads(get_config_value(key)), {"v": 1})
            self.assertEqual(mocked.call_count, 1, "配置读取应命中进程内缓存，只查库一次")

        # 写入后缓存必须失效，立即能读到新值
        set_config_value(key, {"v": 2})
        self.assertEqual(json.loads(get_config_value(key)), {"v": 2})
        delete_config_value(key)
        self.assertIsNone(get_config_value(key))

    def test_02_healthz_endpoint(self):
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["db"], "ok")

    def test_03_shared_search_executor_is_reused(self):
        self.assertIs(_get_search_executor(), _get_search_executor())

    def test_04_record_log_is_async_and_flushable(self):
        keyword = f"async-log-{uuid.uuid4().hex[:8]}"
        self.assertIsNone(record_log(log_type="system", action="opt.test.async", query_text=keyword))
        self.assertTrue(flush_log_queue(5.0))

        success, _, data = query_system_logs(q=keyword)
        self.assertTrue(success)
        self.assertGreaterEqual(data["total"], 1)

    def test_05_login_cookie_has_samesite_and_httponly(self):
        with patch("src.routes.auth_routes.verify_admin_credentials", return_value=(True, "ok")):
            response = self.client.post("/login", data={"username": "admin", "password": "admin123"})
        cookie_headers = response.headers.getlist("Set-Cookie")
        token_cookie = next((c for c in cookie_headers if c.startswith("token=")), "")
        self.assertIn("HttpOnly", token_cookie)
        self.assertIn("SameSite=Lax", token_cookie)

    def test_06_log_batch_writes_all_records(self):
        keyword = f"batch-log-{uuid.uuid4().hex[:8]}"
        for index in range(5):
            self.assertIsNone(
                record_log(log_type="system", action="opt.test.batch", query_text=f"{keyword}-{index}")
            )
        self.assertTrue(flush_log_queue(5.0))

        success, _, data = query_system_logs(q=keyword)
        self.assertTrue(success)
        self.assertEqual(data["total"], 5, "批量落库不能丢日志")

    def test_07_resource_search_is_bounded(self):
        marker = f"zzlimit{uuid.uuid4().hex[:6]}"
        conn = get_db_connection()
        created_ids = []
        try:
            cursor = conn.cursor()
            for index in range(3):
                cursor.execute(
                    "INSERT INTO resources (name, share_link, cloud_name) VALUES (?, ?, ?)",
                    (f"{marker}-资源{index}", f"https://example.com/{marker}/{index}", "百度网盘"),
                )
                created_ids.append(cursor.lastrowid)
            conn.commit()
        finally:
            conn.close()

        try:
            with patch.dict(os.environ, {"RESOURCE_SEARCH_LIMIT": "2"}):
                results = search_resources_by_title_terms([marker])
            self.assertEqual(len(results), 2)
        finally:
            conn = get_db_connection()
            try:
                conn.cursor().executemany(
                    "DELETE FROM resources WHERE id = ?", [(resource_id,) for resource_id in created_ids]
                )
                conn.commit()
            finally:
                conn.close()

    def test_08_store_settle_delay_is_configurable(self):
        class FakeClient:
            def store(self, share_url, to_pdir_path="/"):
                return "file-id", "name", "https://example.com/share"

        with patch.dict(os.environ, {"PAN_STORE_SETTLE_SECONDS": "0"}), patch("src.pan_operator.time.sleep") as mocked_sleep:
            self.assertEqual(_execute_store(FakeClient(), "https://example.com/src"), ("file-id", "name", "https://example.com/share"))
            mocked_sleep.assert_not_called()

        with patch.dict(os.environ, {"PAN_STORE_SETTLE_SECONDS": "0.25"}), patch("src.pan_operator.time.sleep") as mocked_sleep:
            _execute_store(FakeClient(), "https://example.com/src")
            mocked_sleep.assert_called_once_with(0.25)


if __name__ == "__main__":
    unittest.main()
