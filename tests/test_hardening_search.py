"""搜索链路稳定性加固测试。

覆盖：
1. 源级熔断状态机：连续失败进入冷却、冷却期跳过、half-open 单探测、成功恢复、指数退避。
2. 插件专用线程池：与 API/TG 共享池隔离、单例、线程名前缀。
3. 插件硬超时/异常隔离：挂死插件不阻塞其他源返回，失败按源统计。
4. SEARCH_CIRCUIT_BREAKER_ENABLED=0 时行为与历史版本一致。
5. 仅当 plugin.search 签名接受 timeout 时才向插件传递剩余超时。

约束：全部使用 unittest.mock 打桩，不依赖真实网络；临时 SQLITE_DB_PATH 由外部命令注入。
"""

import os
import threading
import time
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from src.models.search_item import SearchResultItem
from src.services import search_service


class _FakeClock:
    """可手动推进的单调时钟，避免熔断测试真的等待 60 秒。"""

    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _FakePlugin:
    """最小插件替身：统计调用次数并执行给定回调（回调签名 search(keyword)）。"""

    def __init__(self, name: str, search_impl):
        self.name = name
        self.display_name = name
        self.search_calls = 0
        self._search_impl = search_impl

    def search(self, keyword):
        self.search_calls += 1
        return self._search_impl(keyword)


def _make_item(source: str, title: str, link: str) -> SearchResultItem:
    return SearchResultItem(source=source, title=title, share_link=link, cloud_name="夸克网盘")


def _raise_search(_keyword):
    raise RuntimeError("fake plugin boom")


def _api_config() -> dict:
    return {
        "name": "api-stub",
        "url": "https://example.invalid/search?q=[[keyword]]",
        "method": "GET",
        "request": "{}",
        "response": "data",
        "headers": None,
        "is_enabled": True,
        "response_time_ms": 1,
    }


def _scheduler_config(plugin_timeout: float = 1.0) -> dict:
    return {
        "api": {"timeout": 5, "max_workers": 8},
        # TG 关闭，避免任何频道搜索请求（测试禁止真实网络）
        "tg": {"enabled": False, "max_workers": 2, "timeout": 5, "proxy": ""},
        "plugin": {"timeout": plugin_timeout, "max_workers": 8},
    }


def _run_upstream(api_results, plugins, plugin_timeout: float = 1.0, api_enabled: bool = True):
    """在全部上游打桩的环境下执行一次 iter_upstream_search_results。

    api_enabled=False 时不注入任何 API 源，便于单独观察插件源。
    """
    api_configs = [_api_config()] if api_enabled else []
    with ExitStack() as stack:
        stack.enter_context(
            patch(
                "src.services.system_config_service.get_search_scheduler_config",
                return_value=_scheduler_config(plugin_timeout),
            )
        )
        stack.enter_context(
            patch.object(search_service, "read_all_api_configs_from_db", return_value=api_configs)
        )
        stack.enter_context(patch.object(search_service, "process_config", return_value=list(api_results)))
        stack.enter_context(patch.object(search_service, "get_enabled_channel_names", return_value=[]))
        stack.enter_context(
            patch.object(search_service.plugin_manager, "get_enabled_plugins", return_value=list(plugins))
        )
        return list(search_service.iter_upstream_search_results("测试关键词"))


class SourceCircuitBreakerStateMachineTest(unittest.TestCase):
    """直接实例化熔断器，验证三态状态机与退避策略。"""

    def setUp(self):
        self.clock = _FakeClock()
        self.breaker = search_service.SourceCircuitBreaker(
            failure_threshold=3,
            cooldown_seconds=60.0,
            max_cooldown_seconds=300.0,
            clock=self.clock,
        )

    def test_01_open_on_three_failures_cooldown_skip_and_success_recovery(self):
        key = "api:flaky"
        self.assertTrue(self.breaker.allow(key))

        self.breaker.record_failure(key)
        self.breaker.record_failure(key)
        self.assertTrue(self.breaker.allow(key), "未达到 3 次连续失败不应熔断")

        self.breaker.record_failure(key)  # 第 3 次连续失败
        state = self.breaker.snapshot(key)
        self.assertEqual(3, state["failures"])
        self.assertAlmostEqual(60.0, state["open_until"] - self.clock.now, places=3)
        self.assertFalse(self.breaker.allow(key), "冷却期内该源必须被直接跳过")

        self.clock.advance(30.0)
        self.assertFalse(self.breaker.allow(key), "冷却未结束仍需跳过")

        self.clock.advance(31.0)  # 越过 60 秒冷却
        self.assertTrue(self.breaker.allow(key), "冷却结束后 half-open 应放行一个探测")
        self.assertFalse(self.breaker.allow(key), "half-open 阶段同时只允许一个探测")

        self.breaker.record_success(key)
        self.assertTrue(self.breaker.allow(key), "探测成功后应恢复放行")
        snapshot = self.breaker.snapshot(key)
        self.assertEqual(0, snapshot["failures"])
        self.assertEqual(0.0, snapshot["open_until"])

    def test_02_half_open_failure_recools_with_exponential_backoff_up_to_cap(self):
        key = "tg:bad_channel"
        for _ in range(3):
            self.breaker.record_failure(key)
        self.assertAlmostEqual(60.0, self.breaker.snapshot(key)["open_until"] - self.clock.now, places=3)

        for expected_cooldown in (120.0, 240.0, 300.0):
            remaining = self.breaker.snapshot(key)["open_until"] - self.clock.now
            self.clock.advance(remaining + 1.0)
            self.assertTrue(self.breaker.allow(key), "冷却结束后应放行探测")
            self.breaker.record_failure(key)  # 探测失败 -> 立即再次冷却
            self.assertAlmostEqual(
                expected_cooldown,
                self.breaker.snapshot(key)["open_until"] - self.clock.now,
                places=3,
            )
        self.assertFalse(self.breaker.allow(key))

    def test_03_single_success_resets_consecutive_failure_counter(self):
        key = "plugin:flaky"
        self.breaker.record_failure(key)
        self.breaker.record_failure(key)
        self.breaker.record_success(key)
        self.breaker.record_failure(key)
        self.breaker.record_failure(key)
        self.assertTrue(self.breaker.allow(key), "成功后计数已清零，两次失败不应触发熔断")
        self.assertEqual(2, self.breaker.snapshot(key)["failures"])

    def test_04_release_probe_returns_half_open_slot(self):
        key = "api:probe"
        for _ in range(3):
            self.breaker.record_failure(key)
        self.clock.advance(61.0)
        self.assertTrue(self.breaker.allow(key))
        self.assertFalse(self.breaker.allow(key), "探测名额已被占用")
        self.breaker.release_probe(key)
        self.assertTrue(self.breaker.allow(key), "被取消的探测任务必须归还 half-open 名额")


class PluginExecutorIsolationTest(unittest.TestCase):
    """插件专用线程池与 API/TG 共享池隔离。"""

    def test_plugin_executor_is_singleton_and_distinct_from_upstream_pool(self):
        search_executor = search_service._get_search_executor()
        plugin_executor = search_service._get_plugin_executor()
        self.assertIs(plugin_executor, search_service._get_plugin_executor(), "插件执行器必须是单例")
        self.assertIsNot(plugin_executor, search_executor, "插件执行器不能与 API/TG 共享池是同一实例")
        self.assertEqual("search-plugin", plugin_executor._thread_name_prefix)
        self.assertEqual(search_service._SEARCH_PLUGIN_MAX_WORKERS, plugin_executor._max_workers)
        self.assertGreaterEqual(plugin_executor._max_workers, 2, "SEARCH_PLUGIN_MAX_WORKERS 下限为 2")

    def test_plugin_tasks_actually_run_on_plugin_thread_pool(self):
        def _probe_thread_name():
            return threading.current_thread().name

        thread_name = search_service._get_plugin_executor().submit(_probe_thread_name).result(timeout=10)
        self.assertTrue(thread_name.startswith("search-plugin"), thread_name)


class PluginTimeoutAndFailureIsolationTest(unittest.TestCase):
    """挂死/异常插件不得影响其他源结果返回，且失败计入源级熔断。"""

    def setUp(self):
        env_patcher = patch.dict(os.environ, {"SEARCH_CIRCUIT_BREAKER_ENABLED": "1"})
        env_patcher.start()
        self.addCleanup(env_patcher.stop)
        search_service._get_circuit_breaker().reset()
        self.addCleanup(search_service._get_circuit_breaker().reset)

    def test_hanging_and_failing_plugins_do_not_block_other_sources(self):
        release_hanging = threading.Event()

        def _hanging_search(_keyword):
            release_hanging.wait(5.0)
            return [_make_item("other", "挂死插件结果", "https://pan.quark.cn/s/hang")]

        hanging = _FakePlugin("plugin-hang", _hanging_search)
        failing = _FakePlugin("plugin-fail", _raise_search)
        healthy = _FakePlugin(
            "plugin-ok",
            lambda _keyword: [_make_item("other", "健康插件结果", "https://pan.quark.cn/s/plugin-ok")],
        )
        api_item = _make_item("other", "API 结果", "https://pan.quark.cn/s/api-ok")

        started = time.monotonic()
        try:
            batches = _run_upstream([api_item], [hanging, failing, healthy], plugin_timeout=1.0)
            elapsed = time.monotonic() - started
        finally:
            release_hanging.set()

        titles = [item.title for batch in batches for item in batch]
        self.assertIn("API 结果", titles, "挂死插件不得阻塞 API 结果返回")
        self.assertIn("健康插件结果", titles, "健康插件结果必须正常返回")
        self.assertNotIn("挂死插件结果", titles, "超时插件的结果必须被丢弃")
        self.assertLess(elapsed, 5.0, "整体循环不能被挂死插件阻塞")
        self.assertEqual(1, healthy.search_calls)

        breaker = search_service._get_circuit_breaker()
        hang_state = breaker.snapshot("plugin:plugin-hang")
        self.assertIsNotNone(hang_state, "超时插件应被熔断器记录一次失败")
        self.assertEqual(1, hang_state["failures"])
        self.assertEqual(1, breaker.snapshot("plugin:plugin-fail")["failures"], "插件抛异常也应记为源失败")
        self.assertIsNone(breaker.snapshot("plugin:plugin-ok"), "成功插件不产生失败状态")

    def test_plugin_with_repeated_failures_is_skipped_during_cooldown(self):
        failing = _FakePlugin("plugin-always-fail", _raise_search)

        for _ in range(3):
            _run_upstream([], [failing])
        self.assertEqual(3, failing.search_calls)
        state = search_service._get_circuit_breaker().snapshot("plugin:plugin-always-fail")
        self.assertEqual(3, state["failures"])

        batches = _run_upstream([], [failing], api_enabled=False)
        self.assertEqual([], batches, "冷却期内的插件源应被直接跳过，不产出任何批次")
        self.assertEqual(3, failing.search_calls, "冷却期内不应再次提交插件任务")


class CircuitBreakerToggleTest(unittest.TestCase):
    """SEARCH_CIRCUIT_BREAKER_ENABLED=0 时行为与历史版本一致。"""

    def test_disabled_breaker_never_degrades_or_skips_sources(self):
        with patch.dict(os.environ, {"SEARCH_CIRCUIT_BREAKER_ENABLED": "0"}):
            breaker = search_service._get_circuit_breaker()
            breaker.reset()

            for _ in range(5):
                search_service._source_failed("plugin:plugin-off")
            self.assertTrue(
                search_service._source_allowed("plugin:plugin-off"),
                "熔断关闭时冷却期判断必须恒为放行",
            )
            self.assertIsNone(breaker.snapshot("plugin:plugin-off"), "熔断关闭时不应记录任何状态")

            failing = _FakePlugin("plugin-ok-off", _raise_search)
            for _ in range(3):
                _run_upstream([], [failing])
            self.assertEqual(3, failing.search_calls)
            _run_upstream([], [failing])
            self.assertEqual(4, failing.search_calls, "熔断关闭后每次搜索都应重新尝试该插件")

        search_service._get_circuit_breaker().reset()


class PluginTimeoutSignatureTest(unittest.TestCase):
    """仅当 plugin.search 签名接受 timeout 时才传递剩余超时。"""

    def test_timeout_passed_only_when_signature_accepts_it(self):
        class _TimeoutAwarePlugin:
            name = "plugin-timeout-aware"
            display_name = "感知超时插件"

            def __init__(self):
                self.received = []

            def search(self, keyword, timeout=None):
                self.received.append(timeout)
                return [_make_item("other", "aware", "https://pan.quark.cn/s/aware")]

        class _LegacyPlugin:
            name = "plugin-legacy"
            display_name = "老插件"

            def search(self, keyword):
                return [_make_item("other", "legacy", "https://pan.quark.cn/s/legacy")]

        class _VarKwargsPlugin:
            name = "plugin-var-kwargs"
            display_name = "kwargs 插件"

            def __init__(self):
                self.received = []

            def search(self, keyword, **kwargs):
                self.received.append(kwargs.get("timeout"))
                return []

        aware = _TimeoutAwarePlugin()
        legacy = _LegacyPlugin()
        var_kwargs = _VarKwargsPlugin()

        self.assertEqual("keyword", search_service._plugin_search_timeout_mode(aware))
        self.assertIsNone(search_service._plugin_search_timeout_mode(legacy))
        self.assertEqual("keyword", search_service._plugin_search_timeout_mode(var_kwargs))

        aware_results = search_service._search_plugin(aware, "关键词", timeout=0.5)
        legacy_results = search_service._search_plugin(legacy, "关键词", timeout=0.5)
        search_service._search_plugin(var_kwargs, "关键词", timeout=0.25)
        self.assertEqual([0.5], aware.received)
        self.assertEqual([0.25], var_kwargs.received)
        self.assertEqual(1, len(aware_results))
        self.assertEqual(1, len(legacy_results), "不接受 timeout 的老插件不能被传参报错")

        search_service._search_plugin(aware, "关键词")
        self.assertEqual([0.5, None], aware.received, "未指定超时时不应传递 timeout 参数")


if __name__ == "__main__":
    unittest.main()
