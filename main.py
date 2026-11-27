import asyncio
import inspect
from datetime import datetime
from functools import wraps
from itertools import count
from json import dump, load
import os
import re
import traceback
from typing import Dict, List

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter, MessageChain
import astrbot.api.message_components as Comp
from astrbot.api.star import Context, Star, register

import importlib
import sys

# 强制热重载所有子模块，确保后台点击“重载插件”无需重启容器即可生效最新代码与表结构
_pkg_prefix = __package__


def _cancel_stale_tasks():
    """取消上一次加载遗留的查询协程（P2-6 泄漏）。

    必须赶在 importlib.reload 之前跑：否则旧 worker 手里还是旧的 pcrclient/
    ApiException 类，新代码 catch 不到，会隐式变成“未知异常”。
    用“协程源码文件路径”识别，与模块是否被 reload 无关（协程对象没有 __module__）。
    """
    try:
        running = asyncio.all_tasks()
        current = asyncio.current_task()
    except RuntimeError:
        return 0          # 没有运行中的事件循环（如单测/静态导入），跳过
    cancelled = 0
    for task in running:
        if task is current:
            continue
        coro = task.get_coro()
        # 协程对象没有 __module__，只能从 code 对象拿源文件路径
        code = getattr(coro, "cr_code", None)
        filename = getattr(code, "co_filename", "") or ""
        qualname = getattr(coro, "__qualname__", "") or ""
        if "astrbot_plugin_pcrjjc" not in filename:
            continue
        if (
            "query_loop" in qualname
            or qualname.endswith("_query")
            or "notice_dispatcher" in qualname
        ):
            task.cancel()
            cancelled += 1
    return cancelled


def _dispose_stale_db_engine():
    """热重载前释放上一次加载遗留的数据库连接池（P2-6）。

    `importlib.reload(database.dal)` 会在模块底部重新 `create_async_engine` 一次，
    而旧 engine 没有任何人 dispose：反复点「重载插件」就会攒下一批 aiosqlite 连接
    与文件句柄（Windows 下还会一直占着 PCRJJC.db）。这里在 reload 之前把旧 engine
    交给当前事件循环关闭。首次加载时 sys.modules 里还没有这个模块，直接 no-op。
    """
    old = sys.modules.get(f"{_pkg_prefix}.database.dal") if _pkg_prefix else None
    engine = getattr(getattr(old, "pcr_sqla", None), "engine", None)
    if engine is None:
        return
    try:
        asyncio.get_running_loop().create_task(engine.dispose())
    except Exception:
        # 没有运行中的事件循环（静态导入/单测）时跳过：旧 engine 会随模块对象被回收
        pass


# 重载时先收尾旧任务，再换新类
if _pkg_prefix and _cancel_stale_tasks():
    logger.warning("[pcrjjc] 检测到上次加载遗留的后台查询任务，已在重载前取消")

if _pkg_prefix:
    # 必须在 reload 之前：否则新 engine 建好了，旧 engine 的连接还挂着
    _dispose_stale_db_engine()
    for _sub in (
        "client.rsacr",
        "client.bsgamesdk",
        "client.pcrclient",
        "var",
        "database.models",
        "database.dal",
        "tool",
        "query",
        "img.text2img",
        "img.create_img",
        "local_log",
        "utils",
    ):
        _mod_name = f"{_pkg_prefix}.{_sub}"
        if _mod_name in sys.modules:
            importlib.reload(sys.modules[_mod_name])


from .client.pcrclient import init_device_id
from .client import bsgamesdk
from .database.dal import PCRBind, pcr_sqla
from .img.text2img import image_draw
from .local_log import remove_local_log, setup_local_log
from .query import login_all, query_all
from .tool import refresh_account
from .utils import (
    HISTORY_KEEP_DAYS_DEFAULT,
    _as_bool,
    _build_image_comp,
    bind_pcrid,
    detial_query,
    get_platform_id,
    init_context,
    query_loop,
    set_history_keep_getter,
    set_peak_config_getter,
    set_recovery_notifier,
    start_notice_dispatcher,
    stop_notice_dispatcher,
    talent_query,
    user_query,
)
from .var import (
    LoadBase,
    Platform,
    Priority,
    private_dict,
    query_cache,
    queue_dict,
    talent_cache,
)

_base_dir = os.path.dirname(__file__)
try:
    from astrbot.api.star import StarTools
    _data_dir = str(StarTools.get_data_dir("astrbot_plugin_pcrjjc"))
except Exception:
    _data_dir = os.path.join("data", "plugin_data", "astrbot_plugin_pcrjjc")
os.makedirs(_data_dir, exist_ok=True)

_frame_json_path = os.path.join(_data_dir, "frame.json")
_frame_json_fresh = not os.path.exists(_frame_json_path)
if _frame_json_fresh:
    with open(_frame_json_path, "w", encoding="UTF-8") as _f:
        dump({"default_frame": "color.png", "customize": {}}, _f, indent=4, ensure_ascii=False)


def _migrate_and_prune_frame_json() -> None:
    """一次性把旧位置的 frame.json 合并进配置目录，并删除冗余副本（P3-5）。

    P1-7 之前 img/create_img.py 会按「相对 CWD 的 data/plugin_data → 插件目录 →
    img 目录」三级回退读取，部分老部署的用户自定义头像框就落在插件目录或 img/ 下；
    而现在的代码只认配置目录这一份。所以先把旧副本的 customize 合并进来（同键以
    配置目录已有的为准），再把旧文件删掉，避免留下两份内容互相矛盾的死重量副本。
    """
    legacy_paths = (
        os.path.join(_base_dir, "frame.json"),
        os.path.join(_base_dir, "img", "frame.json"),
    )
    merged_customize: Dict[str, str] = {}
    legacy_default_frame = ""
    for legacy in legacy_paths:
        if not os.path.exists(legacy):
            continue
        try:
            with open(legacy, "r", encoding="UTF-8") as lf:
                l_data = load(lf)
        except Exception as e:
            logger.warning(f"[pcrjjc] 旧头像框配置读取失败，跳过合并：{legacy} -> {e}")
            continue
        if not legacy_default_frame:
            legacy_default_frame = str(l_data.get("default_frame") or "")
        for key, value in (l_data.get("customize") or {}).items():
            # 多份旧文件里出现同一用户时，以插件目录那份（先读到）为准
            merged_customize.setdefault(str(key), str(value))
        try:
            os.remove(legacy)
            logger.info(f"[pcrjjc] 头像框配置已迁移至 {_frame_json_path}，旧副本已删除：{legacy}")
        except OSError as e:
            logger.warning(f"[pcrjjc] 旧头像框副本删除失败（不影响功能）：{legacy} -> {e}")

    if not merged_customize and not (_frame_json_fresh and legacy_default_frame):
        return
    try:
        with open(_frame_json_path, "r", encoding="UTF-8") as cf:
            c_data = load(cf)
        c_data.setdefault("customize", {})
        added = {k: v for k, v in merged_customize.items() if k not in c_data["customize"]}
        c_data["customize"].update(added)
        # 配置目录这份是刚新建的默认文件时，沿用旧部署里用户改过的默认框
        if _frame_json_fresh and legacy_default_frame:
            c_data["default_frame"] = legacy_default_frame
        with open(_frame_json_path, "w", encoding="UTF-8") as wf:
            dump(c_data, wf, indent=4, ensure_ascii=False)
        if added:
            logger.info(f"[pcrjjc] 已从旧副本恢复 {len(added)} 条自定义头像框：{sorted(added)}")
    except Exception as e:
        logger.error(f"[pcrjjc] 头像框配置合并失败：{e}")


_migrate_and_prune_frame_json()

_admin_umo_path = os.path.join(_data_dir, "admin_umo.json")

# 查询批次号：使同一用户的多条并发指令各自聚合，互不干扰
_batch_seq = count()

# 单条「深域查询」允许的 UID 上限（P2-5）：查一次小号要跑完整协议，无上限时
# 一句指令就能把查询配额刷干，连带把普通竞技场轮询挤在后面。
TALENT_MAX_UIDS = 5

# 帮助图内容固定（只有「普通 / 管理员」两版），渲染一次就复用：
# 出图是 100ms 级的同步 CPU 活，而「竞技场帮助」是最常被触发的指令之一。
_help_pic_cache: Dict[bool, str] = {}


def _get_help_pic(text: str, is_admin: bool) -> str:
    pic = _help_pic_cache.get(is_admin)
    if pic is None:
        pic = _help_pic_cache[is_admin] = image_draw(text)
    return pic


def _reset_batch_cache(cache: dict, sender_id: str, batch: int, keep: int = 2) -> None:
    """登记新批次，并丢弃该用户过久的未完成批次。

    原实现直接 `cache[sender_id] = []`，用户连发两条同类指令会让两批互相覆盖、
    谁都凑不齐齐以至全部静默。按 (用户, 批次) 分桶后互不干扰；保留最近 keep 个
    未完成桶，防止用户放弃指令后内存无限堆积。
    """
    stale = sorted(k for k in cache if k[0] == sender_id and k[1] != batch)
    for k in stale[: max(len(stale) - keep, 0)]:
        cache.pop(k, None)
    cache[(sender_id, batch)] = {}


def _load_admin_umo() -> dict:
    if os.path.exists(_admin_umo_path):
        try:
            with open(_admin_umo_path, "r", encoding="utf-8") as f:
                return load(f)
        except Exception:
            pass
    return {"private_umo": "", "group_umo": ""}


def _save_admin_umo(data: dict):
    try:
        with open(_admin_umo_path, "w", encoding="utf-8") as f:
            dump(data, f, indent=4, ensure_ascii=False)
    except Exception as e:
        logger.error(f"[pcrjjc] 保存管理员路由失败: {e}")


# 注意：第 2 个参数是插件市场校验的署名，必须与 metadata.yaml 里的 author 完全一致。
# 原作者（HoshinoBot 版 pcrjjc_huannai2）的致谢保留在 README「特别感谢」里，不要写在这里。
@register("astrbot_plugin_pcrjjc", "AlfinReiseArnor", "公主连结竞技场与深域查询插件", "1.0.0")
class PcrJjcPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        init_context(context)

        # 日志统一由 AstrBot 的 logger 输出（插件市场审核禁止使用内置 logging）。
        # setup_local_log 仅作为历史兼容空实现保留：不再创建 logs/ 目录、不挂文件 Handler。
        setup_local_log(logger, _data_dir)
        # 维护结束后通知管理员（由 utils.query_loop 在探测成功时调用）
        set_recovery_notifier(self._notify_maintenance_recovered)

        self.admin_id = str(self.config.get("admin_id", "")).strip()

        # 【待命判定】插件不设自己的总开关（WebUI 插件列表里那个开/关由 AstrBot 控制）：
        # 只要「管理员特征码」与「至少一个查询账号」没填全，插件就停在待命：
        # 不登录游戏账号（不触发过码）、不轮询、不推送，指令只回一句配置提示。
        # 用户在 WebUI 保存配置后（AstrBot 会热重载插件），这里重新判定即自动开工。
        self._missing_conf: List[str] = self._check_required_config()
        self.ready = not self._missing_conf
        private_dict[Platform.b_id.value] = int(self.config.get("b_max_pri", 20))
        private_dict[Platform.qu_id.value] = int(self.config.get("qu_max_pri", 20))

        # 结算时段加速轮询：北京时间 14:50~15:05 改用 WebUI 配置的间隔（代码内限 5~30s）。
        # 传闭包而不是快照：WebUI 保存后即使不重载插件，下一轮轮询也能读到新值。
        # 开关的字符串容错统一由 utils._as_bool 处理，这里不做二次判断。
        def _peak_poll_conf():
            return (
                _as_bool(self.config.get("peak_poll_enable", True)),
                self.config.get("peak_poll_interval", 10),
            )

        set_peak_config_getter(_peak_poll_conf)

        # 击剑记录保留天数（性能）：历史表只增不减会让 SQLite 与所有历史查询
        # 一起变慢。同样用闭包读取，改配置不必重载插件；0 = 永久保留。
        set_history_keep_getter(
            lambda: self.config.get("history_keep_days", HISTORY_KEEP_DAYS_DEFAULT)
        )
        _peak_on, _peak_iv = _peak_poll_conf()
        logger.info(
            f"[pcrjjc] 结算时段加速轮询：{'开启' if _peak_on else '关闭'}，"
            f"北京时间 14:50~15:05 生效，间隔由 WebUI 配置（限 5~30 秒，当前填 {_peak_iv}）"
        )
        self.admin_routes = _load_admin_umo()

        # 过码配置（WebUI 可选填，留空则沿用内置默认值）
        _site, _api, _wait = bsgamesdk.configure(
            self.config.get("captcha_site", ""),
            self.config.get("captcha_api", ""),
            self.config.get("captcha_wait", 0),
        )
        # 自动过码：留空 = 用内置默认接口（旧版硬编码的公共过码服务），不是“不启用”
        if not str(self.config.get("captcha_api", "") or "").strip():
            logger.info(
                f"[pcrjjc] 自动过码接口留空，使用内置默认服务 {_api}"
                "（想换服务就填 captcha_api）"
            )
        # 手动过码：留空 = 不启用兜底，这是 README 推荐的默认状态（旧版写死的
        # yousite.com 只是占位域名，并非可用站点），所以这里只作中性 INFO 说明。
        # 不再用 WARNING 预告故障：首次安装时自动过码走内置默认接口通常一次就过，
        # 根本不会走到手动兜底；真过不了码时 login() 会如实报错（见 bsgamesdk）。
        _fallback = (
            f"手动兜底站点={_site}" if _site
            else "未启用手动兜底（captcha_site 留空，属默认行为，无需处理）"
        )
        logger.info(
            f"[pcrjjc] 过码配置：{_fallback}，"
            f"自动过码接口={_api or '未配置'}，手动等待 {_wait}s"
        )

        bsgamesdk.register_send_func(
            private_func=self._send_private,
            group_func=self._send_group
        )
        self._bg_tasks: List[asyncio.Task] = []
        if self.ready:
            self._bg_tasks.append(asyncio.create_task(self._on_startup()))
        else:
            # 配置不全：只挂起指令，不启动任何后台动作（也就不登录、不过码）
            self._gate_handlers_until_ready()
            logger.warning(
                "[pcrjjc] 尚未完成配置，插件已停待命：还缺 "
                + "、".join(self._missing_conf)
                + "。请在 WebUI 的插件设置里填好后保存（会重载插件），插件将自动开始登录与轮询"
            )
    def _is_plugin_admin(self, event: AstrMessageEvent) -> bool:
        """同时兼容 AstrBot 全局管理员与本插件设置的管理员特征码"""
        if event.is_admin():
            return True
        if self.admin_id and str(event.get_sender_id()) == self.admin_id:
            return True
        return False

    async def _record_route(self, event: AstrMessageEvent):
        """自动识别并记录当前会话的通用路由 (unified_msg_origin)"""
        umo = event.unified_msg_origin
        if not umo:
            return
        sender_id = str(event.get_sender_id())
        gid = event.get_group_id()

        # 1. 自动记录管理员的私聊或群聊路由
        if self._is_plugin_admin(event):
            updated = False
            if not gid and self.admin_routes.get("private_umo") != umo:
                self.admin_routes["private_umo"] = umo
                updated = True
                logger.info(f"[pcrjjc] 已自动捕获并保存管理员私聊推送路由: {umo}")
            elif gid and not self.admin_routes.get("private_umo") and self.admin_routes.get("group_umo") != umo:
                self.admin_routes["group_umo"] = umo
                updated = True
            if updated:
                _save_admin_umo(self.admin_routes)

        # 2. 自动更新普通用户的群聊或私聊路由到数据库
        #    只在路由真的变化时才写库：这条逻辑跑在几乎每条指令的最前面，
        #    无条件 UPDATE 会让“发一条指令”固定产生 2 次写事务（还会和后台
        #    轮询写击剑记录抢锁）。稳态下绝大多数指令都能跳过写入。
        for plat in (Platform.b_id.value, Platform.qu_id.value):
            binds = await pcr_sqla.get_bind(plat, sender_id)
            if not binds:
                continue
            if gid:
                if all(
                    str(bind.group or "") == str(gid) and str(bind.group_umo or "") == umo
                    for bind in binds
                ):
                    continue
                await pcr_sqla.update_bind(plat, {"group": str(gid), "group_umo": umo}, sender_id)
            else:
                if all(str(bind.private_umo or "") == umo for bind in binds):
                    continue
                await pcr_sqla.update_bind(plat, {"private_umo": umo}, sender_id)

    async def _send_private(self, target_id: str, text: str):
        umo = self.admin_routes.get("private_umo") or self.admin_routes.get("group_umo")
        if not umo:
            logger.warning(f"[pcrjjc] 收到发往 {target_id} 的私聊通知，但尚未记录管理员路由，请先私聊机器人发送一次指令（如“竞技场帮助”）！")
            return
        try:
            await self.context.send_message(umo, MessageChain([Comp.Plain(text)]))
        except Exception as e:
            logger.error(f"[pcrjjc] 管理员通知发送失败 ({umo}): {e}")

    async def _send_group(self, target_id: str, text: str):
        umo = self.admin_routes.get("group_umo")
        if not umo:
            return
        try:
            await self.context.send_message(umo, MessageChain([Comp.Plain(text)]))
        except Exception as e:
            logger.error(f"[pcrjjc] 群消息发送失败 ({umo}): {e}")

    async def _notify_maintenance_recovered(self, platform: int):
        """服务器维护结束、轮询已自动恢复时通知管理员（由 utils.query_loop 调用）"""
        umo = self.admin_routes.get("private_umo") or self.admin_routes.get("group_umo")
        if not umo:
            logger.warning(
                "[pcrjjc] 维护恢复通知未发出：尚未记录管理员推送路由，"
                "请先私聊机器人发一次“竞技场帮助”"
            )
            return
        plat = "渠道服" if platform == Platform.qu_id.value else "B服"
        try:
            await self.context.send_message(
                umo, MessageChain([Comp.Plain(f"【pcrjjc】{plat} 服务器维护已结束，查询轮询已自动恢复正常。")])
            )
        except Exception as e:
            logger.error(f"[pcrjjc] 维护恢复通知发送失败 ({umo}): {e}")

    @staticmethod
    def _cancel_stale_query_tasks():
        """再次确认遗留任务已被清掉（重载路径与 _on_startup 双保险）"""
        cancelled = _cancel_stale_tasks()
        if cancelled:
            logger.warning(f"[pcrjjc] 已取消上次加载遗留的 {cancelled} 个后台查询任务（避免重复登录/重复轮询）")

    async def _on_startup(self):
        # 账号同步与查询启动分开 try：旧实现一个大 try 包到底，
        # 数据库只读导致 refresh_account 抛错时，login_all/query_loop 全部被跳过，
        # 插件静默零功能（线上 11-23 两次）。
        account_list = self.config.get("bili_accounts", [])
        try:
            await refresh_account(account_list)
        except Exception as e:
            hint = ""
            if "readonly" in str(e).lower() or "read-only" in str(e).lower():
                hint = "（数据库不可写：请检查 data/plugin_data/astrbot_plugin_pcrjjc 目录权限与挂载）"
            logger.error(
                f"[pcrjjc] 刷新查询账号失败{hint}，将尝试用数据库中已有的账号继续启动：\n{traceback.format_exc()}"
            )

        try:
            self._cancel_stale_query_tasks()
            # 推送分发协程必须先于轮询启动：轮询产出的变动只入队，由它负责合并发送
            self._bg_tasks.append(start_notice_dispatcher())
            await login_all(admin_id=self.admin_id if self.admin_id else None)

            for platform in (Platform.b_id.value, Platform.qu_id.value):
                if queue_dict[platform]:
                    self._bg_tasks.append(asyncio.create_task(query_loop(platform)))
            logger.info("pcrjjc 后台初始化与竞技场轮询任务已启动（国服B服/渠道服）")
        except Exception:
            logger.error(f"pcrjjc 初始化失败:\n{traceback.format_exc()}")

    def _check_required_config(self) -> List[str]:
        """返回还缺哪些必填配置（空列表 = 配置齐了，可以开始工作）。"""
        missing: List[str] = []
        if not self.admin_id:
            missing.append("「管理员特征码」")

        accounts = self.config.get("bili_accounts", []) or []
        has_account = False
        if isinstance(accounts, list):
            for item in accounts:
                if not isinstance(item, dict):
                    continue
                # 账号与密码都填了才算一个可用查询号（template_list 里新建的行默认是空串）
                if str(item.get("account", "")).strip() and str(item.get("password", "")).strip():
                    has_account = True
                    break
        if not has_account:
            missing.append("「查询用 B站/渠道服 账号」（至少填一个账号 + 密码）")
        return missing

    def _gate_handlers_until_ready(self):
        """配置没填全时，把本插件所有指令换成“去填配置”的空实现。

        只包本类里 `@filter.xxx` 装饰过的异步生成器（即指令 handler）：
        用 functools.wraps 保留原函数元数据（含 __filter__），AstrBot 的分发与
        热重载都不受影响，也不会干扰其它插件注册的指令。
        """
        cls = type(self)
        gated = 0
        for name, fn in list(vars(cls).items()):
            if not inspect.isasyncgenfunction(fn):
                continue

            @wraps(fn)
            async def _noop(self, event):
                # 待命期只提示管理员（含 AstrBot 全局管理员），普通用户静默不扰群
                if self._is_plugin_admin(event):
                    yield event.plain_result(
                        "插件还没配置好，暂不可用。请在 AstrBot WebUI 的插件设置里填写："
                        + "、".join(self._missing_conf)
                        + "，保存后插件会自动重载并开始过码登录与轮询。"
                    )
                event.stop_event()

            setattr(cls, name, _noop)
            gated += 1
        logger.info(f"[pcrjjc] 已挂起 {gated} 个指令（等待完成配置）")

    async def terminate(self):
        for task in self._bg_tasks:
            if not task.done():
                task.cancel()
        self._bg_tasks.clear()

        from .query import cancel_query_tasks, close_clients
        cancel_query_tasks()
        await close_clients()
        # 推送队列里的残余变动随插件卸载丢弃（历史记录已入库，不受影响）
        await stop_notice_dispatcher()
        await pcr_sqla.close()

        # 本地日志落盘已移除，remove_local_log 仅作为历史兼容空实现保留（无副作用）。
        remove_local_log(logger)

    async def _get_member_name(self, event: AstrMessageEvent, target_qid: str) -> str:
        gid = event.get_group_id()
        if gid and hasattr(event, "bot"):
            try:
                info = await event.bot.get_group_member_info(group_id=gid, user_id=target_qid)
                return info.get("card") or info.get("nickname") or str(target_qid)
            except Exception:
                pass
        if str(target_qid) == str(event.get_sender_id()):
            return event.get_sender_name() or str(target_qid)
        return str(target_qid)
    @filter.regex(r"竞技场帮助")
    async def send_jjchelp(self, event: AstrMessageEvent):
        await self._record_route(event)
        sv_help = """========================================
    【 AstrBot 公主连结竞技场插件 】
========================================
欢迎使用！本助手支持群聊与私聊。
如需查询渠道服，请在指令前加“渠”字。

▼ ---------------- [ 基础绑定 ] ---------------- ▼
● 竞技场绑定 [UID] [昵称]
  └ 昵称可省略，自动获取游戏内名称
  └ 每个账号最多可绑定 8 个 UID
● 竞技场订阅状态
  └ 查看自己已绑定的账号及推送开关
● 竞技场修改昵称 [序号] [新昵称]
  └ 只绑定 1 个账号时序号可省略
● 查询竞技场订阅数
  └ 查看当前服务器已订阅的账号总数
● 删除竞技场绑定 [序号]
  └ 序号必填，如“删除竞技场绑定 1”
● 清空竞技场绑定
  └ 一键解绑自己的所有账号

▼ ---------------- [ 查询功能 ] ---------------- ▼
● 竞技场查询
  └ 快速查看自己所有账号的简讯
● 竞技场查询 [UID]
  └ 查询指定账号（可查他人）
● 详细查询
  └ 生成详细数据长图（单号默认直查）
● 详细查询 [序号/UID]
  └ 查询自己绑定的指定账号
● 深域查询 [UID1] [UID2] ...
  └ 空格隔开，单次最多 5 个号
● 竞技场记录
  └ 查看自己当天的排名变动历史
  └ 可加序号只查某个绑定，如“竞技场记录 2”
  └ “击剑记录”是同一指令的别名

▼ ---------------- [ 推送设置 ] ---------------- ▼
● 在本群推送
  └ 将推送目标设为当前群聊
● 换私聊推送
  └ 将推送目标设为私聊（需先在私聊发指令）
● 竞技场设置 [开启/关闭] [内容] [序号]
  └ 例：竞技场设置 开启 排名上升 1
  └ 内容可选：jjc / pjjc / 排名上升 / 上线提醒
● 竞技场设置 [四位数字] [序号]
  └ 例：竞技场设置 1110
  └ 依次代表：JJC、PJJC、排名上升、上线提醒
  └ 0关闭，1开启。第4位可选0~3，级别越高上线提醒越频繁

▼ ---------------- [ 个性化 ] ---------------- ▼
● 更换头像框 [文件名]
  └ 例：更换头像框 gold.png
  └ 只发“更换头像框”可查看当前可选列表
● 查询头像框
  └ 查看自己当前使用的头像框
========================================"""

        is_admin = self._is_plugin_admin(event)
        if is_admin:
            sv_help_adm = """
▼ -------------- [ 管理员专属 ] -------------- ▼
● pcrjjc负载查询
● pcrjjc删除绑定 [用户特征码]
● pcrjjc关闭私聊推送
● pcrjjc换设备id
========================================"""
            sv_help += sv_help_adm

        # 出图放到线程池：渲染期间事件循环仍可处理其它消息/轮询回调
        pic = await asyncio.to_thread(_get_help_pic, sv_help, is_admin)
        yield event.chain_result([_build_image_comp(pic)])
        event.stop_event()

    @filter.regex(r"查询竞技场订阅数")
    async def pcrjjc_number(self, event: AstrMessageEvent):
        await self._record_route(event)
        platform_id = get_platform_id(event)
        # 只取计数，不再把整张绑定表读成 ORM 对象只为拿一个 len()
        count = await pcr_sqla.count_bind(platform_id)
        yield event.plain_result(f"当前竞技场已订阅的账号数量为【{count}】个")
        event.stop_event()

    @filter.regex(r"竞技场查询")
    async def on_query_arena(self, event: AstrMessageEvent):
        await self._record_route(event)
        text = event.message_str.strip()
        ret = re.search(r"(渠?)竞技场查询 ?(\d+)?", text)
        if not ret:
            return

        qid = str(event.get_sender_id())
        platform_id = get_platform_id(event)

        query_dict: Dict[int, int] = {}
        if ret.group(2):
            if len(ret.group(2)) != 13:
                yield event.plain_result("位数不对，国服uid是13位的！")
                event.stop_event()
                return
            pcrid = int(ret.group(2))
            query_list = [PCRBind(platform=platform_id, pcrid=pcrid)]
            query_dict[pcrid] = 0
        else:
            query_list: List[PCRBind] = await pcr_sqla.get_bind(platform_id, qid)
            if not query_list:
                yield event.plain_result("您还没有绑定竞技场！")
                event.stop_event()
                return
            for i, bind in enumerate(query_list):
                query_dict[bind.pcrid] = i

        sender_id = str(event.get_sender_id())
        batch = next(_batch_seq)
        _reset_batch_cache(query_cache, sender_id, batch)
        await query_all(
            query_list,
            platform_id,
            user_query,
            {"ev": event, "info": query_dict, "platform": platform_id, "batch": batch},
            Priority.query_user.value,
        )
        event.stop_event()

    @filter.regex(r"竞技场订阅状态")
    async def send_arena_sub_status(self, event: AstrMessageEvent):
        await self._record_route(event)
        qid = str(event.get_sender_id())
        platform_id = get_platform_id(event)
        user_bind: List[PCRBind] = await pcr_sqla.get_bind(platform_id, qid)
        if not user_bind:
            yield event.plain_result("您还没有绑定竞技场！")
            event.stop_event()
            return
        name = await self._get_member_name(event, qid)
        first_bind = user_bind[0]
        reply = f'{name}（{qid}）的竞技场订阅列表：\n\n群特征码：{first_bind.group or "未设置"}\n推送方式：{"私聊推送" if first_bind.private else "群聊推送"}\n'
        for i, bind in enumerate(user_bind):
            reply += f"\n【{i+1}】{bind.name}（{bind.pcrid}）\n"
            noticeType = "推送内容："
            if bind.jjc_notice:
                noticeType += "jjc、"
            if bind.pjjc_notice:
                noticeType += "pjjc、"
            if bind.up_notice:
                noticeType += "排名上升、"
            if bind.online_notice:
                noticeType += f"上线提醒LV{bind.online_notice}、"
            noticeType = "推送内容：无" if noticeType == "推送内容：" else noticeType.strip("、")
            reply += noticeType + "\n"
        reply += "###上线提醒LV越高，提醒越频繁。详情见竞技场帮助\n"
        yield event.chain_result([_build_image_comp(await asyncio.to_thread(image_draw, reply))])
        event.stop_event()

    @filter.regex(r"(?:击剑|竞技场)记录")
    async def jjc_log_query(self, event: AstrMessageEvent):
        await self._record_route(event)
        text = event.message_str.strip()
        ret = re.search(r"(?:击剑|竞技场)记录 ?(\d)?", text)
        qid = str(event.get_sender_id())
        platform_id = get_platform_id(event)
        user_bind: List[PCRBind] = await pcr_sqla.get_bind(platform_id, qid)
        if not user_bind:
            yield event.plain_result("您还没有绑定竞技场！")
            event.stop_event()
            return
        pcrid_id_input = int(ret.group(1)) if ret and ret.group(1) else 0
        if pcrid_id_input > len(user_bind):
            yield event.plain_result("序号超出范围，请检查您绑定的竞技场列表")
            event.stop_event()
            return
        msg = "没有击剑记录！"
        name = await self._get_member_name(event, qid)
        target_pcrid = user_bind[pcrid_id_input - 1].pcrid if pcrid_id_input else 0
        if jjc_historys := await pcr_sqla.get_history(platform_id, qid, target_pcrid):
            msg = f"\t\t\t\t【{name}的击剑记录】\n" + "\n".join(
                [
                    f"{datetime.fromtimestamp(h.date).strftime('%H:%M:%S')} {h.name} {'jjc ' if not h.item else 'pjjc'}：{h.before}->{h.after} [{'▲' if h.before > h.after else '▽'}{abs(h.after-h.before)}]"
                    for h in jjc_historys
                ]
            )
        yield event.chain_result([_build_image_comp(await asyncio.to_thread(image_draw, msg))])
        event.stop_event()

    @filter.regex(r"(?<!(?:删除|清空))竞技场绑定")
    async def on_arena_bind(self, event: AstrMessageEvent):
        await self._record_route(event)
        text = event.message_str.strip()
        ret = re.search(r"竞技场绑定 ?(\d+) ?(\S+)?", text)
        if not ret:
            return
        nickname = ret.group(2) if ret.group(2) else ""
        if len(nickname) > 12:
            yield event.plain_result("昵称不能超过12个字，换个短一点的昵称吧~")
            event.stop_event()
            return
        pcr_id = int(ret.group(1))
        platform_id = get_platform_id(event)
        gid = str(event.get_group_id()) if event.get_group_id() else ""
        uid = str(event.get_sender_id())
        umo = event.unified_msg_origin or ""
        is_private = not bool(gid)

        yield event.plain_result(f"正在向服务器请求绑定 UID: {pcr_id}，请稍候...")

        await query_all(
            [PCRBind(platform=platform_id, pcrid=pcr_id, name=nickname, group=gid, user_id=uid)],
            platform_id,
            bind_pcrid,
            {
                "ev": event,
                "info": {
                    "platform": platform_id,
                    "pcrid": pcr_id,
                    "name": nickname,
                    "group": gid,
                    "group_umo": umo if gid else "",
                    "private_umo": umo if is_private else "",
                    "private": is_private,
                    "user_id": uid,
                },
            },
            Priority.bind.value,
        )
        event.stop_event()

    @filter.regex(r"删除竞技场绑定")
    async def delete_arena_sub(self, event: AstrMessageEvent):
        await self._record_route(event)
        text = event.message_str.strip()
        ret = re.search(r"删除竞技场绑定 ?(\d)?", text)
        if not ret or not ret.group(1):
            yield event.plain_result("输入格式不对！“删除竞技场绑定+【序号】”（序号不可省略）")
            event.stop_event()
            return
        qid = str(event.get_sender_id())
        platform_id = get_platform_id(event)
        user_bind: List[PCRBind] = await pcr_sqla.get_bind(platform_id, qid)
        pcrid_id = int(ret.group(1))
        if 0 < pcrid_id <= len(user_bind):
            delete_bind = user_bind[pcrid_id - 1]
            await pcr_sqla.delete_bind(qid, platform_id, delete_bind.pcrid)
            yield event.plain_result(f"您已成功删除：【{pcrid_id}】{delete_bind.name}（{delete_bind.pcrid}）")
        else:
            yield event.plain_result("输入的序号超出范围！")
        event.stop_event()

    @filter.regex(r"清空竞技场绑定")
    async def pcrjjc_del(self, event: AstrMessageEvent):
        await self._record_route(event)
        qid = str(event.get_sender_id())
        platform_id = get_platform_id(event)
        user_bind: List[PCRBind] = await pcr_sqla.get_bind(platform_id, qid)
        if not user_bind:
            yield event.plain_result("您还没有绑定竞技场！")
            event.stop_event()
            return
        reply = "删除成功！\n" + "\n".join([f"【{i+1}】{bind.name}\n（{bind.pcrid}）" for i, bind in enumerate(user_bind)])
        await pcr_sqla.delete_bind(qid, platform_id)
        yield event.plain_result(reply)
        event.stop_event()

    @filter.regex(r"竞技场修改昵称")
    async def change_nickname(self, event: AstrMessageEvent):
        await self._record_route(event)
        text = event.message_str.strip()
        ret = re.search(r"竞技场修改昵称 ?(\d)? (\S+)", text)
        if not ret:
            return
        qid = str(event.get_sender_id())
        platform_id = get_platform_id(event)
        user_bind: List[PCRBind] = await pcr_sqla.get_bind(platform_id, qid)
        if not user_bind:
            yield event.plain_result("您还没有绑定竞技场！")
            event.stop_event()
            return
        pcrid_num = len(user_bind)
        pcrid_id_str = ret.group(1)
        if not pcrid_id_str and pcrid_num != 1:
            yield event.plain_result("您绑定了多个uid，更改昵称时需要加上序号。")
            event.stop_event()
            return
        pcrid_id = int(pcrid_id_str) if pcrid_id_str else 1
        if len(ret.group(2)) > 12:
            yield event.plain_result("昵称不能超过12个字，换个短一点的昵称吧~")
            event.stop_event()
            return
        if pcrid_id > pcrid_num or not pcrid_num:
            yield event.plain_result("序号超出范围，请检查您绑定的竞技场列表")
            event.stop_event()
            return
        await pcr_sqla.update_bind(platform_id, {"name": ret.group(2)}, qid, user_bind[pcrid_id - 1].pcrid)
        yield event.plain_result("更改成功！")
        event.stop_event()
    @filter.regex(r"在本群推送")
    async def group_set(self, event: AstrMessageEvent):
        gid = event.get_group_id()
        if not gid:
            yield event.plain_result("请在群聊中使用此命令！")
            event.stop_event()
            return
        await pcr_sqla.update_bind(
            get_platform_id(event),
            {"group": str(gid), "group_umo": event.unified_msg_origin or "", "private": False},
            str(event.get_sender_id()),
        )
        yield event.plain_result("设置成功！已为您开启本群推送。")
        event.stop_event()

    @filter.regex(r"换私聊推送")
    async def private_notice(self, event: AstrMessageEvent):
        platform_id = get_platform_id(event)
        if await pcr_sqla.get_private(platform_id) >= private_dict[platform_id]:
            yield event.plain_result("私聊推送用户已达上限！")
            event.stop_event()
            return
        if event.get_group_id():
            yield event.plain_result("仅限好友私聊使用！")
            event.stop_event()
            return
        qid = str(event.get_sender_id())
        await pcr_sqla.update_bind(
            platform_id,
            {"private": True, "private_umo": event.unified_msg_origin or ""},
            qid,
        )
        yield event.plain_result("设置成功！已为您开启私聊推送。")
        event.stop_event()

    @filter.regex(r"竞技场设置 ?(开启|关闭)")
    async def set_noticeType(self, event: AstrMessageEvent):
        await self._record_route(event)
        text = event.message_str.strip()
        ret = re.search(r"竞技场设置 ?(开启|关闭) ?(jjc|pjjc|排名上升|上线提醒) ?(\d)?", text)
        if not ret:
            return
        qid = str(event.get_sender_id())
        platform_id = get_platform_id(event)
        user_bind: List[PCRBind] = await pcr_sqla.get_bind(platform_id, qid)
        if not user_bind:
            yield event.plain_result("您还没有绑定jjc，绑定方式：\n[竞技场绑定 uid] uid在pcr个人简介里")
            event.stop_event()
            return
        pcrid_num = len(user_bind)
        turn_on = str(ret.group(1)) == "开启"
        change = ret.group(2)
        pcrid_id = int(ret.group(3)) if ret.group(3) else (1 if pcrid_num == 1 else -1)
        if 0 <= pcrid_id <= pcrid_num:
            if change == "jjc":
                change_dict = {"jjc_notice": turn_on}
            elif change == "pjjc":
                change_dict = {"pjjc_notice": turn_on}
            elif change == "排名上升":
                change_dict = {"up_notice": turn_on}
            else:
                change_dict = {"online_notice": 1 if turn_on else 0}
            target_uid = user_bind[pcrid_id - 1].pcrid if pcrid_id else None
            await pcr_sqla.update_bind(platform_id, change_dict, qid, target_uid)
            yield event.plain_result("设置成功！")
        else:
            yield event.plain_result("序号超出范围或者绑定多个没填序号，请检查您绑定的竞技场列表")
        event.stop_event()
    @filter.regex(r"竞技场设置 ?\d{3,4}")
    async def set_allType(self, event: AstrMessageEvent):
        await self._record_route(event)
        text = event.message_str.strip()
        ret = re.search(r"竞技场设置 ?(\d{3,4}) ?(\d)?", text)
        if not ret:
            return
        qid = str(event.get_sender_id())
        platform_id = get_platform_id(event)
        user_bind: List[PCRBind] = await pcr_sqla.get_bind(platform_id, qid)
        if not user_bind:
            yield event.plain_result("您还没有绑定竞技场！")
            event.stop_event()
            return
        pcrid_num = len(user_bind)
        change = ret.group(1)
        pcrid_id = int(ret.group(2)) if ret.group(2) else (1 if pcrid_num == 1 else -1)
        if 0 <= pcrid_id <= pcrid_num:
            change_dict = {
                "jjc_notice": bool(int(change[0])),
                "pjjc_notice": bool(int(change[1])),
                "up_notice": bool(int(change[2])),
                "online_notice": int(change[3]) if len(change) > 3 else 0,
            }
            target_uid = user_bind[pcrid_id - 1].pcrid if pcrid_id else None
            await pcr_sqla.update_bind(platform_id, change_dict, qid, target_uid)
            yield event.plain_result("设置成功！")
        else:
            yield event.plain_result("序号超出范围或者绑定多个没填序号，请检查您绑定的竞技场列表")
        event.stop_event()

    @filter.regex(r"pcrjjc负载查询")
    async def load_query(self, event: AstrMessageEvent):
        if not self._is_plugin_admin(event):
            event.stop_event()
            return
        await self._record_route(event)
        load_data: LoadBase = await pcr_sqla.query_load()
        msg = f"""pcrjjc负载：
B服：
群聊用户数量：{load_data.b_group_user} 群聊绑定的uid：{load_data.b_group_pcrid}个
私聊用户数量：{load_data.b_private_user} 私聊绑定的uid：{load_data.b_private_pcrid}个
昨天推送次数：{load_data.b_yesterday_send} 今天推送次数：{load_data.b_today_send}
渠道服：
群聊用户数量：{load_data.qu_group_user} 群聊绑定的uid：{load_data.qu_group_pcrid}个
私聊用户数量：{load_data.qu_private_user} 私聊绑定的uid：{load_data.qu_private_pcrid}个
昨天推送次数：{load_data.qu_yesterday_send} 今天推送次数：{load_data.qu_today_send}"""
        yield event.chain_result([_build_image_comp(await asyncio.to_thread(image_draw, msg))])
        event.stop_event()

    @filter.regex(r"pcrjjc关闭私聊推送")
    async def no_private(self, event: AstrMessageEvent):
        if not self._is_plugin_admin(event):
            event.stop_event()
            return
        await self._record_route(event)
        await pcr_sqla.update_bind(get_platform_id(event), {"private": False})
        yield event.plain_result("所有设置为私聊推送的用户的推送已关闭！")
        event.stop_event()

    @filter.regex(r"pcrjjc删除绑定")
    async def del_binds(self, event: AstrMessageEvent):
        if not self._is_plugin_admin(event):
            event.stop_event()
            return
        await self._record_route(event)
        ret = re.search(r"pcrjjc删除绑定 ?(\S+)", event.message_str.strip())
        if not ret:
            return
        qid = str(ret.group(1)).strip()
        platform_id = get_platform_id(event)
        user_bind: List[PCRBind] = await pcr_sqla.get_bind(platform_id, qid)
        if user_bind:
            await pcr_sqla.delete_bind(qid, platform_id)
            yield event.plain_result("删除成功！")
        else:
            yield event.plain_result("绑定列表中找不到这个用户特征码！")
        event.stop_event()

    @filter.regex(r"pcrjjc换设备id")
    async def update_device_id(self, event: AstrMessageEvent):
        if not self._is_plugin_admin(event):
            event.stop_event()
            return
        await self._record_route(event)
        init_device_id(clear_id=True)
        yield event.plain_result("pcrjjc更新设备id成功！重载插件生效新设备id")
        event.stop_event()
    @filter.regex(r"详细查询")
    async def on_query_arena_all(self, event: AstrMessageEvent):
        await self._record_route(event)
        text = event.message_str.strip()
        ret = re.search(r"详细查询 ?(\d+)?", text)
        if not ret:
            return
        target_id = ret.group(1)

        qid = str(event.get_sender_id())
        platform_id = get_platform_id(event)
        user_bind: List[PCRBind] = await pcr_sqla.get_bind(platform_id, qid)

        if not target_id:
            if not user_bind:
                yield event.plain_result("您还未绑定竞技场")
                event.stop_event()
                return
            elif len(user_bind) == 1:
                target_id = "1"
            else:
                yield event.plain_result("您绑定了多个账号，请在详细查询后带上uid或编号")
                event.stop_event()
                return

        if len(target_id) > 1:
            # 原实现把「位数 > 1」一律当 UID 直接发给服务器：
            #   · 2~12 位的数字（序号打错、少输几位）会白白消耗一次查询小号配额，
            #     还回一句误人歧途的「账号不存在」；
            #   · 13 个 0 会被 int() 归零，反而真的去查 UID=0。
            # 这里改按「国服 UID 固定 13 位」判类型，与竞技场查询的处理保持一致。
            if len(target_id) != 13:
                yield event.plain_result("位数不对，国服 uid 是 13 位的；查询自己绑定的账号请用 1 起的序号")
                event.stop_event()
                return
            if int(target_id) <= 0:
                yield event.plain_result("uid 不合法，请检查输入")
                event.stop_event()
                return
            bind = PCRBind(platform=platform_id, pcrid=int(target_id))
        else:
            if not user_bind:
                yield event.plain_result("您还未绑定竞技场")
                event.stop_event()
                return
            if int(target_id) < 1:
                # 原实现缺 0 处理：user_bind[0-1] 会静默查到最后一个绑定（同类指令里唯一漏掉 0 的一个）
                yield event.plain_result(f"序号不能为 0，请输入 1~{len(user_bind)} 的序号，或直接带上 13 位 uid")
                event.stop_event()
                return
            if len(user_bind) < int(target_id):
                yield event.plain_result("输入的序号超出范围，可发送竞技场查询查看你的绑定")
                event.stop_event()
                return
            bind = user_bind[int(target_id) - 1]
        await query_all([bind], platform_id, detial_query, {"ev": event, "platform": platform_id}, Priority.detial_query.value)
        event.stop_event()

    @filter.regex(r"深域查询")
    async def on_query_talent(self, event: AstrMessageEvent):
        await self._record_route(event)
        text = re.sub(r".*?深域查询", "", event.message_str.strip()).strip()
        uid_args = text.split()
        if not uid_args or any(not uid.isdigit() for uid in uid_args):
            yield event.plain_result("格式：深域查询 uid1 uid2 ...（渠道服请加“渠”字前缀）")
            event.stop_event()
            return
        # 单条指令的 UID 上限：防止一条消息就把查询小号的配额刷完（P2-5）
        if len(uid_args) > TALENT_MAX_UIDS:
            yield event.plain_result(f"一次最多查 {TALENT_MAX_UIDS} 个 uid（本次输入 {len(uid_args)} 个），请分批查询")
            event.stop_event()
            return
        uids = list(dict.fromkeys(int(uid) for uid in uid_args))
        platform_id = get_platform_id(event)
        query_list = [PCRBind(platform=platform_id, pcrid=uid) for uid in uids]
        batch = next(_batch_seq)
        _reset_batch_cache(talent_cache, str(event.get_sender_id()), batch)
        await query_all(
            query_list,
            platform_id,
            talent_query,
            {"ev": event, "query_count": len(query_list), "batch": batch},
            Priority.query_talent.value,
        )
        event.stop_event()

    @filter.regex(r"(?:竞技场换头像框|更换竞技场头像框|更换头像框)")
    async def change_frame(self, event: AstrMessageEvent):
        await self._record_route(event)
        user_id = str(event.get_sender_id())
        frame_tmp = re.sub(r".*?(?:竞技场换头像框|更换竞技场头像框|更换头像框)", "", event.message_str.strip()).strip()
        path = os.path.join(_base_dir, "img/img/frame/")
        frame_list = os.listdir(path) if os.path.exists(path) else []
        if not frame_list:
            yield event.plain_result("img/img/frame/路径下没有任何头像框，请联系维护组检查目录")
            event.stop_event()
            return
        if frame_tmp not in frame_list:
            msg = "文件名输入错误，命令样例：\n更换头像框 color.png\n目前可选文件有：\n" + "\n".join(frame_list)
            yield event.plain_result(msg)
            event.stop_event()
            return
        with open(_frame_json_path, "r", encoding="UTF-8") as f:
            f_data = load(f)
        f_data["customize"][user_id] = frame_tmp
        with open(_frame_json_path, "w", encoding="UTF-8") as rf:
            dump(f_data, rf, indent=4, ensure_ascii=False)
        frame_path = os.path.join(_base_dir, f"img/img/frame/{frame_tmp}")
        yield event.chain_result([
            Comp.Plain(f"已成功选择头像框:{frame_tmp}"),
            Comp.Image.fromFileSystem(os.path.abspath(frame_path)),
        ])
        event.stop_event()

    @filter.regex(r"(?:查竞技场头像框|查询竞技场头像框|查询头像框)")
    async def see_a_see_frame(self, event: AstrMessageEvent):
        await self._record_route(event)
        user_id = str(event.get_sender_id())
        with open(_frame_json_path, "r", encoding="UTF-8") as f:
            f_data = load(f)
        frame_tmp = f_data["customize"].get(user_id, f_data["default_frame"])
        path = os.path.join(_base_dir, f"img/img/frame/{frame_tmp}")
        yield event.chain_result([Comp.Image.fromFileSystem(os.path.abspath(path))])
        event.stop_event()

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_group_leave(self, event: AstrMessageEvent):
        raw = getattr(event.message_obj, "raw_message", None)
        if isinstance(raw, dict) and raw.get("post_type") == "notice" and raw.get("notice_type") == "group_decrease":
            uid = str(raw.get("user_id", ""))
            gid = str(raw.get("group_id", ""))
            if uid and gid and await pcr_sqla.get_bind(user_id=uid, group=gid):
                await pcr_sqla.delete_bind(uid, group=gid)
                yield event.plain_result(f"{uid}退群了，已自动删除其绑定在本群的竞技场订阅推送")
                event.stop_event()
