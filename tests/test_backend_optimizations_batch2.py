import json
import os
import threading
import unittest
import uuid
from unittest.mock import MagicMock, patch

import requests

from app import app
from src.db.connection import get_db_connection
from src.db.system_configs import delete_config_value, set_config_value
from src.models.search_item import SearchResultItem
from src.pan_operator import _TRANSFER_LOCKS, _get_transfer_lock, create_share
from src.services import search_service
from src.services.search_service import (
    generate_search_stream_events,
    get_cached_search_items,
    set_cached_search_items,
)


def _item(index, cloud_name="百度网盘", keyword="uniquekw"):
    return SearchResultItem(
        source="api",
        title=f"{keyword}-{index}",
        share_link=f"https://pan.baidu.com/s/zz{keyword}{index}",
        cloud_name=cloud_name,
    )


class SearchCacheSemanticsTestCase(unittest.TestCase):
    """缓存只能保存“完整枚举、与调用方过滤无关”的全集。"""

    @classmethod
    def setUpClass(cls):
        cls.app = app

    def _run_stream(self, items, scope="all", limit=None, apply_frontend_filter=False, keyword="uniquekw"):
        events = generate_search_stream_events(
            keyword,
            client_ip="127.0.0.1",
            action="search.test",
            apply_frontend_filter=apply_frontend_filter,
            limit=limit,
            scope=scope,
        )
        return [json.loads(event) for event in events]

    def test_01_full_enumeration_caches_complete_unfiltered_set(self):
        upstream_items = [_item(1, "百度网盘"), _item(2, "夸克网盘")]

        with patch.object(search_service, "get_cached_search_items", return_value=None),              patch.object(search_service, "search_in_database", return_value=[]),              patch.object(search_service, "iter_upstream_search_results", return_value=iter([upstream_items])),              patch.object(search_service, "get_allowed_frontend_netdisks", return_value={"夸克网盘"}),              patch.object(search_service, "record_log"),              patch.object(search_service, "set_cached_search_items") as mocked_cache:
            events = self._run_stream(upstream_items, apply_frontend_filter=True)

        completed = [event for event in events if event["type"] == "complete"][0]
        self.assertEqual(len(completed["results"]), 1, "输出仍应按前台配置只返回夸克网盘结果")
        mocked_cache.assert_called_once()
        cached_items = mocked_cache.call_args[0][1]
        self.assertEqual({item.cloud_name for item in cached_items}, {"百度网盘", "夸克网盘"}, "缓存必须是过滤前的全集")

    def test_02_scope_own_does_not_write_cache(self):
        own_items = [_item(3)]

        with patch.object(search_service, "get_cached_search_items", return_value=None),              patch.object(search_service, "search_in_database", return_value=own_items),              patch.object(search_service, "iter_upstream_search_results") as mocked_upstream,              patch.object(search_service, "record_log"),              patch.object(search_service, "set_cached_search_items") as mocked_cache:
            events = self._run_stream(own_items, scope="own")

        self.assertTrue(any(event["type"] == "complete" for event in events))
        mocked_upstream.assert_not_called()
        mocked_cache.assert_not_called()

    def test_03_limit_break_does_not_write_partial_cache(self):
        upstream_items = [_item(4), _item(5)]

        with patch.object(search_service, "get_cached_search_items", return_value=None),              patch.object(search_service, "search_in_database", return_value=[]),              patch.object(search_service, "iter_upstream_search_results", return_value=iter([upstream_items])),              patch.object(search_service, "record_log"),              patch.object(search_service, "set_cached_search_items") as mocked_cache:
            events = self._run_stream(upstream_items, limit=1)

        completed = [event for event in events if event["type"] == "complete"][0]
        self.assertEqual(len(completed["results"]), 1)
        mocked_cache.assert_not_called()

    def test_04_config_write_invalidates_search_cache(self):
        keyword = f"cacheinv{uuid.uuid4().hex[:6]}"
        set_cached_search_items(keyword, [_item(6, keyword=keyword)])
        self.assertIsNotNone(get_cached_search_items(keyword))

        config_key = f"cache_inv_{uuid.uuid4().hex[:6]}"
        set_config_value(config_key, {"v": 1})
        try:
            self.assertIsNone(get_cached_search_items(keyword), "保存系统配置后搜索缓存必须立即失效")
        finally:
            delete_config_value(config_key)

    def test_05_cache_caps_items_per_keyword(self):
        keyword = f"cachecap{uuid.uuid4().hex[:6]}"
        items = [_item(index, keyword=keyword) for index in range(search_service.SEARCH_CACHE_MAX_ITEMS_PER_KEYWORD + 1)]
        set_cached_search_items(keyword, items)
        try:
            cached = get_cached_search_items(keyword)
            self.assertIsNotNone(cached)
            self.assertEqual(len(cached), search_service.SEARCH_CACHE_MAX_ITEMS_PER_KEYWORD, "单关键词缓存条数必须有上限")
        finally:
            search_service.clear_search_cache()


class SseHeaderTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = app
        cls.client = cls.app.test_client()

    def test_01_web_stream_has_no_cache_headers(self):
        with patch("src.routes.search_routes.is_frontend_enabled", return_value=True),              patch("src.routes.search_routes.get_security_config", return_value={"min_keyword_length": 2}),              patch("src.routes.search_routes.check_request_security", return_value=(True, "", 200)),              patch("src.routes.search_routes.get_current_client_ip", return_value="127.0.0.1"),              patch("src.routes.search_routes.IPConcurrencyGuard", MagicMock()),              patch(
                 "src.routes.search_routes.generate_search_stream_events",
                 return_value=iter([json.dumps({"type": "complete", "results": []})]),
             ):
            response = self.client.get("/api/search_stream?keyword=测试")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("Cache-Control"), "no-cache")
        self.assertEqual(response.headers.get("X-Accel-Buffering"), "no")
        self.assertEqual(response.get_data(as_text=True).count("data:"), 1)

    def test_02_api_v1_stream_has_no_cache_headers(self):
        with patch("src.routes.api_v1_routes.is_public_search_api_enabled", return_value=True),              patch("src.routes.api_v1_routes.get_security_config", return_value={"min_keyword_length": 2}),              patch("src.routes.api_v1_routes.check_request_security", return_value=(True, "", 200)),              patch("src.routes.api_v1_routes.get_current_client_ip", return_value="127.0.0.1"),              patch("src.routes.api_v1_routes.IPConcurrencyGuard", MagicMock()),              patch(
                 "src.routes.api_v1_routes._resolve_search_api_options",
                 return_value={"keyword": "测试", "limit": 10, "target_clouds": [], "scope": "all"},
             ),              patch(
                 "src.routes.api_v1_routes.generate_search_stream_events",
                 return_value=iter([json.dumps({"type": "complete", "results": []})]),
             ):
            response = self.client.get("/api/v1/search/stream?keyword=测试")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("Cache-Control"), "no-cache")
        self.assertEqual(response.headers.get("X-Accel-Buffering"), "no")


class TransferRobustnessTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = app

    def _fake_pool(self, clients):
        pool = MagicMock()
        accounts = [
            {"id": index + 1, "account_name": f"acc{index + 1}", "cloud_name": "百度网盘"}
            for index in range(len(clients))
        ]
        clients_by_id = {}
        for index, client in enumerate(clients):
            clients_by_id[accounts[index]["id"]] = client
        pool.get_candidate_accounts.return_value = accounts
        pool.select_account_for_transfer.return_value = (accounts[0], clients[0])
        pool.create_client_for_account.side_effect = [clients[index] for index in range(1, len(clients))]
        return pool, accounts, clients_by_id

    def test_01_read_timeout_stops_account_failover(self):
        class TimeoutClient:
            def store(self, share_url, to_pdir_path="/"):
                raise requests.exceptions.ReadTimeout("read timed out")

            def del_file(self, target):
                return True

        second = MagicMock()
        pool, accounts, _ = self._fake_pool([TimeoutClient(), second])
        share_data = {
            "share_url": f"https://pan.baidu.com/s/1timeout{uuid.uuid4().hex[:6]}",
            "title": "超时资源",
            "save_to_netdisk": {"baidu": True},
        }

        with patch("src.services.account_pool_manager.AccountPoolManager.get_instance", return_value=pool),              patch("src.pan_operator.check_link", return_value={"state": "ok"}),              patch("src.services.system_config_service.get_transfer_target_dir", return_value=""):
            result = create_share(share_data)

        self.assertEqual(result, share_data)
        second.store.assert_not_called()
        pool.report_failure.assert_called()
        self.assertFalse(pool.report_failure.call_args.kwargs.get("is_fatal", True))

    def test_02_local_persist_failure_rolls_back_remote_file(self):
        class FakeClient:
            def __init__(self):
                self.deleted = []

            def store(self, share_url, to_pdir_path="/"):
                return "file-xyz", "name", "https://pan.baidu.com/s/new"

            def del_file(self, target):
                self.deleted.append(target)
                return True

        client = FakeClient()
        pool, _, _ = self._fake_pool([client])
        share_data = {
            "share_url": f"https://pan.baidu.com/s/1persist{uuid.uuid4().hex[:6]}",
            "title": "入库失败资源",
            "save_to_netdisk": {"baidu": True},
        }

        with patch("src.services.account_pool_manager.AccountPoolManager.get_instance", return_value=pool),              patch("src.pan_operator.check_link", return_value={"state": "ok"}),              patch("src.pan_operator.insert_resource", return_value=None),              patch("src.services.system_config_service.get_transfer_target_dir", return_value=""):
            result = create_share(share_data)

        self.assertIsNone(result, "本地入库失败时不能返回成功")
        self.assertEqual(len(client.deleted), 1, "必须尽力回收远端已转存文件")

    def test_03_transfer_locks_are_bounded(self):
        self.assertEqual(len(_TRANSFER_LOCKS), 64)
        url = f"https://pan.baidu.com/s/1lock{uuid.uuid4().hex[:6]}"
        self.assertIs(_get_transfer_lock(url), _get_transfer_lock(url))


class SchedulerAndIndexTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = app

    def test_01_start_scheduler_is_idempotent(self):
        from src.services import scheduler_service

        first = scheduler_service.start_scheduler()
        second = scheduler_service.start_scheduler()
        self.assertIs(first, second)

    def test_02_indexes_exist_and_are_used(self):
        conn = get_db_connection()
        try:
            index_names = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
            }
            self.assertIn("idx_resources_share_created", index_names)
            self.assertIn("idx_temp_share_status_expires", index_names)

            hall_plan = " ".join(
                str(row[-1])
                for row in conn.execute(
                    "EXPLAIN QUERY PLAN SELECT id FROM resources "
                    "WHERE share_link IS NOT NULL AND share_link <> '' ORDER BY created_at DESC, id DESC LIMIT 8"
                ).fetchall()
            )
            self.assertIn("idx_resources_share_created", hall_plan)

            temp_plan = " ".join(
                str(row[-1])
                for row in conn.execute(
                    "EXPLAIN QUERY PLAN SELECT id FROM temp_share "
                    "WHERE status='active' AND expires_at <= datetime('now') ORDER BY expires_at ASC LIMIT 10"
                ).fetchall()
            )
            self.assertIn("idx_temp_share_status_expires", temp_plan)
        finally:
            conn.close()

    def test_03_legacy_database_indexes_are_recreated_on_init(self):
        """旧库缺少新索引时，init_sqlite_db() 应自动补建（兼容升级路径）。"""
        from src.db.connection import init_sqlite_db

        conn = get_db_connection()
        try:
            conn.execute("DROP INDEX IF EXISTS idx_resources_share_created")
            conn.execute("DROP INDEX IF EXISTS idx_resources_created_at")
            conn.execute("DROP INDEX IF EXISTS idx_temp_share_status_expires")
            conn.commit()
        finally:
            conn.close()

        init_sqlite_db()

        conn = get_db_connection()
        try:
            index_names = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
            }
        finally:
            conn.close()
        self.assertIn("idx_resources_share_created", index_names)
        self.assertIn("idx_temp_share_status_expires", index_names)


if __name__ == "__main__":
    unittest.main()
