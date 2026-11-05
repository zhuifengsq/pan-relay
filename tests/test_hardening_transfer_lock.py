"""跨进程转存幂等文件锁的加固测试。

覆盖点：
1) normalize_share_url / 锁文件命名稳定性（同 URL 不同写法同 key，不同 URL 不同 key）；
2) 锁文件落在默认的 <repo>/data/locks；
3) 同进程两个线程竞争同一 key：第二个在 timeout 内抛 TransferLockTimeout，释放后可获取；
4) 不同 key 互不阻塞（进程内条带锁 + 文件锁都不互相影响）；
5) contextmanager 在函数体抛异常时也会释放锁；
6) Windows 下真实跨进程文件锁：子进程持锁时父进程等待超时（msvcrt.locking 实测）；
7) create_share 在等待文件锁超时时返回 None（不抛未捕获异常）并打印「同一链接正在转存」告警。

测试隔离：所有真实锁文件都写入 <repo>/.tmp/ 下的临时目录（通过 PAN_TRANSFER_LOCK_DIR 覆盖），
不进行任何真实转存/网络调用；默认目录测试只检查路径与文件名，不删除锁文件（设计上允许残留）。
"""

import hashlib
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import MagicMock, patch

from src.configs.app_config import BASE_DIR
from src.utils.transfer_lock import (
    DEFAULT_LOCK_DIR,
    TransferLockTimeout,
    lock_file_path,
    normalize_share_url,
    transfer_lock,
)

_TMP_ROOT = os.path.join(BASE_DIR, ".tmp")
_PROCESS_LOCK_STRIPES = 64

_CHILD_HOLD_LOCK_SCRIPT = r"""
import sys

from src.utils.transfer_lock import transfer_lock

key = sys.argv[1]
with transfer_lock(key, timeout=10.0):
    print("LOCKED", flush=True)
    # 等待父进程发一行输入后释放
    sys.stdin.readline()
print("RELEASED", flush=True)
"""


def _stripe_index(key):
    return int.from_bytes(hashlib.sha256(key.encode("utf-8")).digest()[:4], "big") % _PROCESS_LOCK_STRIPES


def _key_with_other_stripe(base, forbidden_stripe):
    for index in range(500):
        candidate = "{0}-{1}".format(base, index)
        if _stripe_index(candidate) != forbidden_stripe:
            return candidate
    raise AssertionError("未能构造出不同条带的锁 key")


class _LockDirMixin:
    def setUp(self):
        os.makedirs(_TMP_ROOT, exist_ok=True)
        self.lock_dir = tempfile.mkdtemp(prefix="transfer_lock_", dir=_TMP_ROOT)
        self.addCleanup(shutil.rmtree, self.lock_dir, True)

    def _patch_lock_dir(self):
        patcher = patch.dict(os.environ, {"PAN_TRANSFER_LOCK_DIR": self.lock_dir})
        patcher.start()
        self.addCleanup(patcher.stop)


class TestTransferLockPrimitives(_LockDirMixin, unittest.TestCase):
    def test_normalize_share_url_and_lock_path_stability(self):
        canonical = "https://pan.quark.cn/s/AbC123"
        variants = [
            "  HTTPS://Pan.Quark.CN/s/AbC123/  ",
            "https://pan.quark.cn/s/AbC123/#some-fragment",
        ]
        self.assertEqual(canonical, normalize_share_url(variants[0]))
        self.assertEqual(canonical, normalize_share_url(variants[1]))
        self.assertEqual(canonical, normalize_share_url(canonical))

        self.assertEqual(lock_file_path(canonical), lock_file_path(normalize_share_url(variants[0])))
        self.assertEqual(lock_file_path(canonical), lock_file_path(normalize_share_url(variants[1])))

        other = "https://pan.quark.cn/s/another_share"
        self.assertNotEqual(normalize_share_url(canonical), normalize_share_url(other))
        self.assertNotEqual(os.path.basename(lock_file_path(canonical)), os.path.basename(lock_file_path(other)))
        # 命名规则：transfer-<sha256(key)[:32]>.lock
        expected_digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
        self.assertEqual("transfer-{0}.lock".format(expected_digest), os.path.basename(lock_file_path(canonical)))

        self.assertTrue(lock_file_path(canonical).startswith(DEFAULT_LOCK_DIR))
        self.assertTrue(DEFAULT_LOCK_DIR.replace("\\", "/").endswith("data/locks"))

    def test_lock_file_created_under_lock_dir_and_released(self):
        self._patch_lock_dir()
        key = normalize_share_url("https://pan.quark.cn/s/lock_file_{0}".format(uuid.uuid4().hex))
        path = lock_file_path(key)
        self.assertFalse(os.path.exists(path))

        with transfer_lock(key, timeout=1.0):
            self.assertTrue(os.path.exists(path))
            self.assertGreaterEqual(os.path.getsize(path), 1)

        # 释放后同一 key 可以立刻再次获取（锁文件保留不删除）
        with transfer_lock(key, timeout=1.0):
            self.assertTrue(os.path.exists(path))

    def test_second_thread_times_out_then_acquires_after_release(self):
        self._patch_lock_dir()
        key = normalize_share_url("https://pan.quark.cn/s/thread_race_{0}".format(uuid.uuid4().hex))
        holder_ready = threading.Event()
        release_holder = threading.Event()
        holder_errors = []

        def _hold():
            try:
                with transfer_lock(key, timeout=2.0):
                    holder_ready.set()
                    release_holder.wait(5.0)
            except BaseException as exc:  # pragma: no cover - 仅用于传递线程异常
                holder_errors.append(exc)

        holder = threading.Thread(target=_hold, daemon=True)
        holder.start()
        try:
            self.assertTrue(holder_ready.wait(5.0), "持有锁的线程未能就绪")

            started = time.monotonic()
            with self.assertRaises(TransferLockTimeout):
                with transfer_lock(key, timeout=0.3):
                    self.fail("同一 key 不应同时获取成功")
            elapsed = time.monotonic() - started
            self.assertGreaterEqual(elapsed, 0.25)
            self.assertLess(elapsed, 5.0)
        finally:
            release_holder.set()
            holder.join(5.0)
        self.assertEqual([], holder_errors)

        with transfer_lock(key, timeout=1.0):
            pass

    def test_different_keys_do_not_block(self):
        self._patch_lock_dir()
        key_a = normalize_share_url("https://pan.quark.cn/s/key_a_{0}".format(uuid.uuid4().hex))
        key_b = _key_with_other_stripe(
            normalize_share_url("https://pan.quark.cn/s/key_b_{0}".format(uuid.uuid4().hex)),
            _stripe_index(key_a),
        )
        result = []

        with transfer_lock(key_a, timeout=1.0):
            def _acquire_other():
                try:
                    with transfer_lock(key_b, timeout=1.0):
                        result.append("acquired")
                except BaseException as exc:  # pragma: no cover
                    result.append(exc)

            other = threading.Thread(target=_acquire_other, daemon=True)
            other.start()
            other.join(5.0)
            self.assertFalse(other.is_alive(), "不同 key 的获取不应阻塞")
        self.assertEqual(["acquired"], result)

    def test_lock_released_when_body_raises(self):
        self._patch_lock_dir()
        key = normalize_share_url("https://pan.quark.cn/s/body_raises_{0}".format(uuid.uuid4().hex))
        with self.assertRaises(RuntimeError):
            with transfer_lock(key, timeout=1.0):
                raise RuntimeError("业务异常")
        with transfer_lock(key, timeout=1.0):
            pass

    def test_windows_cross_process_file_lock_blocks(self):
        """真实子进程持有 msvcrt/fcntl 文件锁时，父进程必须等待超时。"""
        self._patch_lock_dir()
        key = normalize_share_url("https://pan.quark.cn/s/xprocess_{0}".format(uuid.uuid4().hex))
        env = dict(os.environ)
        env["PAN_TRANSFER_LOCK_DIR"] = self.lock_dir
        env.setdefault("PYTHONIOENCODING", "utf-8")

        proc = subprocess.Popen(
            [sys.executable, "-c", _CHILD_HOLD_LOCK_SCRIPT, key],
            cwd=BASE_DIR,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        lines = queue.Queue()

        def _reader():
            try:
                for line in proc.stdout:
                    lines.put(line)
            finally:
                lines.put(None)

        reader = threading.Thread(target=_reader, daemon=True)
        reader.start()
        seen = []
        deadline = time.monotonic() + 20.0
        locked = False
        while time.monotonic() < deadline:
            try:
                line = lines.get(timeout=0.1)
            except queue.Empty:
                if proc.poll() is not None:
                    break
                continue
            if line is None:
                break
            seen.append(line)
            if "LOCKED" in line:
                locked = True
                break

        try:
            self.assertTrue(locked, "子进程未能获取文件锁，输出: {0!r}".format("".join(seen)))
            self.assertTrue(os.path.exists(lock_file_path(key)))

            started = time.monotonic()
            with self.assertRaises(TransferLockTimeout):
                with transfer_lock(key, timeout=0.4):
                    self.fail("子进程持锁期间父进程不应获取成功")
            elapsed = time.monotonic() - started
            self.assertGreaterEqual(elapsed, 0.35)
            self.assertLess(elapsed, 10.0)
        finally:
            try:
                proc.stdin.write("\n")
                proc.stdin.flush()
            except OSError:
                pass
            try:
                proc.wait(timeout=10.0)
            except subprocess.TimeoutExpired:  # pragma: no cover
                proc.kill()
                proc.wait(timeout=10.0)
            for stream in (proc.stdin, proc.stdout):
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass

        # 子进程退出后锁被系统释放，父进程应能获取
        with transfer_lock(key, timeout=2.0):
            pass


class TestCreateShareLockTimeout(unittest.TestCase):
    @patch("src.pan_operator._handle_netdisk_operation")
    @patch("src.pan_operator.transfer_lock")
    @patch("src.services.account_pool_manager.AccountPoolManager.get_instance")
    def test_create_share_returns_none_and_warns_when_lock_times_out(
        self, get_instance, transfer_lock_mock, handle_op_mock
    ):
        pool = MagicMock()
        pool.get_candidate_accounts.return_value = [{"id": 11, "account_name": "lock_acc"}]
        pool.select_account_for_transfer.return_value = ({"id": 11, "account_name": "lock_acc"}, MagicMock())
        get_instance.return_value = pool
        transfer_lock_mock.side_effect = TransferLockTimeout("同一链接正在转存")

        from src.pan_operator import create_share

        share_data = {
            "id": 10086,
            "share_url": "https://pan.quark.cn/s/lock_timeout_{0}".format(uuid.uuid4().hex),
            "title": "锁超时测试",
            "save_to_netdisk": {"quark": True},
        }
        with self.assertLogs("src.pan_operator", level="WARNING") as captured:
            result = create_share(share_data)

        self.assertIsNone(result)
        self.assertTrue(any("同一链接正在转存，请稍后重试" in line for line in captured.output))
        # 未执行任何远程转存（账号选择可能在加锁前完成，但远程操作必须没有发生）
        handle_op_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
