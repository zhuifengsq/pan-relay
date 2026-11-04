import atexit
import logging
import os

from apscheduler.schedulers.background import BackgroundScheduler

from src.services.resource_health_service import scheduled_resource_health_audit_job
from src.services.storage_cleanup_service import cleanup_all_storage
from src.services.system_config_service import get_storage_cleanup_config
from src.services.temp_share_service import cleanup_expired_temp_shares

logger = logging.getLogger(__name__)

_scheduler = None
# 跨进程锁文件句柄：多进程/多实例部署时，只有抢到锁的进程执行定时任务。
_SCHEDULER_LOCK_FD = None


def _scheduler_enabled() -> bool:
    return str(os.environ.get("SCHEDULER_ENABLED", "1")).strip().lower() not in {"0", "false", "no", "off"}


def _scheduler_lock_path() -> str:
    from src.configs.app_config import SQLITE_DB_PATH

    db_path = str(SQLITE_DB_PATH or "")
    directory = os.path.dirname(db_path) or "."
    return os.path.join(directory, "scheduler.lock")


def _release_scheduler_lock():
    """释放定时任务跨进程锁（进程退出时由 atexit 调用）。"""
    global _SCHEDULER_LOCK_FD
    fd = _SCHEDULER_LOCK_FD
    _SCHEDULER_LOCK_FD = None
    if fd is None:
        return
    try:
        fd.close()
    except Exception:
        pass


def _try_acquire_scheduler_lock() -> bool:
    """非阻塞获取跨进程文件锁；用于避免多 worker 重复执行定时任务。"""
    global _SCHEDULER_LOCK_FD
    if _SCHEDULER_LOCK_FD is not None:
        return True

    lock_path = _scheduler_lock_path()
    try:
        os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
        fd = open(lock_path, "a+")
    except OSError as err:
        logger.warning(f"无法打开定时任务锁文件 {lock_path}: {err}")
        return False

    try:
        if os.name == "nt":
            import msvcrt

            fd.seek(0, os.SEEK_END)
            if fd.tell() == 0:
                fd.write("0")
                fd.flush()
            fd.seek(0)
            msvcrt.locking(fd.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fd.close()
        return False

    _SCHEDULER_LOCK_FD = fd
    atexit.register(_release_scheduler_lock)
    logger.info(f"已获取定时任务跨进程锁: {lock_path}")
    return True


def _get_cleanup_trigger_kwargs(config: dict) -> tuple:
    unit = str(config.get("cleanup_interval_unit", "hours")).strip().lower()
    val = config.get("cleanup_interval_value")
    if val is None:
        val = config.get("auto_cleanup_interval_hours", 12)
        unit = "hours"

    if unit == "minutes":
        mins = max(15, int(val))
        return {"minutes": mins}, f"每 {mins} 分钟"
    elif unit == "days":
        days = max(1, int(val))
        return {"days": days}, f"每 {days} 天"
    else:
        hours = max(1, int(val))
        return {"hours": hours}, f"每 {hours} 小时"


def reload_storage_cleanup_job():
    """重新载入存储清理定时任务"""
    global _scheduler
    if not _scheduler:
        return
    try:
        config = get_storage_cleanup_config()
        trigger_kwargs, desc = _get_cleanup_trigger_kwargs(config)
        _scheduler.add_job(
            cleanup_all_storage,
            trigger="interval",
            **trigger_kwargs,
            id="cleanup_all_storage",
            max_instances=1,
            replace_existing=True,
        )
        logger.info(f"已更新存储定时清理任务: {desc}执行一次")
    except Exception as exc:
        logger.error(f"重载存储清理定时任务异常: {exc}")


def start_scheduler():
    global _scheduler

    if _scheduler is not None:
        return _scheduler

    if os.environ.get("WERKZEUG_RUN_MAIN") == "false":
        return None

    if not _scheduler_enabled():
        logger.info("SCHEDULER_ENABLED 已关闭，跳过启动定时任务。")
        return None

    if not _try_acquire_scheduler_lock():
        logger.warning("定时任务锁已被其他进程持有（多进程部署时仅一个进程执行定时任务），当前进程跳过启动。")
        return None

    scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    scheduler.add_job(
        cleanup_expired_temp_shares,
        trigger="interval",
        minutes=30,
        id="cleanup_expired_temp_shares",
        max_instances=1,
        replace_existing=True,
    )

    config = get_storage_cleanup_config()
    trigger_kwargs, desc = _get_cleanup_trigger_kwargs(config)
    scheduler.add_job(
        cleanup_all_storage,
        trigger="interval",
        **trigger_kwargs,
        id="cleanup_all_storage",
        max_instances=1,
        replace_existing=True,
    )

    # 每日自动执行一次入库资源全盘巡检与测活更新
    scheduler.add_job(
        scheduled_resource_health_audit_job,
        trigger="interval",
        hours=24,
        id="audit_resources_health",
        max_instances=1,
        replace_existing=True,
    )

    # 每日自动执行一次多网盘账号池凭证保活与续期任务
    scheduler.add_job(
        scheduled_account_keepalive_job,
        trigger="interval",
        hours=24,
        id="keepalive_cloud_accounts",
        max_instances=1,
        replace_existing=True,
    )

    # 每日凌晨执行一次系统日志保留策略清理（LOG_RETENTION_DAYS 环境变量，默认保留 30 天）
    scheduler.add_job(
        scheduled_log_cleanup_job,
        trigger="cron",
        hour=3,
        minute=30,
        id="cleanup_old_logs",
        max_instances=1,
        replace_existing=True,
    )

    scheduler.start()
    logger.info(f"定时任务已启动: 每30分钟清理过期动态分享, {desc}清理存储, 每24小时执行资源健康巡检与多账号保活, 每日03:30清理过期日志")
    _scheduler = scheduler
    return _scheduler


def scheduled_log_cleanup_job():
    """每日按保留期清理系统日志，避免库体积与查询开销无限增长"""
    try:
        from src.services.log_service import cleanup_old_logs

        try:
            retention_days = max(1, int(os.environ.get("LOG_RETENTION_DAYS", "30")))
        except (TypeError, ValueError):
            retention_days = 30
        success, message, count = cleanup_old_logs(days=retention_days)
        if success:
            logger.info(f"系统日志保留策略清理完成（保留 {retention_days} 天）: 清理 {count} 条")
        else:
            logger.warning(f"系统日志保留策略清理失败: {message}")
    except Exception as exc:
        logger.error(f"系统日志保留策略清理异常: {exc}")


def scheduled_account_keepalive_job():
    """每日对多账号池中依赖 Token 自动轮转的账号执行保活与自检"""
    try:
        from src.services.account_pool_manager import AccountPoolManager
        res = AccountPoolManager.get_instance().keepalive_all_accounts()
        logger.info(f"多网盘账号定时保活完成: {res}")
    except Exception as exc:
        logger.error(f"多网盘账号定时保活异常: {exc}")

