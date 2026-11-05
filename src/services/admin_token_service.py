"""管理员 JWT 吊销与令牌版本管理。

- revoked_tokens: 记录主动登出的 jti，重启后仍生效；
- admin_token_version: 修改密码时递增，使所有旧令牌立即失效（无需逐条记录）；
- 过期记录由每日日志清理任务顺带清理。
"""

import datetime
import json
import logging
from typing import Optional

from src.db.connection import db_cursor
from src.db.system_configs import get_config_value, set_config_value

logger = logging.getLogger(__name__)

ADMIN_TOKEN_VERSION_KEY = "admin_token_version"


def get_admin_token_version() -> int:
    raw = get_config_value(ADMIN_TOKEN_VERSION_KEY)
    if not raw:
        return 0
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return max(int(data.get("value", 0) or 0), 0)
        return max(int(data), 0)
    except (TypeError, ValueError, json.JSONDecodeError):
        return 0


def bump_admin_token_version() -> int:
    new_version = get_admin_token_version() + 1
    ok = set_config_value(ADMIN_TOKEN_VERSION_KEY, {"value": new_version})
    if not ok:
        logger.error("递增管理员令牌版本失败，旧令牌可能仍然有效")
        return get_admin_token_version()
    logger.info("管理员令牌版本已递增至 %s，旧令牌全部失效", new_version)
    return new_version


def _expires_to_sql(expires_at: Optional[int]) -> str:
    if not expires_at:
        return (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    return datetime.datetime.fromtimestamp(int(expires_at), tz=datetime.timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def revoke_token(jti: str, expires_at: Optional[int]) -> bool:
    if not jti:
        return False
    expires_sql = _expires_to_sql(expires_at)
    with db_cursor() as cursor:
        if cursor is None:
            return False
        cursor.execute(
            "INSERT INTO revoked_tokens (jti, expires_at) VALUES (?, ?) "
            "ON CONFLICT(jti) DO UPDATE SET expires_at = excluded.expires_at",
            (str(jti), expires_sql),
        )
    return True


def is_token_revoked(jti: str) -> bool:
    if not jti:
        return False
    try:
        with db_cursor() as cursor:
            if cursor is None:
                return False
            cursor.execute("SELECT 1 FROM revoked_tokens WHERE jti = ? LIMIT 1", (str(jti),))
            return cursor.fetchone() is not None
    except Exception as err:  # noqa: BLE001 - 查询失败时宁可放行已签名令牌，避免数据库抖动锁死后台
        logger.warning("检查 JWT 吊销状态失败: %s", err)
        return False


def cleanup_expired_revoked_tokens() -> int:
    with db_cursor() as cursor:
        if cursor is None:
            return 0
        cursor.execute("DELETE FROM revoked_tokens WHERE expires_at <= datetime('now')")
        return cursor.rowcount or 0
