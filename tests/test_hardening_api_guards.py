"""对外 API 边界加固测试。

覆盖：
1. GET /api/v1/resources 分页参数钳制（page_size 负数/超大、page=0 均返回有界结果）。
2. src.db.resources.list_resources 内部防御性钳制（不依赖路由层）。
3. /api/v1/search 的 limit 钳制与 limit_lock 上限语义。
4. /api/v1/search/stream 的 SSE 并发闸门：名额占满返回 503，释放后恢复，且响应头可防缓存/缓冲。
5. /api/v1/transfer 幂等键：同键回放缓存、不同键重新执行、超短键被忽略、
   明确的 4xx 业务失败被缓存、5xx/异常路径不缓存。

约束：全部使用 unittest.mock 打桩，不依赖真实网络与真实转存；
临时 SQLITE_DB_PATH 由外部命令注入（data/pan_relay.team2.db）。
"""

import json
import os
import unittest
import uuid
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

from flask import request as flask_request

from app import app
from src.db.resources import list_resources
from src.routes import api_v1_routes
from src.services.link_checker import STATE_BAD
from src.utils.request_limits import max_page_size, max_search_limit
from src.utils.stream_guard import active_streams, acquire_stream_slot, release_stream_slot


TRANSFER_URL = "https://pan.quark.cn/s/hardening-idem-001"


def _new_key() -> str:
    """每次生成全新幂等键，避免跨测试轮次命中持久化缓存。"""
    return "idem-" + uuid.uuid4().hex


def _transfer_patches(stack: ExitStack) -> MagicMock:
    """打桩转存链路的全部外部依赖，只保留 API 路由与 resolve_view_url 真实逻辑。"""
    stack.enter_context(patch.object(api_v1_routes, "get_transfer_api_key", return_value=""))
    stack.enter_context(patch("src.services.system_config_service.get_frontend_link_mode", return_value="view"))
    stack.enter_context(
        patch("src.services.system_config_service.get_allowed_dynamic_transfer_netdisks", return_value={"夸克网盘"})
    )
    stack.enter_context(
        patch("src.services.system_config_service.get_storage_cleanup_config", return_value={"retention_minutes": 60})
    )
    stack.enter_context(patch("src.services.temp_share_service.get_active_temp_share", return_value=None))
    stack.enter_context(patch("src.services.temp_share_service.create_temp_share_record", return_value=1))
    create_share = stack.enter_context(patch("src.services.temp_share_service.create_share"))
    create_share.return_value = {
        "share_url": "https://pan.quark.cn/s/idem-share-001",
        "file_id": "fid-idem-001",
        "account_id": 1,
    }
    pool = MagicMock()
    pool.get_candidate_accounts.return_value = [{"id": 1}]
    stack.enter_context(
        patch("src.services.account_pool_manager.AccountPoolManager.get_instance", return_value=pool)
    )
    return create_share


def _transfer_body(**overrides) -> dict:
    body = {
        "url": TRANSFER_URL,
        "title": "幂等测试资源",
        "netdisk_name": "夸克网盘",
        "skip_check": True,
    }
    body.update(overrides)
    return body


class ResourcesPaginationGuardTest(unittest.TestCase):
    """/api/v1/resources 对外分页参数钳制。"""

    def setUp(self):
        self.client = app.test_client()

    def _get_data(self, query: str) -> dict:
        resp = self.client.get(f"/api/v1/resources?{query}")
        self.assertEqual(200, resp.status_code)
        body = resp.get_json()
        self.assertTrue(body["success"])
        data = body["data"]
        self.assertGreaterEqual(data["total_pages"], 0)
        self.assertGreaterEqual(data["current_page"], 1)
        self.assertGreaterEqual(data["page_size"], 1)
        self.assertLessEqual(data["page_size"], max_page_size())
        return data

    def test_negative_page_size_is_clamped(self):
        data = self._get_data("page=1&page_size=-1")
        self.assertEqual(1, data["page_size"])

    def test_huge_page_size_is_capped(self):
        data = self._get_data("page=1&page_size=100000")
        self.assertEqual(max_page_size(), data["page_size"])
        self.assertLessEqual(len(data["items"]), max_page_size())

    def test_page_zero_is_clamped_to_one(self):
        data = self._get_data("page=0&page_size=5")
        self.assertEqual(1, data["current_page"])
        self.assertEqual(5, data["page_size"])
        self.assertEqual((data["total_count"] + 4) // 5, data["total_pages"])


class ListResourcesDefensiveClampTest(unittest.TestCase):
    """即使调用方未校验，db 层 list_resources 也必须自我钳制。"""

    def test_db_layer_clamps_negative_and_huge_pagination(self):
        ok, msg, data = list_resources(page=-3, page_size=100000)
        self.assertTrue(ok, msg)
        self.assertEqual(1, data["current_page"])
        self.assertLessEqual(data["page_size"], max_page_size())
        self.assertEqual(data["page_size"], max_page_size())

    def test_db_layer_clamps_zero_values(self):
        ok, msg, data = list_resources(page=0, page_size=0)
        self.assertTrue(ok, msg)
        self.assertEqual(1, data["current_page"])
        self.assertEqual(1, data["page_size"])


class SearchLimitGuardTest(unittest.TestCase):
    """/api/v1/search 的 limit 必须落在 [1, MAX_SEARCH_LIMIT] 内。"""

    def setUp(self):
        self.client = app.test_client()

    def test_huge_limit_is_capped_in_resolver(self):
        with patch.dict(os.environ, {"MAX_SEARCH_LIMIT": "20"}):
            with app.test_request_context("/api/v1/search?keyword=测试关键词&limit=100000&scope=all"):
                opts = api_v1_routes._resolve_search_api_options(flask_request)
            self.assertEqual(20, opts["limit"])
            with app.test_request_context("/api/v1/search?keyword=测试关键词&limit=-5&scope=all"):
                opts = api_v1_routes._resolve_search_api_options(flask_request)
            self.assertGreaterEqual(opts["limit"], 1)
            self.assertLessEqual(opts["limit"], 20)

    def test_limit_lock_still_caps_to_config_value(self):
        with patch.object(api_v1_routes, "get_search_api_limit", return_value=30), patch.object(
            api_v1_routes, "get_search_api_limit_lock", return_value=True
        ), patch.object(api_v1_routes, "get_search_api_scope", return_value="all"), patch.object(
            api_v1_routes, "get_search_api_scope_lock", return_value=False
        ):
            with app.test_request_context("/api/v1/search?keyword=测试关键词&limit=1000"):
                opts = api_v1_routes._resolve_search_api_options(flask_request)
            self.assertEqual(30, opts["limit"])
            with app.test_request_context("/api/v1/search?keyword=测试关键词&limit=5"):
                opts = api_v1_routes._resolve_search_api_options(flask_request)
            self.assertEqual(5, opts["limit"])

    def test_sync_search_route_passes_clamped_limit(self):
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {"MAX_SEARCH_LIMIT": "15"}))
            stack.enter_context(patch.object(api_v1_routes, "is_public_search_api_enabled", return_value=True))
            stack.enter_context(
                patch.object(api_v1_routes, "get_security_config", return_value={"min_keyword_length": 1})
            )
            stack.enter_context(patch.object(api_v1_routes, "check_request_security", return_value=(True, "", 200)))
            stack.enter_context(patch.object(api_v1_routes, "get_current_client_ip", return_value="127.0.0.1"))
            stack.enter_context(patch.object(api_v1_routes, "IPConcurrencyGuard"))
            stack.enter_context(patch.object(api_v1_routes, "get_search_api_scope", return_value="all"))
            stack.enter_context(patch.object(api_v1_routes, "get_search_api_scope_lock", return_value=False))
            search_mock = stack.enter_context(
                patch.object(api_v1_routes, "search_public_resources", return_value=(True, "ok", []))
            )
            resp = self.client.get("/api/v1/search?keyword=测试关键词&limit=99999&scope=all")
        self.assertEqual(200, resp.status_code)
        kwargs = search_mock.call_args.kwargs
        self.assertLessEqual(kwargs["limit"], 15)
        self.assertGreaterEqual(kwargs["limit"], 1)


class SearchStreamConcurrencyGuardTest(unittest.TestCase):
    "/api/v1/search/stream 的 SSE 并发闸门。"

    def setUp(self):
        self.client = app.test_client()

    def _sse_patches(self, stack: ExitStack) -> MagicMock:
        stack.enter_context(patch.object(api_v1_routes, "is_public_search_api_enabled", return_value=True))
        stack.enter_context(
            patch.object(api_v1_routes, "get_security_config", return_value={"min_keyword_length": 1})
        )
        stack.enter_context(patch.object(api_v1_routes, "check_request_security", return_value=(True, "", 200)))
        stack.enter_context(patch.object(api_v1_routes, "get_current_client_ip", return_value="127.0.0.1"))
        stack.enter_context(patch.object(api_v1_routes, "IPConcurrencyGuard"))
        stack.enter_context(
            patch.object(
                api_v1_routes,
                "_resolve_search_api_options",
                return_value={"keyword": "测试关键词", "limit": 5, "target_clouds": [], "scope": "own"},
            )
        )
        gen_mock = stack.enter_context(
            patch.object(
                api_v1_routes,
                "generate_search_stream_events",
                side_effect=lambda *args, **kwargs: iter([json.dumps({"success": True, "done": True})]),
            )
        )
        return gen_mock

    def test_returns_503_when_saturated_and_recovers_after_release(self):
        baseline = active_streams()
        limit = baseline + 1  # 容忍同进程其他用例可能残留的名额
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {"SSE_MAX_CONCURRENT": str(limit)}))
            gen_mock = self._sse_patches(stack)
            self.assertTrue(acquire_stream_slot())
            try:
                resp = self.client.get("/api/v1/search/stream?keyword=测试关键词")
                self.assertEqual(503, resp.status_code)
                body = resp.get_json()
                self.assertFalse(body["success"])
                self.assertEqual("当前流式搜索连接数已达上限，请稍后重试", body["message"])
                gen_mock.assert_not_called()
            finally:
                release_stream_slot()

            resp2 = self.client.get("/api/v1/search/stream?keyword=测试关键词")
            self.assertEqual(200, resp2.status_code)
            self.assertEqual("text/event-stream", resp2.headers.get("Content-Type").split(";")[0])
            self.assertEqual("no-cache", resp2.headers.get("Cache-Control"))
            self.assertEqual("no", resp2.headers.get("X-Accel-Buffering"))
            gen_mock.assert_called_once()
            resp2.close()
        self.assertEqual(baseline, active_streams())


class TransferIdempotencyTest(unittest.TestCase):
    "/api/v1/transfer 的幂等键语义。"

    def setUp(self):
        self.client = app.test_client()

    def test_same_key_replays_cached_response(self):
        key = _new_key()
        with ExitStack() as stack:
            create_share = _transfer_patches(stack)
            headers = {"Idempotency-Key": key}
            first = self.client.post("/api/v1/transfer", json=_transfer_body(), headers=headers)
            self.assertEqual(200, first.status_code)
            first_payload = first.get_json()
            self.assertTrue(first_payload["success"])
            self.assertEqual("temp_share", first_payload["data"]["mode"])
            self.assertEqual(1, create_share.call_count)

            second = self.client.post("/api/v1/transfer", json=_transfer_body(), headers=headers)
            self.assertEqual(200, second.status_code)
            self.assertEqual(1, create_share.call_count)  # 第二次命中缓存，未再转存
            self.assertEqual(first_payload, second.get_json())

    def test_different_keys_still_execute_and_short_key_is_ignored(self):
        with ExitStack() as stack:
            create_share = _transfer_patches(stack)
            key_a, key_b = _new_key(), _new_key()
            self.client.post("/api/v1/transfer", json=_transfer_body(), headers={"Idempotency-Key": key_a})
            # body 字段方式同样生效
            resp = self.client.post("/api/v1/transfer", json=_transfer_body(idempotency_key=key_b))
            self.assertEqual(200, resp.status_code)
            self.assertEqual(2, create_share.call_count)

            # 长度 < 8 的键视为无效，不产生缓存，重复请求仍会执行
            short_key = {"Idempotency-Key": "k-123"}
            self.client.post("/api/v1/transfer", json=_transfer_body(), headers=short_key)
            self.client.post("/api/v1/transfer", json=_transfer_body(), headers=short_key)
            self.assertEqual(4, create_share.call_count)

    def test_explicit_business_failure_is_cached(self):
        key = _new_key()
        with ExitStack() as stack:
            stack.enter_context(patch.object(api_v1_routes, "get_transfer_api_key", return_value=""))
            check_mock = stack.enter_context(patch.object(api_v1_routes, "check_link"))
            check_mock.return_value = {"state": STATE_BAD, "summary": "链接已失效"}
            body = _transfer_body(skip_check=False)
            headers = {"Idempotency-Key": key}

            first = self.client.post("/api/v1/transfer", json=body, headers=headers)
            self.assertEqual(422, first.status_code)
            self.assertEqual("LINK_INVALID", first.get_json()["code"])
            self.assertEqual(1, check_mock.call_count)

            second = self.client.post("/api/v1/transfer", json=body, headers=headers)
            self.assertEqual(422, second.status_code)
            self.assertEqual(1, check_mock.call_count)  # 命中缓存，未再测活
            self.assertEqual(first.get_json(), second.get_json())

    def test_server_error_is_not_cached(self):
        key = _new_key()
        with ExitStack() as stack:
            stack.enter_context(patch.object(api_v1_routes, "get_transfer_api_key", return_value=""))
            resolve_mock = stack.enter_context(
                patch.object(api_v1_routes, "resolve_view_url", side_effect=RuntimeError("boom"))
            )
            headers = {"Idempotency-Key": key}
            first = self.client.post("/api/v1/transfer", json=_transfer_body(), headers=headers)
            self.assertEqual(500, first.status_code)
            second = self.client.post("/api/v1/transfer", json=_transfer_body(), headers=headers)
            self.assertEqual(500, second.status_code)
            self.assertEqual(2, resolve_mock.call_count)  # 5xx 不缓存，允许重试


if __name__ == "__main__":
    unittest.main()
