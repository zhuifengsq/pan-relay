"""管理员登录失败限速与临时锁定（进程内实现）。

默认策略：同一 IP 在 300 秒内失败 5 次后锁定 900 秒。
可通过 LOGIN_MAX_ATTEMPTS / LOGIN_WINDOW_SECONDS / LOGIN_LOCKOUT_SECONDS 调整。
多进程部署时每个进程独立计数，需在反向代理层或共享存储做全局限流（文档已说明）。
"""

import logging
import math
import os
import threading
import time
from typing import Dict, Tuple

logger = logging.getLogger(__name__)

_STATE_LOCK = threading.Lock()
# ip -> {"failures": [timestamp, ...], "locked_until": monotonic}
_STATE: Dict[str, Dict[str, object]] = {}


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(int(str(os.environ.get(name, "") or default).strip()), minimum)
    except (TypeError, ValueError):
        return default


def _max_attempts() -> int:
    return _env_int("LOGIN_MAX_ATTEMPTS", 5)


def _window_seconds() -> int:
    return _env_int("LOGIN_WINDOW_SECONDS", 300)


def _lockout_seconds() -> int:
    return _env_int("LOGIN_LOCKOUT_SECONDS", 900)


def _prune(state: Dict[str, object], now: float) -> None:
    failures = [ts for ts in state.get("failures", []) if now - ts < _window_seconds()]
    state["failures"] = failures


def check_login_allowed(client_ip: str, now: float = None) -> Tuple[bool, int]:
    """返回 (是否允许登录, 需要等待的秒数)。"""
    ip = str(client_ip or "unknown")
    current = now if now is not None else time.monotonic()
    with _STATE_LOCK:
        state = _STATE.get(ip)
        if not state:
            return True, 0
        locked_until = float(state.get("locked_until", 0.0) or 0.0)
        if locked_until > current:
            return False, max(1, int(math.ceil(locked_until - current)))
        if locked_until:
            state["locked_until"] = 0.0
        _prune(state, current)
        if len(state["failures"]) >= _max_attempts():
            state["locked_until"] = current + _lockout_seconds()
            return False, _lockout_seconds()
        return True, 0


def record_login_failure(client_ip: str, now: float = None) -> None:
    ip = str(client_ip or "unknown")
    current = now if now is not None else time.monotonic()
    with _STATE_LOCK:
        state = _STATE.setdefault(ip, {"failures": [], "locked_until": 0.0})
        _prune(state, current)
        state["failures"].append(current)
        if len(state["failures"]) >= _max_attempts():
            state["locked_until"] = current + _lockout_seconds()
            logger.warning("登录失败次数过多，已临时锁定 IP %s（%s 秒）", ip, _lockout_seconds())


def record_login_success(client_ip: str) -> None:
    ip = str(client_ip or "unknown")
    with _STATE_LOCK:
        _STATE.pop(ip, None)


def reset_login_throttle() -> None:
    """清空全部登录限速状态（测试/运维使用）。"""
    with _STATE_LOCK:
        _STATE.clear()
