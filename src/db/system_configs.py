import json
import logging
import time
from typing import Any, Dict, Optional, Tuple

from src.db import connection as _connection_module
from src.db.connection import Error, get_db_connection
from src.utils.cache_events import notify_search_cache_invalidated

logger = logging.getLogger(__name__)

# 系统配置是典型的“读多写少”数据：首页渲染一次要读取几十个配置项。
# 这里在进程内缓存原始 JSON 字符串，后台保存/删除配置时即时失效；
# 多进程部署时其他进程最多滞后 _CONFIG_CACHE_TTL 秒。
_CONFIG_CACHE_TTL = 60.0
# 缓存 key 带上数据库路径，避免测试切换 SQLITE_DB_PATH 时串用其他库的配置值
_config_cache: Dict[Tuple[str, str], Tuple[Optional[str], float]] = {}


def _current_db_path() -> str:
    """读取当前生效的数据库路径（运行时可能被测试或脚本替换）。"""
    return str(getattr(_connection_module, "SQLITE_DB_PATH", "") or "")


def _invalidate_config_cache(config_key: Optional[str] = None) -> None:
    """使某个配置项（或全部）的进程内缓存失效。"""
    if config_key is None:
        _config_cache.clear()
        return
    for cache_key in [key for key in _config_cache if key[1] == config_key]:
        _config_cache.pop(cache_key, None)


def clear_config_cache() -> None:
    """清空进程内配置缓存（供脚本/测试在直接写库后手动刷新）。"""
    _invalidate_config_cache()


def _read_config_value(config_key: str) -> Tuple[bool, Optional[str]]:
    """从数据库读取配置原文，返回 (是否成功, 值)。"""
    conn = get_db_connection()
    if not conn:
        return False, None

    cursor = None
    try:
        cursor = conn.cursor(as_dict=True)
        cursor.execute(
            "SELECT config_value FROM system_config WHERE config_key = ?",
            (config_key,),
        )
        row = cursor.fetchone()
        return True, (row["config_value"] if row else None)
    except Error as err:
        logger.error(f"读取系统配置 {config_key} 失败: {err}")
        return False, None
    finally:
        if cursor is not None:
            cursor.close()
        conn.close()


def get_config_value(config_key: str) -> Optional[str]:
    db_path = _current_db_path()
    cache_key = (db_path, config_key)
    now = time.monotonic()
    cached = _config_cache.get(cache_key)
    if cached is not None and now - cached[1] < _CONFIG_CACHE_TTL:
        return cached[0]

    ok, value = _read_config_value(config_key)
    if not ok:
        # 数据库异常时不缓存结果，避免故障期间持续返回过期值
        return None
    _config_cache[cache_key] = (value, now)
    return value


def set_config_value(config_key: str, config_value: Dict[str, Any]) -> bool:
    conn = get_db_connection()
    if not conn:
        return False

    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO system_config (config_key, config_value)
            VALUES (?, ?)
            ON CONFLICT(config_key) DO UPDATE SET config_value = excluded.config_value, updated_at = datetime('now')
            """,
            (config_key, json.dumps(config_value, ensure_ascii=True)),
        )
        conn.commit()
        _invalidate_config_cache(config_key)
        notify_search_cache_invalidated(f"system_config:{config_key}")
        return True
    except Error as err:
        logger.error(f"保存系统配置 {config_key} 失败: {err}")
        conn.rollback()
        return False
    finally:
        cursor.close()
        conn.close()


def delete_config_value(config_key: str) -> bool:
    conn = get_db_connection()
    if not conn:
        return False

    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM system_config WHERE config_key = ?", (config_key,))
        conn.commit()
        _invalidate_config_cache(config_key)
        notify_search_cache_invalidated(f"system_config-delete:{config_key}")
        return True
    except Error as err:
        logger.error(f"删除系统配置 {config_key} 失败: {err}")
        conn.rollback()
        return False
    finally:
        cursor.close()
        conn.close()
