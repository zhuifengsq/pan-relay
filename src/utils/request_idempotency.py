"""请求级幂等：为写接口（如 /api/v1/transfer）提供跨请求结果复用。

调用方传入 Idempotency-Key 时，首次请求结果落库；相同 key 的后续请求
在 TTL 内直接返回首次结果，避免重复转存/重复扣减账号配额。
"""
import json
import time
from typing import Any, Dict, Optional, Tuple

from src.db.connection import get_db_connection

DEFAULT_TTL_SECONDS = 24 * 60 * 60


def _ensure_table(conn) -> None:
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS request_idempotency (
              idem_key TEXT PRIMARY KEY,
              response_json TEXT NOT NULL,
              status_code INTEGER NOT NULL,
              created_at REAL NOT NULL
            )
            """
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_request_idempotency_created ON request_idempotency(created_at)"
        )
        conn.commit()
    finally:
        cursor.close()


def get_cached_response(key: str, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> Optional[Tuple[Dict[str, Any], int]]:
    """读取缓存响应；不存在或已过期返回 None。"""
    if not key:
        return None
    conn = get_db_connection()
    if not conn:
        return None
    try:
        _ensure_table(conn)
        cursor = conn.cursor(as_dict=True)
        try:
            cursor.execute(
                "SELECT response_json, status_code, created_at FROM request_idempotency WHERE idem_key = ?",
                [key],
            )
            row = cursor.fetchone()
            if not row:
                return None
            if time.time() - float(row["created_at"] or 0) > ttl_seconds:
                cursor.execute("DELETE FROM request_idempotency WHERE idem_key = ?", [key])
                conn.commit()
                return None
            payload = json.loads(row["response_json"])
            return payload, int(row["status_code"] or 200)
        finally:
            cursor.close()
    except Exception:  # noqa: BLE001
        return None
    finally:
        conn.close()


def save_response(
    key: str,
    payload: Dict[str, Any],
    status_code: int = 200,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> None:
    """保存首次请求结果（失败仅忽略，不影响主流程）。"""
    if not key:
        return
    conn = get_db_connection()
    if not conn:
        return
    try:
        _ensure_table(conn)
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO request_idempotency (idem_key, response_json, status_code, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(idem_key) DO UPDATE SET
                  response_json = excluded.response_json,
                  status_code = excluded.status_code,
                  created_at = excluded.created_at
                """,
                [key, json.dumps(payload, ensure_ascii=False, default=str), int(status_code), time.time()],
            )
            conn.commit()
        finally:
            cursor.close()
    except Exception:  # noqa: BLE001
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
    finally:
        conn.close()


def cleanup_expired_idempotency(ttl_seconds: int = DEFAULT_TTL_SECONDS) -> int:
    """删除过期幂等记录，返回删除条数。"""
    conn = get_db_connection()
    if not conn:
        return 0
    try:
        _ensure_table(conn)
        cursor = conn.cursor()
        try:
            threshold = time.time() - ttl_seconds
            cursor.execute("DELETE FROM request_idempotency WHERE created_at < ?", [threshold])
            deleted = cursor.rowcount or 0
            conn.commit()
            return deleted
        finally:
            cursor.close()
    except Exception:  # noqa: BLE001
        return 0
    finally:
        conn.close()
