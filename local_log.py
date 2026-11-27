"""插件本地日志落盘（与 AstrBot 控制台输出并行，互不干扰）。

目录：``<插件数据目录>/logs``（自动创建）
    * ``pcrjjc.log`` —— INFO 及以上，完整流水
    * ``error.log``  —— WARNING 及以上，便于快速定位异常

特性：
    * 单文件 2MB 轮转，最多保留 3 份历史备份（``xx.log.1`` ~ ``xx.log.3``），强制 utf-8；
    * **只收本插件源码产生的日志**（按 ``record.pathname`` 前缀过滤），
      不会把 AstrBot 核心与其它插件的日志灌进来；
    * **幂等挂载**：重复初始化（热重载）会先摘掉并关闭同名旧 Handler，
      再挂新的，避免日志重复打印、以及 Windows 下文件被占用无法轮转；
    * 自动适配 Logger 实现：标准 ``logging``（``addHandler``）优先，
      ``loguru``（``add``/``remove``）兜底。

注意：本模块只设置 Handler 级别，**不改动 logger 自身的级别**，
因此不会改变 AstrBot 控制台的打印量；若 AstrBot 全局日志级别高于 INFO，
落盘文件同样只会有该级别以上的记录（这是"不改控制台"的必然结果）。
"""

import logging
import os
from logging.handlers import RotatingFileHandler
from typing import Dict, List, Optional

# ---- 可调参数 ----
MAX_BYTES = 2 * 1024 * 1024        # 单文件上限 2MB
BACKUP_COUNT = 3                   # 最多保留 3 份历史备份
LOG_DIR_NAME = "logs"
MAIN_LOG_NAME = "pcrjjc.log"
ERROR_LOG_NAME = "error.log"
MAIN_LOG_LEVEL = logging.INFO
ERROR_LOG_LEVEL = logging.WARNING

# handler 身份标记：热重载后模块对象会换，但 handler 实例仍挂在共享 logger 上，
# 只能靠属性标记 + 名字前缀双重识别才能可靠地认回旧 handler。
HANDLER_MARK = "_pcrjjc_local_handler"
HANDLER_NAME_PREFIX = "pcrjjc:"

# loguru 分支的 sink id 登记（按 id(logger) 索引，避免往 Logger 实例塞属性）
_LOGURU_SINKS: Dict[int, List[int]] = {}

PLUGIN_ROOT = os.path.dirname(os.path.abspath(__file__))


def _norm(path: str) -> str:
    try:
        return os.path.normcase(os.path.abspath(path))
    except Exception:
        return ""


def _is_plugin_record(record: logging.LogRecord, plugin_root_norm: str) -> bool:
    """判断一条日志是否由本插件源码产生。"""
    pathname = getattr(record, "pathname", "") or ""
    if pathname and plugin_root_norm:
        if _norm(pathname).startswith(plugin_root_norm):
            return True
    # 兜底：插件内的日志统一带 [pcrjjc] 前缀（含 AstrBot 之外调用的情况）
    try:
        msg = record.getMessage()
    except Exception:
        msg = ""
    return "[pcrjjc]" in str(msg)


class _PluginScopeFilter(logging.Filter):
    """只放行本插件源码的日志记录。"""

    def __init__(self, plugin_root_norm: str):
        super().__init__()
        self._root = plugin_root_norm

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        try:
            return _is_plugin_record(record, self._root)
        except Exception:
            return False


def _loguru_filter(record) -> bool:
    """loguru 记录（dict）的插件范围过滤。"""
    try:
        pathname = str(record.get("file").path) if record.get("file") else ""
    except Exception:
        pathname = ""
    if pathname and _norm(pathname).startswith(_norm(PLUGIN_ROOT)):
        return True
    return "[pcrjjc]" in str(record.get("message") or "")


def _make_handler(path: str, level: int, plugin_root_norm: str) -> RotatingFileHandler:
    handler = RotatingFileHandler(
        path,
        maxBytes=MAX_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
        delay=False,
    )
    handler.setLevel(level)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    # 注意顺序：level 过滤在 Handler 上，插件范围过滤在 Filter 上，两者叠加生效
    handler.addFilter(_PluginScopeFilter(plugin_root_norm))
    setattr(handler, HANDLER_MARK, True)
    handler.name = HANDLER_NAME_PREFIX + os.path.basename(path)
    return handler


def remove_local_log(logger) -> int:
    """摘掉并关闭本插件挂过的全部日志 Handler / sink，返回移除数量。

    可安全重复调用（热重载、terminate 都会走这里）。
    """
    removed = 0
    if logger is None:
        return 0

    # ---- 标准 logging ----
    for handler in list(getattr(logger, "handlers", None) or []):
        try:
            is_mine = bool(getattr(handler, HANDLER_MARK, False)) or str(
                getattr(handler, "name", "") or ""
            ).startswith(HANDLER_NAME_PREFIX)
        except Exception:
            is_mine = False
        if not is_mine:
            continue
        try:
            logger.removeHandler(handler)
        except Exception:
            pass
        # 先 flush 再 close，确保缓冲区内容落盘、并释放文件句柄
        for op in (handler.flush, handler.close):
            try:
                op()
            except Exception:
                pass
        removed += 1

    # ---- loguru ----
    sink_ids = _LOGURU_SINKS.pop(id(logger), None)
    if sink_ids:
        for sink_id in sink_ids:
            try:
                logger.remove(sink_id)
                removed += 1
            except Exception:
                pass

    return removed


def setup_local_log(
    logger,
    data_dir: str,
    plugin_root: Optional[str] = None,
) -> Dict[str, object]:
    """挂载本地日志文件输出（幂等）。

    :param logger: ``astrbot.api.logger``（标准 logging 或 loguru 实例）
    :param data_dir: 插件数据目录（``StarTools.get_data_dir(...)``）
    :param plugin_root: 插件源码根目录，用于"只收本插件日志"，默认取本文件所在目录
    :return: ``{"dir": 日志目录, "files": [pcrjjc.log, error.log]}``；失败时返回 ``{}``
    """
    result: Dict[str, object] = {}

    # 1. 先摘掉上次加载遗留的旧 handler，再继续（热重载防重复的关键一步）
    try:
        remove_local_log(logger)
    except Exception:
        pass

    if logger is None:
        return result

    try:
        log_dir = os.path.join(str(data_dir), LOG_DIR_NAME)
        os.makedirs(log_dir, exist_ok=True)
        main_path = os.path.join(log_dir, MAIN_LOG_NAME)
        error_path = os.path.join(log_dir, ERROR_LOG_NAME)
        plugin_root_norm = _norm(plugin_root or PLUGIN_ROOT)

        # ---- 标准 logging（AstrBot 现行实现）----
        if hasattr(logger, "addHandler") and hasattr(logger, "handlers"):
            logger.addHandler(_make_handler(main_path, MAIN_LOG_LEVEL, plugin_root_norm))
            logger.addHandler(_make_handler(error_path, ERROR_LOG_LEVEL, plugin_root_norm))

        # ---- loguru 兜底 ----
        elif hasattr(logger, "add") and hasattr(logger, "remove"):
            sink_ids: List[int] = []
            for path, level in (
                (main_path, "INFO"),
                (error_path, "WARNING"),
            ):
                sink_ids.append(
                    logger.add(
                        path,
                        level=level,
                        rotation="2 MB",
                        retention=BACKUP_COUNT,
                        encoding="utf-8",
                        enqueue=False,
                        backtrace=False,
                        diagnose=False,
                        format="{time:YYYY-MM-DD HH:mm:ss} [{level}] {message}",
                        filter=_loguru_filter,
                    )
                )
            _LOGURU_SINKS[id(logger)] = sink_ids

        else:
            # 未知 Logger 实现：不报错，交由 AstrBot 控制台即可
            return result

        result = {"dir": log_dir, "files": [main_path, error_path]}
    except Exception:
        # 日志落盘只是加分项：只读容器、目录无权限等场景绝不能让插件起不来
        return {}

    return result