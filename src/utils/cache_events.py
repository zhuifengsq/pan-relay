"""进程内缓存失效事件（避免 db 与 service 相互导入形成环）。

搜索缓存依赖数据库配置（API 源、TG 频道、调度参数等）。这些配置在后台
保存后需要立即失效搜索缓存，但写库函数位于 src/db/，而缓存实现位于
src/services/search_service.py。这里提供一个极简的发布/订阅点：
search_service 启动时注册清理函数，配置写入成功后广播事件。
"""

import logging
from typing import Callable, List

logger = logging.getLogger(__name__)

_search_cache_invalidators: List[Callable[[], None]] = []


def register_search_cache_invalidator(callback: Callable[[], None]) -> None:
    """注册一个搜索缓存失效回调（重复注册会被忽略）。"""
    if callback in _search_cache_invalidators:
        return
    _search_cache_invalidators.append(callback)


def notify_search_cache_invalidated(reason: str = "") -> None:
    """通知所有订阅者清理搜索缓存；单个回调失败不影响其他回调。"""
    for callback in list(_search_cache_invalidators):
        try:
            callback()
        except Exception as err:  # pragma: no cover - 防御性分支
            logger.warning("清理搜索缓存失败(reason=%s): %s", reason, err)
