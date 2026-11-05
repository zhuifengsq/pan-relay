"""跨进程转存幂等文件锁。

背景：src/pan_operator.py::create_share 原本只有进程内 64 条带锁 _get_transfer_lock(url)，
多 worker / 多容器部署时进程内锁互相不可见，同一分享链接仍可能被重复远程转存。
本模块用操作系统文件锁（Windows msvcrt.locking / POSIX fcntl.flock）提供跨进程互斥。

设计要点：
- 锁粒度：规范化后的分享链接（normalize_share_url）-> sha256 前 32 位十六进制 -> 锁文件名，
  同一链接的大小写/尾部斜杠/空白差异会落到同一把锁，不同链接互不影响。
- 锁文件固定位于 data/locks/transfer-<sha256(key)[:32]>.lock；目录按需创建。
- 锁文件不删除（避免 unlink 与加锁之间的竞争），允许残留 1 字节空文件。
- 固定加锁顺序：调用方先拿进程内锁，再拿本跨进程文件锁（create_share 已按此顺序），
  全仓不要反向获取，避免死锁。
- 同进程内再按 key 分 64 条带 RLock 兜底（RLock 便于同一线程对不同 key 的嵌套获取），
  跨线程仍然互斥，跨进程由文件锁保证。
- 超时抛 TransferLockTimeout。它故意继承普通 Exception 而不是 TimeoutError，
  避免被转存链路里的 _AMBIGUOUS_STORE_EXCEPTIONS 误判为“远程结果不确定”。
- 不新增第三方依赖，仅使用标准库。
"""

import hashlib
import logging
import os
import threading
import time
from contextlib import contextmanager
from typing import Iterator, List, Optional
from urllib.parse import urlsplit, urlunsplit

from src.configs.app_config import BASE_DIR

logger = logging.getLogger(__name__)

# 锁文件根目录：<repo>/data/locks（可用 PAN_TRANSFER_LOCK_DIR 覆盖，便于容器挂载/测试隔离）
DEFAULT_LOCK_DIR = os.path.join(BASE_DIR, "data", "locks")
# 与任务约定一致的默认等待上限（秒）
DEFAULT_TIMEOUT = 10.0
# 文件锁轮询间隔（秒）
LOCK_POLL_INTERVAL = 0.05

# 进程内条带锁数量：固定条带，避免按 key 建锁导致字典无界增长
_PROCESS_LOCK_STRIPES = 64
_PROCESS_LOCKS: List[threading.RLock] = [
    threading.RLock() for _ in range(_PROCESS_LOCK_STRIPES)
]


class TransferLockTimeout(Exception):
    """获取转存跨进程文件锁超时（同一链接正在被其他 worker/进程转存）。"""


def normalize_share_url(url: str) -> str:
    """规范化分享链接作为锁 key：去空白、scheme/host 小写、去尾部斜杠。

    保留 path 大小写与 query 原始顺序；分享链接的 fragment 不影响资源，直接丢弃。
    非标准 URL（无 scheme/netloc）退化为仅去空白与尾部斜杠。
    """
    clean = str(url or "").strip()
    if not clean:
        return ""
    try:
        parts = urlsplit(clean)
    except ValueError:
        return clean.rstrip("/")
    if not parts.scheme or not parts.netloc:
        return clean.rstrip("/")
    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path.rstrip("/"),
            parts.query,
            "",
        )
    )


def _get_lock_dir() -> str:
    override = os.environ.get("PAN_TRANSFER_LOCK_DIR", "").strip()
    if override:
        return os.path.abspath(override)
    return DEFAULT_LOCK_DIR


def lock_file_path(key: str, lock_dir: Optional[str] = None) -> str:
    """返回 key 对应的锁文件绝对路径（不创建文件）。"""
    digest = hashlib.sha256(str(key or "").encode("utf-8")).hexdigest()[:32]
    return os.path.join(lock_dir or _get_lock_dir(), "transfer-{0}.lock".format(digest))


def _stripe_index(key: str) -> int:
    digest = hashlib.sha256(str(key or "").encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % _PROCESS_LOCK_STRIPES


def _open_lock_handle(path: str):
    """打开（必要时创建）锁文件，保证文件至少 1 字节，定位到偏移 0 以便加锁。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    handle = open(path, "a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() < 1:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        return handle
    except BaseException:
        try:
            handle.close()
        except OSError:
            pass
        raise


def _try_lock_handle(handle) -> bool:
    """非阻塞尝试获取文件锁：成功返回 True，被其他句柄占用返回 False。"""
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _release_lock_handle(handle) -> None:
    """释放文件锁并关闭句柄；即使解锁失败，关闭句柄也会释放操作系统锁。"""
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        logger.warning("释放跨进程转存文件锁失败（关闭句柄后操作系统会自动释放）", exc_info=True)
    finally:
        try:
            handle.close()
        except OSError:
            pass


@contextmanager
def transfer_lock(key: str, timeout: float = DEFAULT_TIMEOUT) -> Iterator[str]:
    """跨进程转存锁上下文管理器。

    :param key: 锁 key，通常是 normalize_share_url(share_url) 的结果
    :param timeout: 最长等待秒数（进程内锁 + 文件锁共用同一 deadline），超时抛 TransferLockTimeout
    """
    clean_key = str(key or "").strip()
    try:
        wait_seconds = max(0.0, float(timeout))
    except (TypeError, ValueError):
        wait_seconds = DEFAULT_TIMEOUT
    deadline = time.monotonic() + wait_seconds
    path = lock_file_path(clean_key)
    stripe_lock = _PROCESS_LOCKS[_stripe_index(clean_key)]

    if not stripe_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
        raise TransferLockTimeout(
            "同一链接正在转存，获取进程内锁超时({0}s): {1}".format(wait_seconds, clean_key)
        )

    handle = None
    try:
        handle = _open_lock_handle(path)
        while True:
            if _try_lock_handle(handle):
                break
            if time.monotonic() >= deadline:
                raise TransferLockTimeout(
                    "同一链接正在转存，获取跨进程文件锁超时({0}s): {1} ({2})".format(
                        wait_seconds, clean_key, path
                    )
                )
            time.sleep(LOCK_POLL_INTERVAL)
        try:
            yield clean_key
        finally:
            _release_lock_handle(handle)
            handle = None
    finally:
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
        stripe_lock.release()


__all__ = [
    "DEFAULT_LOCK_DIR",
    "DEFAULT_TIMEOUT",
    "TransferLockTimeout",
    "lock_file_path",
    "normalize_share_url",
    "transfer_lock",
]
