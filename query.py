import traceback
import asyncio
import itertools
import time
from typing import List
from httpx import HTTPError
from astrbot.api import logger
from .client.pcrclient import pcrclient, bsdkclient, ApiException
from .database.dal import PCRBind, pcr_sqla, Account
from .var import Platform, Priority, platform_dict, queue_dict


class MatchError(Exception):
    pass


# 用于记录后台查询任务，方便插件重载时统一清理
_query_tasks = []

# 任务入队序号：同优先级内保证先进先出，并避免 PriorityQueue 去比较回调函数本身
# （原实现把 (pcrid, user_id, platform) 当作第二排序键，同一 UID 的重复指令会因
#  回调函数不可比较而在 heappush 时抛 TypeError）
_task_seq = itertools.count()

# 单个任务的最大尝试次数（含首次），超限则放弃并通知调用方
MAX_ATTEMPTS = 3

# 重试前的等待时间（秒）：旧实现零延迟重试，网络抖动持续 1 秒以上时
# 三次尝试会在几十毫秒内全部溃败，正好把“重试 3 次”变成摆设。
RETRY_DELAYS = (1.0, 3.0)

# 等待「本批轮询任务全部结束」的上限（秒）。
# 理论上每一条任务都会回报完成；这个上界只是兜底：万一某条异常路径漏了回报，
# 也不会把整个 query_loop 永久卡死，只会在日志里留下一条告警。
POLL_BATCH_WAIT_MAX = 600

# 可重试的错误特征（对应 client/pcrclient.py 里的分类）：
#   未知错误*         -> 本地/网络异常（连不上、超时、协议意外）
#   接口异常*         -> 非 200（网关错误页、限流页、维护页）
#   响应格式异常*     -> 200 但 body 不是协议数据（HTML/空包/截断）
# 服务器明确给出的结果（账号存在风险、没过完教程、UID 不存在等）不得重试。
RETRYABLE_MARKERS = ("未知错误", "接口异常", "响应格式异常")

# 这些错误指向“连接已不可用”（服务端断开 keep-alive、网关 5xx、TCP 重置），
# 重试前应先丢弃旧连接池，否则会拿着一条死连接反复重试。
CONN_RESET_MARKERS = (
    "Server disconnected",
    "接口异常：HTTP 5",
    "ConnectError",
    "ConnectTimeout",
    "ReadTimeout",
    "RemoteProtocol",
    "Connection reset",
    "Connection aborted",
)


def _should_reset_connection(reason: str) -> bool:
    return any(marker in reason for marker in CONN_RESET_MARKERS)


# ===================== 服务器维护感知退避 =====================
# 维护期（国服每月两次，北京时间 11:00 开始）服务器会持续不可用 3~7 小时。
# 旧实现每 30 秒就把全部绑定重新入队，而每个任务都会先走一遍完整登录
# （bsdk.b_login）才会在 check_dangerous() 里看到 maintenance_message，
# 结果维护期每小时会发起上千次 B 站登录：既容易触发风控（is_risk=1 会变成
# 无法自愈的“账号存在风险”），也会用 traceback 洗掉日志。
# 这里改成「检测到维护 → 整个平台退避 → 到点只探测一次」。
MAINTENANCE_BASE_DELAY = 300        # 首次探测间隔：5 分钟
MAINTENANCE_MAX_DELAY = 1800        # 退避封顶：30 分钟（即维护末期最多晚 30 分钟恢复）
MAINTENANCE_MARKER = "服务器在维护"

_maintenance_until: dict = {}        # platform -> 下次允许探测的 monotonic 时刻
_maintenance_backoff: dict = {}      # platform -> 当前退避秒数
_recovered_platforms: list = []       # 本轮刚恢复的平台，等 query_loop 取走发通知


def _platform_name(platform: int) -> str:
    return "渠道服" if platform == Platform.qu_id.value else "B服"


def maintenance_remaining(platform: int) -> float:
    """返回该平台还需退避的秒数；0 表示可正常查询/探测。"""
    remain = _maintenance_until.get(platform, 0.0) - time.monotonic()
    return remain if remain > 0 else 0.0


def _enter_maintenance(platform: int, reason: str) -> float:
    """登记一次维护命中，返回本次退避秒数（5→10→20→30 分钟封顶）。

    日志只在「从正常转入维护」时打一条 warning，后续探测降为 debug，
    避开维护期每轮每个 UID 刷 traceback 的日志洪水。
    """
    prev = _maintenance_backoff.get(platform)
    if prev is None:
        delay = MAINTENANCE_BASE_DELAY
        logger.warning(
            f"[pcrjjc] 检测到 {_platform_name(platform)} 服务器维护（{reason}），"
            f"已暂停高频轮询，{delay // 60} 分钟后探测一次"
        )
    else:
        delay = min(prev * 2, MAINTENANCE_MAX_DELAY)
        logger.debug(
            f"[pcrjjc] {_platform_name(platform)} 仍在维护，退避延长至 {delay // 60} 分钟"
        )
    _maintenance_backoff[platform] = delay
    _maintenance_until[platform] = time.monotonic() + delay
    return delay


def _exit_maintenance(platform: int) -> bool:
    """查询成功时调用；返回是否刚从维护中恢复。"""
    if platform not in _maintenance_backoff:
        return False
    _maintenance_backoff.pop(platform, None)
    _maintenance_until.pop(platform, None)
    _recovered_platforms.append(platform)
    logger.info(f"[pcrjjc] {_platform_name(platform)} 服务器维护结束，已恢复正常轮询")
    return True


def drain_recovered_platforms() -> list:
    """取出本次刚恢复的平台（由 query_loop 负责发管理员通知）。"""
    out = list(_recovered_platforms)
    _recovered_platforms.clear()
    return out


_clients: list = []


def cancel_query_tasks():
    for task in _query_tasks:
        if not task.done():
            task.cancel()
    _query_tasks.clear()


async def close_clients():
    """关闭所有 pcrclient 的 httpx 连接池（插件终止/重载时调用）"""
    while _clients:
        client = _clients.pop()
        try:
            await client.aclose()
        except Exception:
            logger.debug(f"关闭查询客户端失败：{traceback.format_exc()}")


def _retryable_from_apiexc(msg: str) -> bool:
    """判断 ApiException 是否值得重试。

    ・未知错误 / 接口异常 / 响应格式异常 都是传输层问题（网络、非 200 响应、
      响应体不是协议数据），重试有意义。
    ・“服务器在维护”“账号存在风险”“该账号没过完教程”“UID 不存在” 是服务器
      已给出的明确结果，重试只会把 queue.join() 拖住，一律快速失败。
    """
    return any(marker in msg for marker in RETRYABLE_MARKERS)


def _signal_batch_done(result_storage) -> None:
    """回报「本任务已结束」（只对后台轮询批次有效；其它任务没有这个标记，直接忽略）。"""
    if not isinstance(result_storage, dict):
        return
    done_queue = result_storage.pop("_batch_done", None)
    if done_queue is None:
        return
    try:
        done_queue.put_nowait(1)
    except Exception:
        pass


def _with_batch_done(callback):
    """给轮询回调包一层：处理完（含回调自身抛错）后回报批次完成。"""

    async def _wrapped(result_storage):
        try:
            await callback(result_storage)
        finally:
            _signal_batch_done(result_storage)

    return _wrapped


async def _wait_batch_done(done_queue, count: int) -> None:
    """等到本批 count 条任务都回报结束；超时只告警，绝不无限等待。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + POLL_BATCH_WAIT_MAX
    finished = 0
    while finished < count:
        remain = deadline - loop.time()
        if remain <= 0:
            break
        try:
            await asyncio.wait_for(done_queue.get(), remain)
        except asyncio.TimeoutError:
            break
        finished += 1
    if finished < count:
        logger.warning(
            f"[pcrjjc] 本轮轮询等待超时（{POLL_BATCH_WAIT_MAX}s 内完成 {finished}/{count} 条），"
            "本轮先继续，剩余结果仍会照常回调"
        )


async def _give_up(task, reason: str, viewer_id: str):
    """放弃任务：仅在 notify_on_error 为真时通知调用方，且每个任务最多通知一次。"""
    callback, info, result_storage, _, notify_on_error = task
    logger.info(f"放弃：{viewer_id} 查询 {info.pcrid} 失败（{reason}）")
    if not notify_on_error:
        # 轮询任务（query_rank）不接收失败通知，避免往击剑记录/负载统计里塞假变动；
        # 但必须回报批次完成，否则 query_loop 会一直等着这一条。
        _signal_batch_done(result_storage)
        return
    try:
        result_storage["res"] = None
        result_storage["uid"] = int(info.pcrid)
        result_storage["bind_info"] = info
        result_storage["error"] = reason
        await callback(result_storage)
    except Exception:
        logger.error(f"[pcrjjc] 失败回调自身异常：{traceback.format_exc()}")


async def _query(client: pcrclient, platform_id: int):
    if platform_id == 0:
        viewer_id: str = client.bsdk.account
    else:
        viewer_id: str = str(client.viewer_id)
    queue: asyncio.PriorityQueue = queue_dict[platform_id]

    while True:
        DA = await queue.get()
        queue.task_done()
        callback, info, result_storage, attempts, notify_on_error = DA[2]
        uid = int(info.pcrid)
        reason = None
        retryable = False

        # 平台正处于维护退避期：一个请求都不发（也就不再触发 B 站登录）。
        # 用户主动查询仍给出明确回复，后台轮询任务静默丢弃。
        if remain := maintenance_remaining(platform_id):
            if notify_on_error:
                await _give_up(
                    DA[2],
                    f"服务器维护中，约 {int(remain // 60) + 1} 分钟后恢复探测",
                    viewer_id,
                )
            else:
                # 轮询任务在维护期被直接丢弃，同样要回报批次完成
                _signal_batch_done(result_storage)
            continue

        try:
            res = await client.callapi('/profile/get_profile', {'target_viewer_id': uid})
            if 'user_info' not in res:
                await client.login()  # 会话可能已失效，重登后再试一次
                res = await client.callapi('/profile/get_profile', {'target_viewer_id': uid})
            # 重登后的第二次响应同样必须校验。原实现用 elif，第二次的结果被无
            # 条件当成成功：UID 不存在时会白跑一次完整登录，然后把错误体当正常
            # 结果传下去，回调里的 KeyError 被各自的 try/except 吞掉，表现为
            # “轮询静默失效 + 零日志”。
            user_info = res.get('user_info') if isinstance(res, dict) else None
            if not user_info:
                raise ApiException('该 UID 不存在或查询受限', 404)
            if str(user_info.get('viewer_id')) != str(uid):
                # 复用了缓存会话但服务端不认（viewer_id 已失效）：清掉缓存，
                # 让下一次重试走完整登录，而不是拿着旧会话反复 MatchError。
                if getattr(client, "_session_restored", False):
                    client.reset_session()
                raise MatchError
            result_storage["res"] = res
            result_storage["uid"] = uid
            result_storage["bind_info"] = info
        except asyncio.CancelledError:
            # 插件重载被取消时，优雅退出（先回报，避免等待方卡到超时）
            _signal_batch_done(result_storage)
            break
        except RuntimeError as e:
            if "different event loop" in str(e):
                logger.warning(f"{viewer_id} 事件循环已变更，停止旧的后台查询队列。")
                _signal_batch_done(result_storage)
                break
            reason = f"运行时错误：{e}"
            logger.error(traceback.format_exc())
        except ApiException as e:
            reason = str(e)
            retryable = _retryable_from_apiexc(reason)
            if MAINTENANCE_MARKER in reason:
                # 维护：整个平台退避，并且不打 traceback（维护期这点本就是预期内的）
                delay = _enter_maintenance(platform_id, reason)
                reason = f"服务器维护中，约 {delay // 60} 分钟后恢复探测"
            else:
                # 单行即可定因（带分类与状态码）；不再每次失败都打三行 traceback
                logger.info(f"[pcrjjc] {viewer_id} 查询 {uid} 失败：{reason}")
        except (HTTPError, MatchError):
            reason = "查询超时或返回的 UID 不匹配"
            retryable = True
            logger.info(f"{viewer_id} 对于 {uid} 的查询请求超时或不匹配。")
        except Exception as e:
            reason = f"未知异常：{e}"
            logger.info(f"失败：{viewer_id} 未能完成查询请求。")
            logger.info(traceback.format_exc())

        if reason is None:
            # 查询成功。回调自身的异常不属于“查询失败”，只记日志，
            # 否则同一个任务会被重复通知（聚合计数会被弄乱）
            _exit_maintenance(platform_id)
            try:
                await callback(result_storage)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.error(f"[pcrjjc] {viewer_id} 处理 {uid} 的回调异常：{traceback.format_exc()}")
            continue

        # 本轮查询失败：临时故障且未达上限则退避后重试，否则放弃并只通知一次
        if retryable and attempts + 1 < MAX_ATTEMPTS:
            delay = RETRY_DELAYS[min(attempts, len(RETRY_DELAYS) - 1)]
            logger.info(
                f"重试（第 {attempts + 2}/{MAX_ATTEMPTS} 次，{delay:g}s 后）：{viewer_id} 查询 {uid}"
            )
            # 连接类错误：先把旧连接池丢掉，避免复用服务端已关闭的 keep-alive 连接
            if _should_reset_connection(reason):
                reconnect = getattr(client, "reconnect", None)
                if reconnect:
                    await reconnect()
            await asyncio.sleep(delay)
            await queue.put(
                (DA[0], next(_task_seq), (callback, info, result_storage, attempts + 1, notify_on_error))
            )
            continue
        await _give_up(DA[2], reason, viewer_id)


async def query1(query_list: List[PCRBind], platform, function, result_storage: dict = None, priority: int = Priority.query_all.value, notify_on_error: bool = True):
    # 可变默认参数（原为 {}）改掉：虽然下面会做浅拷贝，但默认值仍会在多次调用间共享
    if result_storage is None:
        result_storage = {}
    queue: asyncio.PriorityQueue = queue_dict[platform]
    if not queue:
        logger.warning(f"未绑定{platform_dict.get(platform, 'B服')}服务器的查询账户，已自动忽略")
        return

    # 后台轮询改成「只等本批」，不再用 queue.join()：
    # join() 等的是队列里“所有”任务——上一轮残留、用户临时触发的详细查询、
    # 退避 1s+3s 的慢重试全都算在内，一条慢任务就能把整轮轮询拖住几十秒。
    # 现在每条任务结束（成功 / 失败 / 放弃 / 被取消）时向本批次的完成队列回报一次。
    is_poll = priority == Priority.query_all.value
    done_queue = asyncio.Queue() if is_poll else None
    if done_queue is not None:
        function = _with_batch_done(function)

    # 每个任务一份独立的 result_storage 浅拷贝：避免同平台多个 worker 并发写同一个 dict 串号
    jobs = []
    for info in query_list:
        storage = dict(result_storage)
        if done_queue is not None:
            storage["_batch_done"] = done_queue
        jobs.append((priority, next(_task_seq), (function, info, storage, 0, notify_on_error)))
    await asyncio.gather(*[queue.put(job) for job in jobs])

    if done_queue is not None:
        await _wait_batch_done(done_queue, len(jobs))


# 仅保留本地模式：由本插件配置的查询小号直连游戏服务器轮询
query_all = query1


async def login_all(admin_id=None):
    acinfo: List[Account] = await pcr_sqla.select_account()
    for i in acinfo:
        if i.platform not in (Platform.b_id.value, Platform.qu_id.value):
            continue

        client = pcrclient(bsdkclient(i.account, i.password, i.platform, admin_id=admin_id))

        if not queue_dict[i.platform]:
            queue_dict[i.platform] = asyncio.PriorityQueue()

        task = asyncio.create_task(_query(client, i.platform))
        _query_tasks.append(task)
        _clients.append(client)

        logger.info(f"加载账号：ID{int(i.viewer_id) or i.account}, 服务器：{platform_dict.get(i.platform, 'B服')}")
