"""日志兼容层（历史遗留接口的空实现）。

早期版本在本模块里用标准库 ``logging`` + ``RotatingFileHandler``，把本插件
的日志额外落盘到 ``<插件数据目录>/logs/{pcrjjc.log,error.log}``（2MB × 3 轮转），
与控制台输出并行。

AstrBot 插件市场审核明令禁止插件使用 Python 内置 ``logging`` 模块，
因此落盘功能已整体移除：**插件所有日志统一通过 ``from astrbot.api import logger``
输出**，由 AstrBot 自身负责控制台打印与文件记录。

为不牵动业务代码，本模块保留原有的两个对外函数及其签名，行为退化为空操作：

    * :func:`setup_local_log`  —— 恒返回 ``{}``（不再创建目录、不再挂 Handler）
    * :func:`remove_local_log` —— 恒返回 ``0``（没有 Handler 需要摘除）

新代码请直接使用 ``from astrbot.api import logger``，不要再依赖本模块。
"""

from typing import Any, Dict, Optional

from astrbot.api import logger

__all__ = ["setup_local_log", "remove_local_log"]


def remove_local_log(target_logger: Any = None) -> int:
    """[兼容接口] 摘除本地日志 Handler —— 已无本地日志，恒返回 0。

    :param target_logger: 保留参数以兼容旧调用（当前不使用）
    :return: 被移除的 Handler 数量，恒定 ``0``
    """
    return 0


def setup_local_log(
    target_logger: Any = None,
    data_dir: Optional[str] = None,
    plugin_root: Optional[str] = None,
) -> Dict[str, object]:
    """[兼容接口] 挂载本地日志文件输出 —— 已移除，恒返回空字典。

    :param target_logger: 保留参数以兼容旧调用（当前不使用）
    :param data_dir: 保留参数以兼容旧调用（当前不使用）
    :param plugin_root: 保留参数以兼容旧调用（当前不使用）
    :return: 恒为 ``{}``
    """
    try:
        logger.info("[pcrjjc] 日志统一由 AstrBot logger 输出，本地日志落盘已停用")
    except Exception:
        # 日志本身不能成为插件启动的阻碍
        pass
    return {}