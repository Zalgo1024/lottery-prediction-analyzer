"""
分级日志系统（增强版）

特性：
- INFO / WARN / ERROR / DEBUG 全分级
- 控制台彩色输出（ANSI）
- 文件日志自动轮转（10MB，保留30天）
- 计时器装饰器 @timed()
- 模块级别日志控制
- 日志查看命令（展示最近日志摘要）
"""

import logging
import os
import sys
import time
import functools
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from config import LOGS_DIR, LOG_LEVEL, LOG_FORMAT

# ANSI 颜色代码
_COLORS = {
    "DEBUG": "\033[36m",      # Cyan
    "INFO": "\033[32m",       # Green
    "WARNING": "\033[33m",    # Yellow
    "ERROR": "\033[31m",      # Red
    "CRITICAL": "\033[41m",   # Red background
    "RESET": "\033[0m",       # Reset
    "BOLD": "\033[1m",        # Bold
    "DIM": "\033[2m",         # Dim
}

# 模块级别日志覆盖
_MODULE_LEVELS = {}  # {module_name: log_level_int}


def set_module_level(module_name: str, level: str):
    """设置特定模块的日志级别"""
    level_num = getattr(logging, level.upper(), None)
    if level_num is None:
        raise ValueError(f"Invalid log level: {level}")
    _MODULE_LEVELS[module_name] = level_num
    # 如果 logger 已存在，立即调整
    logger = logging.getLogger(module_name)
    logger.setLevel(level_num)


class ColoredFormatter(logging.Formatter):
    """带颜色的日志格式器（控制台用）"""

    def __init__(self, fmt: str, use_color: bool = True):
        super().__init__(fmt)
        self.use_color = use_color

    def format(self, record):
        if self.use_color and sys.stderr.isatty():
            level_name = record.levelname
            color = _COLORS.get(level_name, _COLORS["RESET"])
            reset = _COLORS["RESET"]
            bold = _COLORS["BOLD"]

            # 给 levelname 上色
            record.levelname = f"{color}{bold}{level_name}{reset}"

            # 给 module:line 上色（淡色）
            if hasattr(record, "funcName"):
                module_info = f"{_COLORS['DIM']}{record.name}:{record.lineno}{reset}"
            else:
                module_info = record.name
            record.name = module_info

        return super().format(record)


def setup_logger(name: str = "lottery") -> logging.Logger:
    """
    初始化日志系统
    同时输出到文件（logs/）和控制台（带颜色）

    日志分级：
    - DEBUG: 详细调试信息
    - INFO: 正常流程记录（数据加载、训练开始/结束）
    - WARNING: 数据问题（编码告警、缓存失效）
    - ERROR: 异常终止（文件损坏、计算失败）
    - CRITICAL: 严重错误
    """
    logger = logging.getLogger(name)

    # 检查模块级别覆盖
    module_level = _MODULE_LEVELS.get(name)
    default_level = getattr(logging, LOG_LEVEL.upper(), logging.INFO)
    logger.setLevel(module_level if module_level else default_level)

    # 避免重复添加 handler
    if logger.handlers:
        return logger

    # ----- 1. 文件日志（无颜色，记录所有级别） -----
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    # 测试运行时写独立文件：pytest 会驱动 worker / scheduler 的假事件
    # （如「事件触发 双色球 期号 500->501」），若混进每日生产日志会干扰排查。
    if os.environ.get("PYTEST_CURRENT_TEST") or "pytest" in sys.modules:
        log_file = LOGS_DIR / "pytest.log"
    else:
        log_file = LOGS_DIR / f"{datetime.now().strftime('%Y%m%d')}.log"
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=30,
        encoding="utf-8",
    )
    file_formatter = logging.Formatter(LOG_FORMAT)
    file_handler.setFormatter(file_formatter)
    file_handler.setLevel(logging.DEBUG)
    logger.addHandler(file_handler)

    # ----- 2. 控制台日志（彩色，过滤级别） -----
    console_handler = logging.StreamHandler(sys.stdout)
    console_formatter = ColoredFormatter(LOG_FORMAT)
    console_handler.setFormatter(console_formatter)
    console_handler.setLevel(module_level if module_level else default_level)
    logger.addHandler(console_handler)

    return logger


def get_logger(name: str = "lottery") -> logging.Logger:
    """获取已初始化的 logger"""
    logger = logging.getLogger(name)
    if not logger.handlers:
        return setup_logger(name)
    return logger


# ============================================================
# 计时器装饰器 @timed()
# ============================================================

def timed(logger_name: Optional[str] = None):
    """
    函数执行时间计时器装饰器

    用法：
        @timed()
        def my_func():
            ...

        @timed("train.engine")
        def train():
            ...
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            log = get_logger(logger_name or func.__module__)
            func_name = func.__qualname__ or func.__name__
            log.debug(f"[TIMER] {func_name} started...")
            start = time.perf_counter()
            try:
                result = func(*args, **kwargs)
                elapsed = time.perf_counter() - start
                if elapsed > 10:
                    log.info(f"[TIMER] {func_name} completed in {elapsed:.2f}s")
                else:
                    log.debug(f"[TIMER] {func_name} completed in {elapsed:.3f}s")
                return result
            except Exception as e:
                elapsed = time.perf_counter() - start
                log.error(f"[TIMER] {func_name} FAILED after {elapsed:.2f}s: {e}")
                raise
        return wrapper
    return decorator


# ============================================================
# 日志查看
# ============================================================

def show_recent_logs(lines: int = 20, level: str = "INFO") -> None:
    """
    查看最近日志
    从今天的日志文件中读取最后 N 行

    参数：
    - lines: 显示行数
    - level: 过滤级别（INFO / WARN / ERROR 等）
    """
    log_file = LOGS_DIR / f"{datetime.now().strftime('%Y%m%d')}.log"
    if not log_file.exists():
        print(f"日志文件不存在: {log_file}")
        return

    level_num = getattr(logging, level.upper(), logging.INFO)

    with open(log_file, "r", encoding="utf-8") as f:
        all_lines = f.readlines()

    # 按级别过滤
    filtered = []
    for line in all_lines:
        for lvl_name, lvl_num in logging._nameToLevel.items():
            if f"[{lvl_name}]" in line and lvl_num >= level_num:
                filtered.append(line.strip())
                break

    # 取最后 N 行
    recent = filtered[-lines:] if len(filtered) > lines else filtered

    print(f"\n===== 最近日志 ({log_file.name}) [≥{level}] =====")
    for line in recent:
        print(f"  {line}")
    print(f"  (显示 {len(recent)}/{len(filtered)} 条)\n")


def log_summary() -> dict:
    """返回今日日志统计摘要"""
    log_file = LOGS_DIR / f"{datetime.now().strftime('%Y%m%d')}.log"
    if not log_file.exists():
        return {"error": "no log file today"}

    counts = {"DEBUG": 0, "INFO": 0, "WARNING": 0, "ERROR": 0, "CRITICAL": 0}
    with open(log_file, "r", encoding="utf-8") as f:
        for line in f:
            for level in counts:
                if f"[{level}]" in line:
                    counts[level] += 1
                    break

    return {
        "file": str(log_file),
        "size_kb": round(log_file.stat().st_size / 1024, 1),
        "counts": counts,
    }
