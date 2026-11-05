"""SSE 并发闸门：限制单进程同时打开的流式搜索连接数。

waitress 每条长连接占一个工作线程，SSE 连接失控会拖垮普通请求；
该模块提供进程内计数闸门，超限时由路由返回 503（而不是继续占用线程）。
"""
import os
import threading
from typing import Optional

_LOCK = threading.Lock()
_ACTIVE = 0


def max_concurrent_streams() -> int:
    """允许的最大并发 SSE 连接数（SSE_MAX_CONCURRENT，默认 64）。"""
    raw = os.environ.get("SSE_MAX_CONCURRENT", "64").strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 64
    return max(value, 1)


def acquire_stream_slot() -> bool:
    """尝试占用一个流式连接名额，成功返回 True。"""
    global _ACTIVE
    with _LOCK:
        if _ACTIVE >= max_concurrent_streams():
            return False
        _ACTIVE += 1
        return True


def release_stream_slot() -> None:
    """释放流式连接名额（必须与 acquire 成对调用）。"""
    global _ACTIVE
    with _LOCK:
        if _ACTIVE > 0:
            _ACTIVE -= 1


def active_streams() -> int:
    """当前占用中的流式连接数。"""
    with _LOCK:
        return _ACTIVE
