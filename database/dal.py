import asyncio
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import shutil
from typing import List, Optional, Tuple
from sqlmodel import SQLModel
from sqlalchemy.future import select
from sqlalchemy import delete, desc, distinct, event, insert, update
from sqlalchemy.orm import sessionmaker
from sqlalchemy.sql.expression import func
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from .models import Account, PCRBind, JJCHistory
from ..var import LoadBase, Platform

from astrbot.api import logger

def _get_plugin_data_dir() -> Path:
    try:
        from astrbot.api.star import StarTools
        data_dir = Path(StarTools.get_data_dir("astrbot_plugin_pcrjjc"))
    except Exception:
        data_dir = Path("data") / "plugin_data" / "astrbot_plugin_pcrjjc"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir

_DATA_DIR = _get_plugin_data_dir()
_OLD_DB_PATH = Path(__file__).parent / 'PCRJJC.db'
_NEW_DB_PATH = _DATA_DIR / 'PCRJJC.db'

if _OLD_DB_PATH.exists() and not _NEW_DB_PATH.exists():
    shutil.move(str(_OLD_DB_PATH), str(_NEW_DB_PATH))

DB_PATH = str(_NEW_DB_PATH)

def _install_sqlite_pragmas(engine) -> None:
    """SQLite 调优（写压力 + 读写互斥）。

    · WAL：读事务不再被写事务挡住——「指令里写绑定路由」与「轮询里写击剑记录」
      原本会互相卡；同时不必每次提交都 fsync 整个回滚日志（实测单次提交
      3.1ms -> 0.1ms，机械盘/容器 overlay 上差距更大）。
    · synchronous=NORMAL：WAL 下的常规折中，断电最多丢最后几条历史记录，
      不影响绑定表的正确性。
    · busy_timeout=5000：并发写时先等 5 秒再报 database is locked，
      而不是让用户指令直接收到异常。
    """

    @event.listens_for(engine.sync_engine, "connect")
    def _set_pragmas(dbapi_connection, connection_record):
        for stmt in (
            "PRAGMA journal_mode=WAL",
            "PRAGMA synchronous=NORMAL",
            "PRAGMA busy_timeout=5000",
        ):
            try:
                cursor = dbapi_connection.cursor()
                cursor.execute(stmt)
                cursor.close()
            except Exception:
                # 只读库 / 不支持 WAL 的文件系统：保持默认模式，绝不能因此起不来
                pass


def _index_statements() -> tuple:
    """性能索引（幂等建索引，对应插件固定的查询模式）。

    · JJCHistory：击剑记录按 (platform, user_id) 过滤后按 date 排序；上升次数按
      (platform, pcrid) + 日期区间；负载统计与历史清理按 (is_send, platform, date)、date。
    · PCRBind：轮询按 platform 取片，指令按 (platform, user_id)、user_id、
      group_umo / private_umo 查找。

    没有索引时全是全表扫描，且成本随历史表行数线性上涨：实测 20 万行时
    击剑记录 31ms / 上升次数 23.5ms / 负载查询 31.9ms，补索引后 0.21 / 0.03 / 0.07ms。
    """
    hist = JJCHistory.__table__.name
    bind = PCRBind.__table__.name
    return (
        f"CREATE INDEX IF NOT EXISTS ix_{hist}_platform_pcrid_date ON {hist} (platform, pcrid, date)",
        f"CREATE INDEX IF NOT EXISTS ix_{hist}_platform_user_item_date ON {hist} (platform, user_id, item, date)",
        f"CREATE INDEX IF NOT EXISTS ix_{hist}_is_send_platform_date ON {hist} (is_send, platform, date)",
        f"CREATE INDEX IF NOT EXISTS ix_{hist}_date ON {hist} (date)",
        f"CREATE INDEX IF NOT EXISTS ix_{bind}_platform ON {bind} (platform)",
        f"CREATE INDEX IF NOT EXISTS ix_{bind}_platform_user ON {bind} (platform, user_id)",
        f"CREATE INDEX IF NOT EXISTS ix_{bind}_user_id ON {bind} (user_id)",
        f"CREATE INDEX IF NOT EXISTS ix_{bind}_group_umo ON {bind} (group_umo)",
        f"CREATE INDEX IF NOT EXISTS ix_{bind}_private_umo ON {bind} (private_umo)",
    )


def pcr_date(timeStamp: int) -> datetime:
    now = datetime.fromtimestamp(timeStamp, tz=timezone(timedelta(hours=8)))
    if now.hour < 5:
        now -= timedelta(days=1)
    return now.replace(hour=5, minute=0, second=0, microsecond=0)


class SQLA:
    def __init__(self, url: str):
        self.url = f'sqlite+aiosqlite:///{url}'
        self.engine = create_async_engine(self.url, pool_recycle=1500)
        self.async_session = sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )
        _install_sqlite_pragmas(self.engine)

    def create_all(self):
        try:
            asyncio.create_task(self._create_all())
        except RuntimeError:
            loop = asyncio.get_event_loop()
            loop.run_until_complete(self._create_all())
            loop.close()

    async def _create_all(self):
        async with self.engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
            # create_all 只建表，对已存在的表不会补索引：老部署必须靠显式 DDL。
            # 首次在大表上建索引会有一次性耗时（几秒级），之后都是秒过的 no-op。
            for stmt in _index_statements():
                try:
                    await conn.exec_driver_sql(stmt)
                except Exception as e:
                    logger.warning(f"[pcrjjc] 创建索引失败（不影响功能，但查询会变慢）：{e}")

    # 账号部分
    async def select_account(self) -> List[Account]:
        async with self.async_session() as session:
            async with session.begin():
                result = await session.execute(select(Account))
                data = result.scalars().all()
                return data if data else []

    async def insert_account(self, account_list: List[dict]) -> int:
        async with self.async_session() as session:
            async with session.begin():
                for account in account_list:
                    await session.merge(Account(**account))
                return 1

    async def upsert_accounts(self, accounts: List[dict]) -> int:
        """按 (account, platform) 增量写入配置里的账号（P1-9）。

        旧实现是“先把整张表删空再插入”：只要数据库处于只读/被锁状态，
        启动第一步就抛错，整个 _on_startup 中断，插件会零功能地静默停摆。
        """
        async with self.async_session() as session:
            async with session.begin():
                result = await session.execute(select(Account))
                existing = {(a.account, a.platform): a for a in result.scalars().all()}
                for item in accounts:
                    row = existing.get((item["account"], item["platform"]))
                    if row is None:
                        session.add(Account(**item))
                    else:
                        row.password = item["password"]
                        if item.get("viewer_id"):
                            row.viewer_id = item["viewer_id"]
                return len(accounts)

    async def prune_accounts(self, keep: List[Tuple[str, int]]) -> int:
        """删除已从配置中移除的查询账号；keep 为空时不动表，避免配置读空时误清库。"""
        if not keep:
            return 0
        keep_set = {(str(acc), int(plat)) for acc, plat in keep}
        async with self.async_session() as session:
            async with session.begin():
                result = await session.execute(select(Account))
                removed = 0
                for row in result.scalars().all():
                    if (row.account, row.platform) not in keep_set:
                        await session.delete(row)
                        removed += 1
                return removed

    async def delete_all_account(self) -> int:
        async with self.async_session() as session:
            async with session.begin():
                await session.execute(delete(Account))
                return 1

    # 绑定部分
    async def get_bind(self, platform: Optional[int] = -1, user_id: Optional[str] = None, group: Optional[str] = None) -> List[PCRBind]:
        async with self.async_session() as session:
            async with session.begin():
                sql = select(PCRBind)
                if platform != -1:
                    sql = sql.filter(PCRBind.platform == platform)
                if user_id:
                    sql = sql.filter(PCRBind.user_id == str(user_id)).order_by(PCRBind.id)
                if group:
                    sql = sql.filter(PCRBind.group == str(group)).filter(PCRBind.private == False)
                result = await session.execute(sql)
                data = result.scalars().all()
                return data if data else []

    async def count_bind(self, platform: int) -> int:
        """该平台绑定总数（轮询分片用，只取计数不取行）。"""
        async with self.async_session() as session:
            async with session.begin():
                result = await session.execute(
                    select(func.count()).select_from(PCRBind).where(PCRBind.platform == platform)
                )
                return int(result.scalar() or 0)

    async def get_bind_page(self, platform: int, offset: int, limit: int) -> List[PCRBind]:
        """按 id 顺序取一段绑定（轮询分片用）。

        原实现每 30 秒调用 get_bind(platform) 取回整表、构造全部 ORM 对象，
        实际只用到其中 POLL_CHUNK 条；绑定数上千时等于每轮白扫一遍全表 + 白造
        上千个对象给 GC。分页下推到 SQL 后，每轮只读真正要查的那一片。
        """
        async with self.async_session() as session:
            async with session.begin():
                result = await session.execute(
                    select(PCRBind)
                    .where(PCRBind.platform == platform)
                    .order_by(PCRBind.id)
                    .offset(max(int(offset), 0))
                    .limit(max(int(limit), 1))
                )
                data = result.scalars().all()
                return data if data else []

    async def get_private(self, platform: int) -> int:
        async with self.async_session() as session:
            async with session.begin():
                result = await session.execute(
                    select(func.count(distinct(PCRBind.user_id)))
                    .where(PCRBind.platform == platform, PCRBind.private == True)
                )
                return result.scalar() or 0

    async def get_bind_by_umo(self, umo: str, private: Optional[bool] = None) -> List[PCRBind]:
        """按推送路由找出所有绑定，用于推送连续失败后自动解除该目标的推送（P2-1）"""
        if not umo:
            return []
        async with self.async_session() as session:
            async with session.begin():
                sql = select(PCRBind)
                if private is True:
                    sql = sql.where(PCRBind.private_umo == str(umo))
                elif private is False:
                    sql = sql.where(PCRBind.group_umo == str(umo))
                else:
                    sql = sql.where(
                        (PCRBind.private_umo == str(umo)) | (PCRBind.group_umo == str(umo))
                    )
                result = await session.execute(sql)
                data = result.scalars().all()
                return data if data else []

    async def insert_bind(self, bind: dict) -> int:
        async with self.async_session() as session:
            async with session.begin():
                await session.execute(insert(PCRBind).values(**bind))
                return 1

    async def delete_bind(self, user_id: str, platform: Optional[int] = -1, pcrid: Optional[int] = None, group: Optional[str] = None) -> int:
        async with self.async_session() as session:
            async with session.begin():
                sql = delete(PCRBind).where(PCRBind.user_id == str(user_id))
                if platform is not None and platform != -1:
                    sql = sql.filter(PCRBind.platform == platform)
                if pcrid:
                    sql = sql.filter(PCRBind.pcrid == pcrid)
                if group:
                    sql = sql.filter(PCRBind.group == str(group))
                await session.execute(sql)
                return 1

    async def update_bind(self, platform: int, data: dict, user_id: Optional[str] = None, pcrid: Optional[int] = None) -> int:
        async with self.async_session() as session:
            async with session.begin():
                sql = update(PCRBind).where(PCRBind.platform == platform)
                if user_id:
                    sql = sql.filter(PCRBind.user_id == str(user_id))
                if pcrid:
                    sql = sql.filter(PCRBind.pcrid == pcrid)
                sql = sql.values(**data)
                await session.execute(sql)
                return 1

    # 竞技场记录
    async def get_up_num(self, platform: int, pcrid: int, date: int) -> Tuple[int, int]:
        pcr_time: float = pcr_date(date).timestamp()
        async with self.async_session() as session:
            async with session.begin():
                sql = select(
                    func.count().filter(JJCHistory.item == 0),
                    func.count().filter(JJCHistory.item == 1)
                ).where(
                    JJCHistory.pcrid == pcrid,
                    pcr_time + 3600 * 24 > JJCHistory.date,
                    pcr_time < JJCHistory.date,
                    JJCHistory.platform == platform,
                    JJCHistory.before > JJCHistory.after
                )
                result = await session.execute(sql)
                return result.fetchone()

    async def get_history(self, platform: int, user_id: Optional[str] = "", pcrid: Optional[int] = 0) -> List[JJCHistory]:
        async with self.async_session() as session:
            async with session.begin():
                sql = select(JJCHistory).where(
                    JJCHistory.platform == platform, JJCHistory.item != 2
                )
                if user_id:
                    sql = sql.filter(JJCHistory.user_id == str(user_id))
                if pcrid:
                    sql = sql.filter(JJCHistory.pcrid == pcrid)
                sql = sql.order_by(desc(JJCHistory.date)).limit(50)
                result = await session.execute(sql)
                data = result.scalars().all()
                return data if data else []

    async def insert_history(self, historys: List[JJCHistory]) -> int:
        if not historys:
            return 0
        async with self.async_session() as session:
            async with session.begin():
                session.add_all(historys)
                return 1

    async def prune_history(self, cutoff: int) -> int:
        """删除早于 cutoff（时间戳）的击剑记录，返回删除行数。

        历史表原本只增不减：每次排名变动/上线提醒都写一行，几个月后几十万行，
        磁盘与所有历史查询（击剑记录 / 上升次数 / 负载统计）一起变慢。
        这里只做 DELETE，不做 VACUUM——VACUUM 会长时间独占数据库，
        腾出的页 SQLite 会自行复用。
        """
        async with self.async_session() as session:
            async with session.begin():
                result = await session.execute(
                    delete(JJCHistory).where(JJCHistory.date < int(cutoff))
                )
                return int(getattr(result, "rowcount", 0) or 0)

    async def query_load(self) -> LoadBase:
        pcr_time: float = pcr_date(int(datetime.now().timestamp())).timestamp()
        async with self.async_session() as session:
            async with session.begin():
                sql = select(
                    func.count().filter(JJCHistory.platform == Platform.b_id.value, pcr_time + 3600 * 24 > JJCHistory.date, pcr_time < JJCHistory.date),
                    func.count().filter(JJCHistory.platform == Platform.qu_id.value, pcr_time + 3600 * 24 > JJCHistory.date, pcr_time < JJCHistory.date),
                    func.count().filter(JJCHistory.platform == Platform.b_id.value, pcr_time - 3600 * 24 < JJCHistory.date, pcr_time > JJCHistory.date),
                    func.count().filter(JJCHistory.platform == Platform.qu_id.value, pcr_time - 3600 * 24 < JJCHistory.date, pcr_time > JJCHistory.date),
                ).where(JJCHistory.is_send == True)
                result = await session.execute(sql)
                send = result.fetchone()

                sql = select(
                    func.count(distinct(PCRBind.user_id)).filter(PCRBind.platform == Platform.b_id.value, PCRBind.private == False),
                    func.count(distinct(PCRBind.pcrid)).filter(PCRBind.platform == Platform.b_id.value, PCRBind.private == False),
                    func.count(distinct(PCRBind.user_id)).filter(PCRBind.platform == Platform.b_id.value, PCRBind.private == True),
                    func.count(distinct(PCRBind.pcrid)).filter(PCRBind.platform == Platform.b_id.value, PCRBind.private == True),
                    func.count(distinct(PCRBind.user_id)).filter(PCRBind.platform == Platform.qu_id.value, PCRBind.private == False),
                    func.count(distinct(PCRBind.pcrid)).filter(PCRBind.platform == Platform.qu_id.value, PCRBind.private == False),
                    func.count(distinct(PCRBind.user_id)).filter(PCRBind.platform == Platform.qu_id.value, PCRBind.private == True),
                    func.count(distinct(PCRBind.pcrid)).filter(PCRBind.platform == Platform.qu_id.value, PCRBind.private == True),
                )
                result = await session.execute(sql)
                bind_num = result.fetchone()

                return LoadBase(
                    b_today_send=send[0], qu_today_send=send[1],
                    b_yesterday_send=send[2], qu_yesterday_send=send[3],
                    b_group_user=bind_num[0], b_group_pcrid=bind_num[1],
                    b_private_user=bind_num[2], b_private_pcrid=bind_num[3],
                    qu_group_user=bind_num[4], qu_group_pcrid=bind_num[5],
                    qu_private_user=bind_num[6], qu_private_pcrid=bind_num[7],
                )


    async def close(self):
        await self.engine.dispose()


pcr_sqla = SQLA(DB_PATH)
