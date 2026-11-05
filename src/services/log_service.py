import atexit
import csv
import io
import logging
import queue
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from src.db.logs import (
    cleanup_logs_before_days,
    clear_all_logs as db_clear_all_logs,
    delete_logs_by_filter,
    delete_logs_by_ids,
    get_logs_summary_stats,
    get_search_analytics_stats,
    insert_system_log,
    insert_system_logs_batch,
    query_logs_for_export,
    query_search_logs,
    query_system_logs,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 异步日志写入
# 请求线程只负责入队，由单个后台守护线程串行落库，避免每条业务日志的
# INSERT + commit 阻塞接口响应；队列有界，积压时回退为同步写入以免丢日志。
# ---------------------------------------------------------------------------
_LOG_QUEUE_MAXSIZE = 10000
_LOG_QUEUE: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=_LOG_QUEUE_MAXSIZE)
_LOG_WRITER_THREAD: Optional[threading.Thread] = None
_LOG_WRITER_LOCK = threading.Lock()
# 单次最多批量落库的日志条数：积压越多收益越大，同时避免长时间占用写连接
_LOG_WRITE_BATCH_MAX = 200


def _log_writer_loop() -> None:
    while True:
        batch = [_LOG_QUEUE.get()]
        try:
            # 非阻塞排空当前积压，合并为一次 executemany + commit
            while len(batch) < _LOG_WRITE_BATCH_MAX:
                try:
                    batch.append(_LOG_QUEUE.get_nowait())
                except queue.Empty:
                    break

            written = insert_system_logs_batch(batch)
            if written == 0:
                # 批量写入失败时逐条回退，尽量保留日志（例如个别记录触发约束错误）
                for payload in batch:
                    try:
                        insert_system_log(**payload)
                    except Exception as err:  # noqa: BLE001 - 后台线程必须吞掉所有异常
                        logger.warning(f"异步写入系统日志失败: {err}")
        except Exception as err:  # noqa: BLE001 - 后台线程必须吞掉所有异常
            logger.warning(f"异步批量写入系统日志失败: {err}")
        finally:
            for _ in batch:
                _LOG_QUEUE.task_done()


def _ensure_log_writer() -> None:
    global _LOG_WRITER_THREAD
    if _LOG_WRITER_THREAD is not None and _LOG_WRITER_THREAD.is_alive():
        return
    with _LOG_WRITER_LOCK:
        if _LOG_WRITER_THREAD is not None and _LOG_WRITER_THREAD.is_alive():
            return
        thread = threading.Thread(target=_log_writer_loop, name="log-writer", daemon=True)
        thread.start()
        _LOG_WRITER_THREAD = thread


def flush_log_queue(timeout: float = 3.0) -> bool:
    """等待队列中已入队的日志全部落库；返回是否在超时前写完。"""
    deadline = time.monotonic() + max(timeout, 0.0)
    while _LOG_QUEUE.unfinished_tasks > 0:
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.002)
    return True


def _flush_logs_before_read() -> None:
    """后台查询/导出日志前先冲刷队列，保证读到的数据是最新的。"""
    try:
        flush_log_queue()
    except Exception as err:  # noqa: BLE001 - 冲刷失败不影响读取
        logger.warning(f"冲刷日志队列失败: {err}")


atexit.register(lambda: flush_log_queue(5.0))


def get_current_client_ip() -> str:
    """
    从 Flask 请求上下文中获取客户端 IP 地址。

    只有 TRUSTED_PROXIES 配置的受信代理才会解析 X-Forwarded-For / X-Real-IP /
    CF-Connecting-IP / True-Client-IP，否则一律返回直连对端地址，防止伪造请求头
    绕过限流、黑名单与审计日志。
    """
    from src.utils.request_utils import get_client_ip

    return get_client_ip()


def record_log(
    log_type: str,
    action: str,
    query_text: Optional[str] = None,
    status_code: int = 200,
    error_message: Optional[str] = None,
    duration_ms: int = 0,
    result_count: int = 0,
    client_ip: Optional[str] = None,
) -> Optional[int]:
    """
    安全记录一条系统运行/审计日志。
    具备全局异常捕获，即使数据库瞬时报错也绝不影响核心业务运行。
    """
    try:
        if not client_ip:
            client_ip = get_current_client_ip()
        _ensure_log_writer()
        payload = {
            "log_type": log_type,
            "action": action,
            "query_text": query_text,
            "status_code": status_code,
            "error_message": error_message,
            "duration_ms": duration_ms,
            "result_count": result_count,
            "client_ip": client_ip,
        }
        try:
            _LOG_QUEUE.put_nowait(payload)
        except queue.Full:
            # 队列积压（极端流量 / 数据库变慢）时回退同步写，优先保证日志不丢
            logger.warning("日志队列已满，改为同步写入系统日志")
            return insert_system_log(**payload)
        return None
    except Exception as err:
        logger.warning(f"静默记录日志时发生异常: {err}")
        return None


def list_logs(
    page: int = 1,
    page_size: int = 15,
    q: str = "",
    log_type: str = "",
    status: str = "",
    start_date: str = "",
    end_date: str = "",
    sort_by: str = "created_at",
    order: str = "desc",
) -> Tuple[bool, str, Dict[str, Any]]:
    """
    分页查询系统日志
    """
    _flush_logs_before_read()
    return query_system_logs(
        page=page,
        page_size=page_size,
        q=q,
        log_type=log_type,
        status=status,
        start_date=start_date,
        end_date=end_date,
        sort_by=sort_by,
        order=order,
    )


def delete_logs(ids: List[int]) -> Tuple[bool, str, int]:
    """
    批量删除日志
    """
    return delete_logs_by_ids(ids)


def delete_logs_with_filter(
    ids: Optional[List[int]] = None,
    q: str = "",
    log_type: str = "",
    status: str = "",
    channel: str = "",
    result_filter: str = "",
    start_date: str = "",
    end_date: str = "",
) -> Tuple[bool, str, int]:
    """
    按过滤条件或 ID 列表批量删除日志
    """
    return delete_logs_by_filter(
        ids=ids,
        q=q,
        log_type=log_type,
        status=status,
        channel=channel,
        result_filter=result_filter,
        start_date=start_date,
        end_date=end_date,
    )


def clear_all_logs() -> Tuple[bool, str, int]:
    """
    清空全部日志
    """
    return db_clear_all_logs()


def cleanup_old_logs(days: int = 7) -> Tuple[bool, str, int]:
    """
    清理指定天数之前的旧日志
    """
    return cleanup_logs_before_days(days)


def get_logs_summary() -> Dict[str, Any]:
    """
    获取后台日志 KPI 统计指标
    """
    _flush_logs_before_read()
    return get_logs_summary_stats()


def export_logs_csv(
    ids: Optional[List[int]] = None,
    q: str = "",
    log_type: str = "",
    status: str = "",
    channel: str = "",
    result_filter: str = "",
    start_date: str = "",
    end_date: str = "",
) -> str:
    """
    导出系统日志为带 UTF-8 BOM 的 CSV 文本
    """
    _flush_logs_before_read()
    logs = query_logs_for_export(
        ids=ids,
        q=q,
        log_type=log_type,
        status=status,
        channel=channel,
        result_filter=result_filter,
        start_date=start_date,
        end_date=end_date,
    )

    output = io.StringIO()
    # 写入 UTF-8 BOM 便于 Excel 正常打开中文无乱码
    output.write("\ufeff")
    writer = csv.writer(output)
    writer.writerow([
        "日志ID",
        "时间",
        "类型",
        "动作/接口",
        "搜索词/请求内容",
        "状态码",
        "耗时(ms)",
        "命中数量",
        "客户端IP",
        "异常/错误信息",
    ])

    for row in logs:
        writer.writerow([
            row.get("id"),
            row.get("created_at"),
            row.get("log_type"),
            row.get("action"),
            row.get("query_text") or "",
            row.get("status_code"),
            row.get("duration_ms"),
            row.get("result_count"),
            row.get("client_ip") or "",
            row.get("error_message") or "",
        ])

    return output.getvalue()


def list_search_logs(
    page: int = 1,
    page_size: int = 15,
    q: str = "",
    channel: str = "",
    result_filter: str = "",
    start_date: str = "",
    end_date: str = "",
    sort_by: str = "created_at",
    order: str = "desc",
) -> Tuple[bool, str, Dict[str, Any]]:
    """
    分页查询搜索业务日志（Web / API）
    """
    _flush_logs_before_read()
    return query_search_logs(
        page=page,
        page_size=page_size,
        q=q,
        channel=channel,
        result_filter=result_filter,
        start_date=start_date,
        end_date=end_date,
        sort_by=sort_by,
        order=order,
    )


def get_search_analytics() -> Dict[str, Any]:
    """
    获取搜索业务分析指标与热词榜
    """
    _flush_logs_before_read()
    return get_search_analytics_stats()
