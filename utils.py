import asyncio
import base64
from bisect import bisect_right
from copy import deepcopy
import csv
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from io import BytesIO
from pathlib import Path
import re
import traceback
from typing import List

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
import astrbot.api.message_components as Comp
from astrbot.api.star import Context

from .database.dal import JJCHistory, pcr_sqla, PCRBind
from .img.create_img import generate_info_pic
from .img.text2img import image_draw
from .query import query_all, drain_recovered_platforms, maintenance_remaining
from .var import (
    NoticeType,
    Platform,
    platform_dict,
    query_cache,
    talent_cache,
    cache,
    lck,
    jjc_log,
)

TALENT_QUEST_NAME = ("火", "水", "风", "光", "暗")

# 同一个推送目标（群/私聊路由）连续推送失败多少次就自动解绑（P2-1）：
# Matrix/Telegram 没有退群/离房事件上抛到插件层，机器人被移出房后绑定会
# 静默堆积，每轮轮询都刷一条「主动推送失败」。靠失败计数自动清理。
PUSH_FAIL_LIMIT = 3

# ===================== 推送分发（查询与推送解耦） =====================
# 旧实现里 sendNotice 直接 await 平台发送：查询 worker 必须等一条消息真正发出去
# 才能回队列取下一个 UID，平台的网络抖动/限流会被算进查询吞吐，号越多越慢。
# 现在查询侧只把变动 put_nowait 进队列，由独立的 _notice_dispatcher 协程负责发送：
#   · 同一路由（umo）在一个窗口内的多条变动合并成一条纯文本消息（不转图），
#     群里 20 个人同时变排名从 20+ 次平台调用降到 1 次；
#   · 群聊 @ 按用户特征码去重后统一放在消息末尾，同一个人双榜+上线同时变动只 @ 一次；
#   · 单条消息最多合并 NOTICE_MAX_ENTRIES 条变动、同一目标单次最多发
#     NOTICE_MAX_MESSAGES 条，避免超长文本被 QQ 侧吞掉或触发风控。
NOTICE_WINDOW = 0.8          # 聚合窗口（秒）：把同一时间片内的变动一起收走
NOTICE_MAX_ENTRIES = 8       # 单条消息最多合并的变动条数
NOTICE_MAX_MESSAGES = 5      # 同一目标单次分发最多发出的消息条数（超出部分只留历史记录）

# 支持 At 组件的平台前缀（取自 unified_msg_origin 的第一段，即适配器名）
_AT_SUPPORTED_PREFIX = ("aiocqhttp",)

_notice_queue = None         # 变动队列（由 start_notice_dispatcher 创建）
_notice_task = None          # 分发协程

# umo -> 连续推送失败次数（内存计数，发送成功即清零；插件重载后归零，
# 不影响已落库的绑定；轮询频率下误判需要连续三轮失败，容忍了瞬时网络抖动）
_push_fail_counts: dict = {}

_bot_context: Context = None

# 维护结束后的管理员通知回调（由 main.py 注入，避免 query -> utils 循环导入）
_recovery_notifier = None


def set_recovery_notifier(func):
    global _recovery_notifier
    _recovery_notifier = func


def init_context(context: Context, platform_id: str = ""):
    global _bot_context
    _bot_context = context


# 详细查询成品图的编码格式：模板本身是 RGB（无透明通道），PNG 的无损压缩纯属浪费。
# 实测同一张 603×1097 成品图：PNG 58.6ms / 279KB（base64 后约 372KB），
# JPEG(q92) 6ms / 约 110KB——省 10 倍 CPU、约 3 倍上传带宽。
PIC_JPEG_QUALITY = 92


def pic2b64(pic) -> str:
    buf = BytesIO()
    # 兜底：万一将来又往模板上贴了带 alpha 的图层，先转 RGB 再存 JPEG
    if pic.mode not in ("RGB", "L"):
        pic = pic.convert("RGB")
    pic.save(buf, format="JPEG", quality=PIC_JPEG_QUALITY)
    base64_str = base64.b64encode(buf.getvalue()).decode()
    return f"base64://{base64_str}"


def filt_message(text: str) -> str:
    text = re.sub(r"\[CQ:[^\]]+\]", "", str(text))
    return text.replace("\r", "").replace("\n", " ").strip()


def _build_image_comp(pic_str: str) -> Comp.Image:
    if pic_str.startswith("base64://"):
        return Comp.Image.fromBase64(pic_str[9:])
    return Comp.Image.fromFileSystem(pic_str)


@lru_cache(maxsize=128)
def _knight_rank_exp():
    with open(
        Path(__file__).with_name("rank_exp.csv"), encoding="utf-8", newline=""
    ) as fp:
        rows = csv.reader(fp)
        next(rows)
        return tuple(int(row[1]) for row in rows)


def read_knight_exp_rank(target_value: int) -> int:
    return max(1, bisect_right(_knight_rank_exp(), target_value))


class ApiException(Exception):
    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


# 消息开头可能出现的“非指令噪音”：引用回复残留的 [xxx]、@昵称、冒号/全角空格等。
# 它们出现在“渠”字之前时，旧实现只看 message_str[0]，会把“@某人 渠竞技场查询”
# 当成 B 服处理，用户会收到莫名的“UID 不存在”（P2-2）。
_LEAD_NOISE_RE = re.compile(
    r"^(?:\[[^\]]{0,64}\]|[＠@][^\s]{1,64}[\s:：,，、]*|[\s:：,，、]+)+"
)


def get_platform_id(event: AstrMessageEvent) -> int:
    """判断本条指令查的是 B 服（0）还是渠道服（1）。

    渠道服的写法是“在指令前加一个「渠」字”，所以只认规范化后的首字符；
    先把 @ / 引用 / 全角空格等前置噪音剥掉再判断。
    """
    info: str = _LEAD_NOISE_RE.sub("", filt_message(event.message_str))
    if not info:
        return Platform.b_id.value
    return platform_dict.get(info[0], Platform.b_id.value)


def get_qid(event: AstrMessageEvent) -> str:
    return str(event.get_sender_id())


async def _send_reply(event: AstrMessageEvent, chain: list):
    if _bot_context:
        await _bot_context.send_message(event.unified_msg_origin, MessageChain(chain))


async def _proactive_send_by_umo(umo: str, chain: list, private: bool = False):
    """通过保存的通用路由标识 unified_msg_origin 进行全平台主动推送。

    附带连续失败计数（P2-1）：同一目标连续 PUSH_FAIL_LIMIT 次失败后，
    自动解除走该路由的订阅并记日志，解决 Matrix/Telegram 被移出房后
    后台反复报错、绑定静默堆积的问题。
    """
    if not _bot_context or not umo:
        return
    try:
        await _bot_context.send_message(umo, MessageChain(chain))
    except Exception as e:
        logger.error(f"[pcrjjc] 主动推送失败 ({umo}): {e}")
        fails = _push_fail_counts[umo] = _push_fail_counts.get(umo, 0) + 1
        if fails >= PUSH_FAIL_LIMIT:
            await _unbind_dead_route(umo, private)
        return
    _push_fail_counts.pop(umo, None)  # 成功一次就重新开始计数（“连续”语义）


async def _unbind_dead_route(umo: str, private: bool):
    """推送目标已死：解除所有指向该路由的订阅并清理内存缓存（P2-1）。"""
    _push_fail_counts.pop(umo, None)
    try:
        binds = await pcr_sqla.get_bind_by_umo(umo, private)
        for bind in binds:
            await pcr_sqla.delete_bind(bind.user_id, bind.platform, bind.pcrid)
            cache.pop((bind.pcrid, bind.user_id, bind.platform), None)
            logger.warning(
                f"[pcrjjc] 推送目标 {umo} 连续 {PUSH_FAIL_LIMIT} 次失败，"
                f"已自动解除 {'私聊' if bind.private else '群聊'} 订阅："
                f"{bind.name}（{bind.pcrid}），用户 {bind.user_id}；如仍需推送请重新绑定"
            )
    except Exception:
        logger.error(f"[pcrjjc] 自动清理失效推送路由失败 ({umo}):\n{traceback.format_exc()}")


def _enqueue_notice(umo: str, text: str, user_id: str, private: bool) -> None:
    """把一条变动投进推送队列（不阻塞查询 worker）。

    jjc_log / 失败计数都不在这里动：历史记录仍由 sendNotice 同步写入，
    语义与改造前一致（is_send 表示“决定推送”，不是“推送成功”）。
    """
    queue = _notice_queue
    if queue is None:
        logger.warning(f"[pcrjjc] 推送队列未就绪，本次变动未推送：{text.splitlines()[0]}")
        return
    queue.put_nowait((umo, private, str(user_id or ""), text))


def _supports_at(umo: str) -> bool:
    """该推送路由所在的平台能否使用 At 组件。

    只有 OneBot 系（QQ）适配器支持按 QQ 号 @；Matrix / Telegram 等平台
    碰到 At 组件可能整条消息发送失败，而发送连续失败会触发 P2-1 的
    “自动解除订阅”——那是直接删用户绑定，代价太大。所以其它平台只发纯文本。
    """
    return str(umo or "").split(":", 1)[0].strip().lower() in _AT_SUPPORTED_PREFIX


def _build_notice_chains(items: list, private: bool, umo: str = "") -> list:
    """把同一路由的变动切成待发消息：[(user_id, text), ...] -> [[Comp, ...], ...]

    纯文本拼接，不渲染图片（后台监控用图既慢又多占带宽，查图看「详细查询」）。
    """
    total = len(items)
    chunks = [items[i:i + NOTICE_MAX_ENTRIES] for i in range(0, total, NOTICE_MAX_ENTRIES)]
    dropped = 0
    if len(chunks) > NOTICE_MAX_MESSAGES:
        # 单次爆发过多时只发前几条，剩下的仍在数据库里（可发“竞技场记录”查）
        dropped = total - sum(len(c) for c in chunks[:NOTICE_MAX_MESSAGES])
        chunks = chunks[:NOTICE_MAX_MESSAGES]

    chains = []
    for idx, chunk in enumerate(chunks):
        text = "\n".join(t for _, t in chunk)
        if dropped and idx == len(chunks) - 1:
            text += f"\n…另有 {dropped} 条变动未展开，可发送「竞技场记录」查看"
        chain = [Comp.Plain(text)]
        if not private and _supports_at(umo):
            # 末尾统一 @：同一个人 jjc/pjjc/上线同时变动只 @ 一次。
            # 只 @ 纯数字特征码：非数字（如 Matrix 的 @用户:域名）拼进 At 组件
            # 会被平台判为非法参数，反而让整条消息发不出去。
            seen = list(
                dict.fromkeys(uid for uid, _ in chunk if uid and str(uid).isdigit())
            )
            if seen:
                try:
                    ats = [Comp.At(qq=uid) for uid in seen]
                except Exception:
                    chain.append(Comp.Plain(" " + " ".join(f"@{uid}" for uid in seen)))
                else:
                    chain.extend(ats)
        chains.append(chain)
    return chains


async def _flush_notice_batch(jobs: list):
    """按 (umo, private) 分桶，逐桶合并发送。"""
    buckets: dict = {}
    for umo, private, user_id, text in jobs:
        buckets.setdefault((umo, private), []).append((user_id, text))
    for (umo, private), items in buckets.items():
        for chain in _build_notice_chains(items, private, umo):
            await _proactive_send_by_umo(umo, chain, private=private)


async def _collect_notice_batch(queue) -> list:
    """取一条，再在窗口内尽可能多地收走同一时间片的变动。"""
    jobs = [await queue.get()]
    loop = asyncio.get_running_loop()
    deadline = loop.time() + NOTICE_WINDOW
    while True:
        remain = deadline - loop.time()
        if remain <= 0:
            break
        try:
            jobs.append(await asyncio.wait_for(queue.get(), remain))
        except asyncio.TimeoutError:
            break
    return jobs


async def _notice_dispatcher():
    """后台推送协程：合并发送，失败也不会打断外层的查询轮询。"""
    queue = _notice_queue
    while True:
        try:
            jobs = await _collect_notice_batch(queue)
            if jobs:
                await _flush_notice_batch(jobs)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error(f"[pcrjjc] 推送分发异常：\n{traceback.format_exc()}")
            await asyncio.sleep(1)


def start_notice_dispatcher():
    """启动推送分发协程（main.py 启动时调用，必须早于轮询启动）。"""
    global _notice_queue, _notice_task
    # 重建队列：丢弃热重载前残留的待发变动，也避开旧事件循环的队列对象
    _notice_queue = asyncio.Queue()
    _notice_task = asyncio.create_task(_notice_dispatcher())
    return _notice_task


async def stop_notice_dispatcher():
    """停止分发协程；队列里尚未发出的变动直接丢弃（插件正在卸载）。"""
    global _notice_queue, _notice_task
    task, _notice_task = _notice_task, None
    queue, _notice_queue = _notice_queue, None
    if task and not task.done():
        task.cancel()
        # 等它真的退出，避免插件卸载/热重载时留着 pending task
        try:
            await asyncio.wait({task}, timeout=1.0)
        except asyncio.CancelledError:
            raise
    if queue is not None and not queue.empty():
        logger.warning(f"[pcrjjc] 插件停止，丢弃 {queue.qsize()} 条尚未推送的变动")
        while not queue.empty():
            queue.get_nowait()


async def _flush_recovery_notices():
    """把「维护已恢复」的平台交给 main.py 发管理员通知。"""
    if _recovery_notifier is None:
        return
    for platform in drain_recovered_platforms():
        try:
            await _recovery_notifier(platform)
        except Exception:
            logger.error(f"[pcrjjc] 维护恢复通知发送失败：\n{traceback.format_exc()}")


# ===================== 分片轮询 =====================
# 旧实现每轮把该平台的全部绑定一次性入队，单轮周期 = 全量绑定耗时 + 30s：
# 绑定越多周期越长，一条要重试 3 次（1s+3s 退避）的慢任务还会把整轮拖住，
# 结果是“号越多，每个号被刷新的间隔越久”。改成按 id 轮转取一片：
#   · 绑定数 ≤ POLL_CHUNK 时行为与改造前完全一致（小部署不会变慢）；
#   · 超过时每轮只查 POLL_CHUNK 条，周期有上界，所有绑定仍会按轮转顺序被查到。
POLL_CHUNK = 80

_poll_cursor: dict = {}      # platform -> 下一片在该平台绑定列表中的起点


async def _next_poll_chunk(platform: int, total: int) -> list:
    """取本轮要查的一片绑定（按 id 顺序，结果与改造前逐条一致）。

    分片逻辑本身不变，只是把「取表 + 切片」换成「SQL 侧 ORDER BY ... LIMIT/OFFSET」：
    原实现每 30 秒把该平台的整张绑定表读成 ORM 对象，再丢掉 (总数 - 80) 条；
    绑定数上千时这是每轮一次的全表扫描 + 上千个临时对象。
    """
    if total <= POLL_CHUNK:
        _poll_cursor.pop(platform, None)
        return await pcr_sqla.get_bind_page(platform, 0, POLL_CHUNK)
    start = _poll_cursor.get(platform, 0) % total
    chunk = await pcr_sqla.get_bind_page(platform, start, POLL_CHUNK)
    if len(chunk) < POLL_CHUNK:
        # 尾部不足一片时从列表开头补齐，保证每轮查询量稳定
        chunk = chunk + await pcr_sqla.get_bind_page(platform, 0, POLL_CHUNK - len(chunk))
    _poll_cursor[platform] = (start + POLL_CHUNK) % total
    return chunk


# ===================== 历史记录保留期 =====================
# 击剑记录只增不减会让 SQLite 文件和所有历史查询一起变慢 / 变大：
# 保留 N 天（WebUI 可配，0 = 永久保留），每天最多清理一次。
HISTORY_KEEP_DAYS_DEFAULT = 90

_history_keep_getter = None      # 由 main.py 注入，返回保留天数
_last_prune_day = None           # 上次清理的（北京）日期，避免每轮都去查库


def set_history_keep_getter(func):
    """注入历史记录保留天数的读取函数（main.py 在 __init__ 里调用）。"""
    global _history_keep_getter
    _history_keep_getter = func


def _history_keep_days() -> int:
    """读取保留天数；配置异常或非法值一律回落默认值（绝不能返回负数）。"""
    if _history_keep_getter is None:
        return HISTORY_KEEP_DAYS_DEFAULT
    try:
        days = int(_history_keep_getter())
    except Exception:
        return HISTORY_KEEP_DAYS_DEFAULT
    if days <= 0:
        return 0
    return min(days, 3650)


async def _maybe_prune_history():
    """每天最多清理一次过期击剑记录。"""
    global _last_prune_day
    day = _beijing_now().date()
    if _last_prune_day == day:
        return
    # 先占位再执行：数据库只读时也只会在当天失败一次，不会每 30 秒重试刷日志
    _last_prune_day = day
    days = _history_keep_days()
    if days <= 0:
        return
    cutoff = int(_beijing_now().timestamp()) - days * 86400
    try:
        removed = await pcr_sqla.prune_history(cutoff)
    except Exception:
        logger.warning(f"[pcrjjc] 清理过期击剑记录失败（不影响查询）：\n{traceback.format_exc()}")
        return
    if removed:
        logger.info(f"[pcrjjc] 已清理 {removed} 条早于 {days} 天前的击剑记录")


# ===================== 结算时段加速轮询 =====================
# 国服 JJC / PJJC 的排名奖励每天 15:00（北京时间）结算，14:50~15:05 是排名变动
# 最密集、也最值得盯的窗口。全天统一 30s 太粗，但全天提速会把请求量放大数倍，
# 绑定 UID 多的部署很容易招来风控。折中：只在这个固定窗口内改用 WebUI 可配的
# 间隔（默认 10s，硬性限 5~30s），窗口外仍然是 30s。
PEAK_WINDOW_START = (14, 50)     # 北京时间，含该分钟
PEAK_WINDOW_END = (15, 5)        # 北京时间，含该分钟
POLL_INTERVAL_NORMAL = 30        # 常规轮询间隔（秒）
POLL_INTERVAL_DEFAULT = 10       # 结算时段默认间隔（秒）
POLL_INTERVAL_MIN = 5            # 下限：再快就接近“每心跳一次请求”的脚本特征
POLL_INTERVAL_MAX = 30           # 上限：比常规更慢对这个窗口没有意义

_peak_config_getter = None       # 由 main.py 注入，返回 (是否开启, 期望间隔)
_peak_active = None              # 上次判定结果，仅在“进/出窗口”时打一条日志


def set_peak_config_getter(func):
    """注入配置读取函数（main.py 在 __init__ 里调用）。

    用 getter 而不是保存一份快照：WebUI 保存配置后即使不重载插件，
    下一轮轮询也能立刻读到新值。
    """
    global _peak_config_getter
    _peak_config_getter = func


def _beijing_now() -> datetime:
    """当前北京时间（显式带时区，不依赖宿主机时区设置）。"""
    return datetime.now(timezone(timedelta(hours=8)))


def _as_bool(value) -> bool:
    """宽松布尔判定。

    WebUI 在部分场景会把 bool 存成字符串，直接用 bool("false") 会得到 True，
    所以字符串要单独归一化。
    """
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "0", "no", "off", "")
    return bool(value)


def _clamp_peak_interval(value) -> int:
    """把 WebUI 填的间隔收进 [5, 30]；非法值回落默认 10s。"""
    try:
        interval = int(value)
    except (TypeError, ValueError):
        return POLL_INTERVAL_DEFAULT
    if interval <= 0:
        # 0/负数在语义上只能是“填错了”，不能理解成“不限间隔”，否则会变成狂刷
        return POLL_INTERVAL_DEFAULT
    return max(POLL_INTERVAL_MIN, min(POLL_INTERVAL_MAX, interval))


def current_poll_interval() -> int:
    """返回本轮轮询结束后应休眠的秒数。

    命中北京时间 14:50~15:05 且开关打开时返回 WebUI 配置的间隔，其余时间
    恒为 30s。配置读取异常一律回落 30s——绝不能让配置问题把间隔变成 0。
    """
    global _peak_active
    enabled, raw_interval = True, POLL_INTERVAL_DEFAULT
    if _peak_config_getter is not None:
        try:
            enabled, raw_interval = _peak_config_getter()
        except Exception:
            logger.debug(f"[pcrjjc] 读取结算时段轮询配置失败，按常规间隔处理：{traceback.format_exc()}")
            return POLL_INTERVAL_NORMAL

    interval = _clamp_peak_interval(raw_interval)
    now = _beijing_now()
    cur = now.hour * 60 + now.minute
    start = PEAK_WINDOW_START[0] * 60 + PEAK_WINDOW_START[1]
    end = PEAK_WINDOW_END[0] * 60 + PEAK_WINDOW_END[1]
    active = _as_bool(enabled) and start <= cur <= end

    if active != _peak_active:
        first = _peak_active is None
        _peak_active = active
        if active:
            logger.info(
                f"[pcrjjc] 进入结算加速时段（北京时间 "
                f"{PEAK_WINDOW_START[0]:02d}:{PEAK_WINDOW_START[1]:02d}~"
                f"{PEAK_WINDOW_END[0]:02d}:{PEAK_WINDOW_END[1]:02d}），"
                f"轮询间隔调整为 {interval}s"
            )
        elif not first:
            logger.info(f"[pcrjjc] 结算加速时段结束，轮询间隔恢复 {POLL_INTERVAL_NORMAL}s")

    return interval if active else POLL_INTERVAL_NORMAL


async def query_loop(platform: int):
    while True:
        try:
            # 维护退避期内直接挂起，不入队任何任务。
            # 这是把「每 30s × 每个绑定各登一次」降为「每次退避 1 次探测」的关键。
            if remain := maintenance_remaining(platform):
                await asyncio.sleep(min(remain, 60))
                continue

            await _flush_recovery_notices()

            # 历史表保留期清理（每天最多一次，见 _maybe_prune_history）
            await _maybe_prune_history()

            total = await pcr_sqla.count_bind(platform)
            if not total:
                _poll_cursor.pop(platform, None)
                await asyncio.sleep(60)
                continue

            # 分片：绑定数超过 POLL_CHUNK 时每轮只取一片，让轮询周期有上界
            binds = await _next_poll_chunk(platform, total)

            # 轮询任务不需要失败通知，也不占用用户侧的失败重试语义
            if sleep_time := await query_all(binds, platform, query_rank, notify_on_error=False):
                await asyncio.sleep(sleep_time)
            else:
                # 结算时段（北京时间 14:50~15:05）用 WebUI 配置的间隔加速，其余时间 30s
                await asyncio.sleep(current_poll_interval())

            await pcr_sqla.insert_history(jjc_log[platform])
            jjc_log[platform].clear()
        except Exception:
            logger.error(traceback.format_exc())
            await asyncio.sleep(10)


async def query_rank(data):
    global cache
    try:
        info = data["res"]["user_info"]
    except Exception:
        return
    bind: PCRBind = data["bind_info"]
    res = [
        int(info["arena_rank"]),
        int(info["grand_arena_rank"]),
        int(info["last_login_time"]),
    ]
    key = (bind.pcrid, bind.user_id, bind.platform)
    if key not in cache:
        cache[key] = res
    else:
        last = deepcopy(cache[key])
        cache[key][0] = res[0]
        cache[key][1] = res[1]
        cache[key][2] = res[2]
        if res[0] != last[0]:
            await sendNotice(res[0], last[0], bind, NoticeType.jjc.value)
        if res[1] != last[1]:
            await sendNotice(res[1], last[1], bind, NoticeType.pjjc.value)
        if res[2] != last[2]:
            await sendNotice(res[2], last[2], bind, NoticeType.online.value)
async def detial_query(data):
    res = data["res"]
    event: AstrMessageEvent = data["ev"]
    pcrid = data["uid"]
    platfrom = data["platform"]
    if data.get("error"):
        await _send_reply(event, [Comp.Plain(f"详细查询失败（{pcrid}）：{data['error']}")])
        return
    try:
        logger.info("开始生成竞技场查询图片...")
        # 头像框以聊天平台用户 ID 为键（main.py 的“更换头像框”写的就是 sender_id），
        # 与作为查询目标的 pcrid 分开传入
        frame_user_id = str(event.get_sender_id())
        # 只出账号基础信息图；支援图与深域图（support.png/background.png 那套）已按需求移除
        result_image = await generate_info_pic(res, pcrid, platfrom, frame_user_id)

        chain = [
            _build_image_comp(pic2b64(result_image)),
        ]

        logger.info("竞技场查询图片已准备完毕！")
        await _send_reply(event, chain)
    except Exception as e:
        # 原实现只捕 ApiException：图片渲染中的 KeyError/IndexError 会漏出导致整条静默无回复
        logger.error(traceback.format_exc())
        await _send_reply(event, [Comp.Plain(f"详细查询出错：{e}")])


async def user_query(data: dict):
    global lck
    pcrid = data["uid"]
    info = data["info"]
    platfrom = data["platform"]
    if data.get("error"):
        idx = info.get(pcrid, 0)
        query = f"【{idx + 1:02d}】查询失败：{data['error']}\n\n"
        await _merge_query_result(data, pcrid, query, info)
        return
    try:
        res = data["res"]["user_info"]
        last_login = datetime.fromtimestamp(int(res["last_login_time"])).strftime("%Y-%m-%d %H:%M:%S")
        jjc_up, grand_jjc_up = await pcr_sqla.get_up_num(platfrom, pcrid, int(datetime.now().timestamp()))

        jjc_group_str = str(res["arena_group"]).zfill(3)
        pjjc_group_str = str(res["grand_arena_group"]).zfill(3)

        query = f'【{info[pcrid]+1:02d}】\n'
        query += f'昵称：{filt_message(str(res["user_name"]))}\n'
        query += f'jjc【{jjc_group_str}】排名：{res["arena_rank"]}\n'
        query += f'pjjc【{pjjc_group_str}】排名：{res["grand_arena_rank"]}\n'
        query += f'上升：{jjc_up}次 / {grand_jjc_up}次\n'
        query += f'最后登录：{last_login}\n\n'
    except Exception:
        logger.error(traceback.format_exc())
        query = "查询失败\n\n"

    await _merge_query_result(data, pcrid, query, info)


async def _merge_query_result(data: dict, pcrid: int, query: str, info: dict):
    """按批次聚合简讯结果；用 dict 以 pcrid 为键，同一 UID 重复通知只覆盖不累加。

    锁内只做字典操作：出图（同步 CPU 活，已挪到线程池）与发送（等平台回执）
    都不再占用全局锁，否则一个用户的发送慢会串行拖住所有用户的聚合回复。
    """
    final_text = None
    async with lck:
        event: AstrMessageEvent = data["ev"]
        key = (str(event.get_sender_id()), data.get("batch"))
        bucket: dict = query_cache.get(key)
        if bucket is None:
            return
        bucket[pcrid] = query
        if len(bucket) == len(info):
            query_cache.pop(key, None)
            ordered = [bucket[p] for p in sorted(bucket, key=lambda p: info[p])]
            final_text = "".join(ordered).strip()
    if final_text is None:
        return
    pic = await asyncio.to_thread(image_draw, final_text)
    await _send_reply(event, [_build_image_comp(pic)])


async def talent_query(data: dict):
    uid = data["uid"]
    event: AstrMessageEvent = data["ev"]
    sender_id = str(event.get_sender_id())
    if data.get("error"):
        await _merge_talent_result(data, uid, f"{uid} 查询失败：{data['error']}\n\n")
        return
    try:
        res = data["res"]
        user_info = res["user_info"]
        talent_quest = res["quest_info"]["talent_quest"]
        talent_progress = []
        for quest in talent_quest:
            clear_count = int(quest["clear_count"])
            stage = (clear_count - 1) // 10 + 1
            level = clear_count % 10 or 10
            talent_name = TALENT_QUEST_NAME[int(quest["talent_id"]) - 1]
            talent_progress.append(f"{talent_name}: {stage}-{level}")
        query = (
            f'{filt_message(str(user_info["user_name"]))}\n'
            f'公主骑士等级{read_knight_exp_rank(int(user_info["princess_knight_rank_total_exp"]))}\n'
            f'深域：{" / ".join(talent_progress)}\n\n'
        )
    except Exception:
        logger.error(traceback.format_exc())
        query = f"{uid}查询失败或该服务器暂不支持深域查询\n\n"

    await _merge_talent_result(data, uid, query)


async def _merge_talent_result(data: dict, uid: int, query: str):
    """按批次聚合深域结果，同样以 uid 为键保证幂等。

    与简讯聚合一致：出图/发送放到锁外，锁内只更新字典。
    """
    final_text = None
    async with lck:
        event: AstrMessageEvent = data["ev"]
        key = (str(event.get_sender_id()), data.get("batch"))
        bucket: dict = talent_cache.get(key)
        if bucket is None:
            return
        bucket[uid] = query
        if len(bucket) == data["query_count"]:
            talent_cache.pop(key, None)
            final_text = "".join(bucket.values())
    if final_text is None:
        return
    pic = await asyncio.to_thread(image_draw, final_text)
    await _send_reply(event, [_build_image_comp(pic)])


async def bind_pcrid(data):
    event: AstrMessageEvent = data["ev"]
    pcrid = data["uid"]
    info: dict = data["info"]
    if data.get("error"):
        await _send_reply(event, [Comp.Plain(f"绑定失败（{pcrid}）：{data['error']}")])
        return
    try:
        res = data["res"]["user_info"]
        qid = str(event.get_sender_id())
        have_bind: List[PCRBind] = await pcr_sqla.get_bind(info["platform"], qid)
        bind_num = len(have_bind)
        if bind_num >= 8:
            reply = "您订阅了太多账号啦！"
        elif pcrid in [bind.pcrid for bind in have_bind]:
            reply = "这个uid您已经订阅过了，不要重复订阅！"
        else:
            info["name"] = (
                info["name"]
                if info["name"]
                else filt_message(str((res["user_name"])))
            )
            await pcr_sqla.insert_bind(info)
            mode_tip = "私聊推送" if info.get("private") else "群聊推送"
            reply = f"添加成功！已为您开启{mode_tip}！"
    except Exception:
        logger.error(traceback.format_exc())
        reply = "找不到这个uid，大概率是你输错了！"
    await _send_reply(event, [Comp.Plain(reply)])


async def sendNotice(new: int, old: int, info: PCRBind, noticeType: int):
    global jjc_log
    if noticeType == NoticeType.online.value:
        change = "上线了！"
    else:
        if noticeType == NoticeType.jjc.value:
            change = "\njjc: "
        else:
            change = "\npjjc: "
        if new < old:
            change += f"""{old}->{new} [▲{old-new}]"""
        else:
            change += f"""{old}->{new} [▽{new-old}]"""

    msg = ""
    onlineNotice = False
    is_send = False
    key = (info.pcrid, info.user_id, info.platform)
    if info.online_notice and noticeType == NoticeType.online.value:
        if (new - old) < (60 if info.online_notice == 3 else 60 * 10):
            cache[key][2] = old
        elif info.online_notice != 1 or (
            (new % 86400 // 3600 + 8) % 24 == 14 and new % 3600 // 60 >= 30
        ):
            onlineNotice = True

    if (
        (
            (noticeType == NoticeType.jjc.value and info.jjc_notice)
            or (noticeType == NoticeType.pjjc.value and info.pjjc_notice)
        )
        and (info.up_notice or (new > old))
    ) or (noticeType == NoticeType.online.value and onlineNotice):
        logger.info(f"Send Notice FOR {info.user_id}({info.pcrid})")
        msg = info.name + change
        is_send = True
        if info.private:
            target_umo = info.private_umo or ""
            if target_umo:
                # 只入队、不等待发送：合并与 @ 去重交给 _notice_dispatcher
                _enqueue_notice(target_umo, msg, str(info.user_id), private=True)
            else:
                logger.warning(f"[pcrjjc] 用户 {info.user_id} 开启了私聊推送但尚未记录私聊路由，请先私聊机器人发送一次指令")
        else:
            target_umo = info.group_umo or ""
            if target_umo:
                _enqueue_notice(target_umo, msg, str(info.user_id), private=False)
            else:
                logger.warning(f"[pcrjjc] 群聊 {info.group} 尚未记录群路由，请在群内发送一次指令激活")

    if (noticeType != NoticeType.online.value) or is_send:
        # 逐条取当前时刻，不再依赖跨协程共享的全局快照
        ts = int(datetime.now().timestamp())
        jjc_log[info.platform].append(
            JJCHistory(
                user_id=info.user_id,
                pcrid=info.pcrid,
                name=info.name,
                platform=info.platform,
                date=ts,
                before=old,
                after=new,
                is_send=is_send,
                item=noticeType,
            )
        )
