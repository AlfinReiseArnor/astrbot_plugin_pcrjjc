from .database.dal import pcr_sqla
from astrbot.api import logger


async def refresh_account(account_list: list = None):
    """把 WebUI 配置里的查询账号同步进数据库（增量，不清表）。

    旧实现先 `DELETE FROM account` 再插入：数据库只读/被锁时第一步就抛错，
    使 main._on_startup 整个中断，插件零功能静默停摆（线上 11-23 出现过两次
    “attempt to write a readonly database”）。现在改为增量同步，失败也不清空。
    """
    await pcr_sqla._create_all()

    valid_accounts = []
    if account_list:
        for item in account_list:
            acc = str(item.get("account", "")).strip()
            pwd = str(item.get("password", "")).strip()
            if acc and pwd:
                valid_accounts.append({
                    "viewer_id": "0",
                    "account": acc,
                    "password": pwd,
                    "platform": int(item.get("platform", 0)),
                })

    if valid_accounts:
        await pcr_sqla.upsert_accounts(valid_accounts)
        removed = await pcr_sqla.prune_accounts(
            [(a["account"], a["platform"]) for a in valid_accounts]
        )
        logger.info(
            f"[pcrjjc] 后台配置账号刷新完毕，共加载 {len(valid_accounts)} 个有效账号"
            + (f"（移除 {removed} 个已下线的账号）" if removed else "")
        )
    else:
        # 配置为空时保留库里已有账号，避免“配置读取失败”被当成“清空账号”
        logger.warning(
            "[pcrjjc] WebUI 未配置任何查询账号，已保留数据库中已有的账号；"
            "如需停止查询请清空绑定或停用插件"
        )
