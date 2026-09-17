"""
跨平台文件锁（防止多个进程同时写同一 CSV 导致数据损坏）

用法：
    from data.file_lock import file_lock
    with file_lock(csv_path):
        # 读 → 合并 → 写

Windows 用 msvcrt.locking（文件锁），其他平台用 fcntl.flock。
锁文件为 <目标文件>.lock，持锁期间阻塞其他进程。
"""

import logging
import os
import sys
import time
from pathlib import Path
from typing import Union

logger = logging.getLogger(__name__)

# 锁超时（秒）：拿不到锁就放弃，避免死等
LOCK_TIMEOUT = 30


class _LockBase:
    def __init__(self, target: Path, timeout: float = LOCK_TIMEOUT):
        self.target = Path(target)
        self.lock_file = self.target.with_suffix(self.target.suffix + ".lock")
        self.timeout = timeout
        self._fh = None

    def __enter__(self):
        self._acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._release()
        return False

    def _acquire(self):
        raise NotImplementedError

    def _release(self):
        raise NotImplementedError


class _WindowsLock(_LockBase):
    """Windows：msvcrt.locking 对锁文件加独占锁"""

    def _acquire(self):
        import msvcrt

        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.lock_file, "a+")
        start = time.time()
        while True:
            try:
                # 锁定整个文件（1 字节即可，独占）
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.time() - start > self.timeout:
                    self._fh.close()
                    self._fh = None
                    raise TimeoutError(f"获取文件锁超时: {self.lock_file}")
                time.sleep(0.2)

    def _release(self):
        import msvcrt

        if self._fh:
            try:
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            self._fh.close()
            self._fh = None


class _PosixLock(_LockBase):
    """Linux/macOS：fcntl.flock 独占锁"""

    def _acquire(self):
        import fcntl

        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.lock_file, "a+")
        start = time.time()
        while True:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.time() - start > self.timeout:
                    self._fh.close()
                    self._fh = None
                    raise TimeoutError(f"获取文件锁超时: {self.lock_file}")
                time.sleep(0.2)

    def _release(self):
        import fcntl

        if self._fh:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            self._fh.close()
            self._fh = None


def file_lock(target: Union[str, Path], timeout: float = LOCK_TIMEOUT):
    """获取目标文件的跨进程锁（上下文管理器）"""
    if sys.platform == "win32":
        return _WindowsLock(Path(target), timeout)
    return _PosixLock(Path(target), timeout)
