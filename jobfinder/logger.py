"""统一日志配置。

使用方式：
    from jobfinder.logger import get_logger
    log = get_logger(__name__)
    log.info("消息")
    log.debug("调试信息")

环境变量：
    LOG_LEVEL   日志级别，默认 INFO（可设为 DEBUG 查看详细请求）
    LOG_FILE    日志文件路径，默认 jobfinder.log（设为空字符串禁用文件日志）
"""
from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler

_LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
_LOG_FILE = os.getenv("LOG_FILE", "jobfinder.log")

_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_DATE_FMT = "%Y-%m-%d %H:%M:%S"

_configured = False


def _configure() -> None:
    global _configured
    if _configured:
        return
    _configured = True

    root = logging.getLogger("jobfinder")
    root.setLevel(_LOG_LEVEL)
    root.propagate = False

    formatter = logging.Formatter(_FMT, datefmt=_DATE_FMT)

    # ── 控制台 handler（WARNING 及以上，避免干扰 Rich UI）─────────────────────
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(logging.WARNING)
    console_handler.setFormatter(formatter)
    root.addHandler(console_handler)

    # ── 文件 handler（写入全部日志）──────────────────────────────────────────
    if _LOG_FILE:
        file_handler = RotatingFileHandler(
            _LOG_FILE,
            maxBytes=5 * 1024 * 1024,   # 5 MB 自动轮转
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setLevel(_LOG_LEVEL)
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)


def get_logger(name: str) -> logging.Logger:
    """获取模块专属 logger，自动触发全局配置。"""
    _configure()
    # 将模块路径转为 jobfinder.xxx 命名空间
    if not name.startswith("jobfinder"):
        name = f"jobfinder.{name}"
    return logging.getLogger(name)
